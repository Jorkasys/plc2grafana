"""Подключение к PostgreSQL и первичная установка схемы.

Приложение должно быть самодостаточным, поэтому оно само:
  * создаёт роль приложения и роль только на чтение (для Grafana);
  * создаёт базу данных;
  * накатывает схему из db/*.sql;
  * включает TimescaleDB, если расширение есть, и молча продолжает без него.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass

import asyncpg

from .settings import DB_SQL_DIR, DatabaseSettings, Settings

log = logging.getLogger("app.db")


class DatabaseError(RuntimeError):
    """Понятная человеку ошибка подключения/установки."""


@dataclass(slots=True)
class DbInfo:
    server_version: str
    timescaledb: bool
    database: str
    user: str


class Database:
    """Пул соединений приложения + сведения о возможностях сервера."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.pool: asyncpg.Pool | None = None
        self.info: DbInfo | None = None
        self.last_error: str | None = None

    @property
    def cfg(self) -> DatabaseSettings:
        return self.settings.database

    @property
    def connected(self) -> bool:
        return self.pool is not None

    # ------------------------------------------------------------------ пул
    async def connect(self) -> None:
        """Поднять пул. Бросает DatabaseError с внятным текстом."""
        await self.close()
        try:
            self.pool = await asyncpg.create_pool(
                dsn=self.cfg.dsn,
                min_size=self.cfg.min_pool_size,
                max_size=self.cfg.max_pool_size,
                command_timeout=60,
            )
            async with self.pool.acquire() as conn:
                version = await conn.fetchval("SHOW server_version")
                has_ts = await conn.fetchval(
                    "SELECT count(*) > 0 FROM pg_extension WHERE extname = 'timescaledb'"
                )
            self.info = DbInfo(str(version), bool(has_ts), self.cfg.name, self.cfg.user)
            self.last_error = None
            log.info("БД подключена: %s (PostgreSQL %s, TimescaleDB: %s)",
                     self.cfg.safe_dsn(), version, "да" if has_ts else "нет")
        except Exception as exc:  # noqa: BLE001
            self.pool = None
            self.last_error = _human(exc)
            raise DatabaseError(self.last_error) from exc

    async def close(self) -> None:
        pool, self.pool = self.pool, None
        if pool is not None:
            try:
                await pool.close()
            except Exception:  # noqa: BLE001
                pool.terminate()

    def acquire(self):
        if self.pool is None:
            raise DatabaseError("нет соединения с базой данных")
        return self.pool.acquire()

    # -------------------------------------------------------------- установка
    async def bootstrap(self) -> list[str]:
        """Создать роли, базу и схему. Возвращает журнал выполненных шагов."""
        cfg = self.cfg
        steps: list[str] = []

        if not cfg.password:
            cfg.password = secrets.token_urlsafe(18)
            steps.append("сгенерирован пароль пользователя приложения")
        if not cfg.readonly_password:
            cfg.readonly_password = secrets.token_urlsafe(18)

        admin = await self._connect_admin()
        try:
            await _ensure_role(admin, cfg.user, cfg.password, steps)
            await _ensure_role(admin, cfg.readonly_user, cfg.readonly_password, steps)

            exists = await admin.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", cfg.name)
            if not exists:
                await admin.execute(
                    f'CREATE DATABASE "{cfg.name}" OWNER "{cfg.user}" ENCODING \'UTF8\'')
                steps.append(f"создана база данных {cfg.name}")
            else:
                steps.append(f"база данных {cfg.name} уже существует")
        finally:
            await admin.close()

        # Дальше работаем уже внутри целевой базы, от имени администратора:
        # только он может создать расширение и раздать права.
        admin_db = await self._connect_admin(database=cfg.name)
        try:
            has_ts = await _try_timescale(admin_db, steps)
            await admin_db.execute(
                f'GRANT ALL ON SCHEMA public TO "{cfg.user}"; '
                f'ALTER SCHEMA public OWNER TO "{cfg.user}"'
            )
        finally:
            await admin_db.close()

        # Схему накатываем от имени владельца, чтобы таблицы принадлежали ему
        owner = await asyncpg.connect(dsn=cfg.dsn)
        try:
            await owner.execute(_read_sql("001_core.sql"))
            steps.append("схема 001_core применена")
            if has_ts:
                failed = await _apply_timescale(owner)
                if failed:
                    steps.append("TimescaleDB-часть применена частично: " + "; ".join(failed))
                else:
                    steps.append("схема 002_timescale применена (гипертаблица, агрегаты, сжатие)")
            else:
                steps.append("TimescaleDB не найден — работаем на обычном PostgreSQL")
            await _grant_readonly(owner, cfg.readonly_user, has_ts, steps)
        finally:
            await owner.close()

        cfg.configured = True
        self.settings.save()
        await self.connect()
        return steps

    async def _connect_admin(self, database: str | None = None):
        cfg = self.cfg
        dsn = cfg.admin_dsn if database is None else _admin_dsn_for(cfg, database)
        try:
            return await asyncpg.connect(dsn=dsn, timeout=10)
        except Exception as exc:  # noqa: BLE001
            raise DatabaseError(
                f"не удалось подключиться к PostgreSQL как {cfg.admin_user}: {_human(exc)}"
            ) from exc

    # ----------------------------------------------------------- обслуживание
    async def migrate(self) -> None:
        """Догнать схему до текущей версии кода.

        001_core.sql целиком идемпотентен (IF NOT EXISTS, ADD COLUMN IF NOT
        EXISTS, CREATE OR REPLACE VIEW), поэтому его безопасно применять при
        каждом старте. Обновление приложения сводится к git pull и перезапуску.
        """
        if self.pool is None:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(_read_sql("001_core.sql"))
        except DatabaseError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DatabaseError(_human(exc)) from exc

    async def apply_retention(self) -> int:
        """Удалить сырьё старше retention_days. Для баз без TimescaleDB."""
        days = self.cfg.retention_days
        if days <= 0 or self.pool is None:
            return 0
        if self.info and self.info.timescaledb:
            async with self.acquire() as conn:
                await conn.execute(
                    "SELECT add_retention_policy('tag_value', $1::interval, "
                    "if_not_exists => TRUE)", f"{days} days")
            return 0
        async with self.acquire() as conn:
            status = await conn.execute(
                "DELETE FROM tag_value WHERE ts < now() - $1::interval", f"{days} days")
        try:
            return int(status.rsplit(" ", 1)[1])
        except (IndexError, ValueError):
            return 0


# ---------------------------------------------------------------------------
#  Вспомогательное
# ---------------------------------------------------------------------------
def _admin_dsn_for(cfg: DatabaseSettings, database: str) -> str:
    from urllib.parse import quote

    return (f"postgresql://{quote(cfg.admin_user)}:{quote(cfg.admin_password)}"
            f"@{cfg.host}:{cfg.port}/{database}")


def split_sql(text: str) -> list[str]:
    """Разбить SQL-файл на отдельные операторы.

    Нужен для TimescaleDB: CREATE MATERIALIZED VIEW ... WITH
    (timescaledb.continuous) нельзя выполнять внутри транзакции, а несколько
    операторов одним запросом PostgreSQL выполняет как раз в одной неявной
    транзакции. Файлы схемы простые: без функций и точек с запятой в строках.
    """
    lines = []
    for line in text.splitlines():
        code = line.split("--", 1)[0].rstrip()
        if code:
            lines.append(code)
    statements = chr(10).join(lines).split(";")
    return [stmt.strip() for stmt in statements if stmt.strip()]


async def _apply_timescale(conn) -> list[str]:
    """Применить 002_timescale.sql по одному оператору. Возвращает ошибки."""
    failed: list[str] = []
    for statement in split_sql(_read_sql("002_timescale.sql")):
        try:
            await conn.execute(statement)
        except Exception as exc:  # noqa: BLE001
            head = " ".join(statement.split()[:4])
            failed.append(f"{head}…: {_human(exc)}")
            log.warning("TimescaleDB: %s — %s", head, exc)
    return failed


def _read_sql(name: str) -> str:
    path = DB_SQL_DIR / name
    if not path.is_file():
        raise DatabaseError(f"не найден файл схемы {path}")
    return path.read_text(encoding="utf-8")


async def _ensure_role(conn, name: str, password: str, steps: list[str]) -> None:
    exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", name)
    if exists:
        await conn.execute(f'ALTER ROLE "{name}" WITH LOGIN PASSWORD $${password}$$')
        steps.append(f"пароль роли {name} обновлён")
    else:
        await conn.execute(f'CREATE ROLE "{name}" LOGIN PASSWORD $${password}$$')
        steps.append(f"создана роль {name}")


async def _try_timescale(conn, steps: list[str]) -> bool:
    available = await conn.fetchval(
        "SELECT count(*) > 0 FROM pg_available_extensions WHERE name = 'timescaledb'")
    if not available:
        return False
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        steps.append("расширение timescaledb включено")
        return True
    except Exception as exc:  # noqa: BLE001
        steps.append(f"timescaledb доступен, но не включился: {_human(exc)}")
        return False


async def _grant_readonly(conn, role: str, has_ts: bool, steps: list[str]) -> None:
    statements = [
        f'GRANT USAGE ON SCHEMA public TO "{role}"',
        f'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{role}"',
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO "{role}"',
    ]
    if has_ts:
        # Без доступа к внутренним чанкам SELECT из гипертаблицы упадёт
        statements += [
            f'GRANT USAGE ON SCHEMA _timescaledb_internal TO "{role}"',
            f'GRANT SELECT ON ALL TABLES IN SCHEMA _timescaledb_internal TO "{role}"',
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA _timescaledb_internal '
            f'GRANT SELECT ON TABLES TO "{role}"',
        ]
    for sql in statements:
        try:
            await conn.execute(sql)
        except Exception as exc:  # noqa: BLE001
            log.debug("грант пропущен (%s): %s", sql, exc)
    steps.append(f"роли {role} выданы права только на чтение")


def _human(exc: Exception) -> str:
    """Сообщение asyncpg → понятная человеку подсказка."""
    text = str(exc) or exc.__class__.__name__
    lowered = text.lower()
    if "password authentication failed" in lowered:
        return f"{text} — проверьте имя пользователя и пароль PostgreSQL"
    if "connection refused" in lowered or "connect call failed" in lowered:
        return f"{text} — PostgreSQL не отвечает на этом адресе и порту"
    if "does not exist" in lowered and "database" in lowered:
        return f"{text} — база будет создана при установке"
    if "timeout" in lowered:
        return f"{text} — сервер не ответил вовремя"
    return text
