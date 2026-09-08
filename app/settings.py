"""Настройки приложения.

Живут в JSON-файле, а не в БД: часть из них (адрес и пароль PostgreSQL)
нужна до того, как база вообще появится. Файл создаётся при первом запуске
со значениями по умолчанию и правится либо руками, либо из веб-интерфейса.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
RUNTIME_DIR = ROOT / "runtime"
WEB_DIR = ROOT / "web"
DB_SQL_DIR = ROOT / "db"
CONFIG_FILE = CONFIG_DIR / "app.json"


@dataclass(slots=True)
class DatabaseSettings:
    """Куда пишем историю. Приложение само создаст БД, схему и роль."""

    host: str = "127.0.0.1"
    port: int = 5432
    name: str = "plc"
    user: str = "plc"
    password: str = ""
    # Учётка с правом CREATE DATABASE / CREATE ROLE — нужна один раз, при
    # первичной настройке. Хранится, чтобы «Пересоздать схему» работало позже.
    admin_user: str = "postgres"
    admin_password: str = ""
    admin_db: str = "postgres"
    # Роль только на чтение — под ней в базу ходит Grafana
    readonly_user: str = "grafana_ro"
    readonly_password: str = ""
    # Пул и буфер записи
    min_pool_size: int = 1
    max_pool_size: int = 6
    batch_max_rows: int = 500
    batch_max_delay_ms: int = 1000
    buffer_max_rows: int = 200_000
    # Срок хранения сырья, дней (0 = не удалять). Работает и без TimescaleDB.
    retention_days: int = 90
    configured: bool = False

    @property
    def dsn(self) -> str:
        return _dsn(self.user, self.password, self.host, self.port, self.name)

    @property
    def admin_dsn(self) -> str:
        return _dsn(self.admin_user, self.admin_password, self.host, self.port, self.admin_db)

    def safe_dsn(self) -> str:
        return f"postgresql://{self.user}:***@{self.host}:{self.port}/{self.name}"


@dataclass(slots=True)
class GrafanaSettings:
    """Локальная Grafana, которой управляет приложение."""

    enabled: bool = True
    # portable-сборка распаковывается в runtime/grafana/<version>
    version: str = "11.6.0"
    port: int = 3000
    admin_user: str = "admin"
    admin_password: str = ""
    # Анонимный просмотр — чтобы дашборды открывались внутри нашего интерфейса
    # без второго логина. Grafana слушает только localhost.
    anonymous_view: bool = True
    # Внешняя Grafana вместо встроенной (пусто = использовать встроенную)
    external_url: str = ""
    external_token: str = ""

    @property
    def url(self) -> str:
        return self.external_url.rstrip("/") or f"http://127.0.0.1:{self.port}"

    @property
    def managed(self) -> bool:
        return not self.external_url


@dataclass(slots=True)
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 8000
    open_browser: bool = True


@dataclass(slots=True)
class Settings:
    server: ServerSettings = field(default_factory=ServerSettings)
    database: DatabaseSettings = field(default_factory=DatabaseSettings)
    grafana: GrafanaSettings = field(default_factory=GrafanaSettings)

    # -- файл ------------------------------------------------------------
    _lock = threading.Lock()

    @classmethod
    def load(cls) -> "Settings":
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        raw: dict[str, Any] = {}
        if CONFIG_FILE.is_file():
            try:
                raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                raw = {}
        settings = cls(
            server=_build(ServerSettings, raw.get("server")),
            database=_build(DatabaseSettings, raw.get("database")),
            grafana=_build(GrafanaSettings, raw.get("grafana")),
        )
        settings.apply_env()
        # Пароли, которые пользователь никогда не увидит и не должен вводить
        changed = False
        for holder, attr in ((settings.database, "readonly_password"),
                             (settings.grafana, "admin_password")):
            if not getattr(holder, attr):
                setattr(holder, attr, secrets.token_urlsafe(18))
                changed = True
        if changed or not CONFIG_FILE.is_file():
            settings.save()
        return settings

    def apply_env(self) -> None:
        """Переменные окружения перекрывают файл — удобно для docker-режима."""
        env_map = {
            "PLC_DB_HOST": (self.database, "host", str),
            "PLC_DB_PORT": (self.database, "port", int),
            "PLC_DB_NAME": (self.database, "name", str),
            "PLC_DB_USER": (self.database, "user", str),
            "PLC_DB_PASSWORD": (self.database, "password", str),
            "PLC_DB_ADMIN_USER": (self.database, "admin_user", str),
            "PLC_DB_ADMIN_PASSWORD": (self.database, "admin_password", str),
            "PLC_WEB_HOST": (self.server, "host", str),
            "PLC_WEB_PORT": (self.server, "port", int),
            "PLC_GRAFANA_URL": (self.grafana, "external_url", str),
            "PLC_GRAFANA_TOKEN": (self.grafana, "external_token", str),
            "PLC_GRAFANA_PORT": (self.grafana, "port", int),
        }
        for env_name, (holder, attr, cast) in env_map.items():
            value = os.environ.get(env_name)
            if value:
                setattr(holder, attr, cast(value))

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "server": asdict(self.server),
            "database": asdict(self.database),
            "grafana": asdict(self.grafana),
        }
        with Settings._lock:
            tmp = CONFIG_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(CONFIG_FILE)

    def public_dict(self) -> dict[str, Any]:
        """То, что можно отдать в браузер: без паролей."""
        db = asdict(self.database)
        for key in ("password", "admin_password", "readonly_password"):
            db[key] = "••••" if db[key] else ""
        gf = asdict(self.grafana)
        gf["admin_password"] = "••••" if gf["admin_password"] else ""
        gf["external_token"] = "••••" if gf["external_token"] else ""
        gf["url"] = self.grafana.url
        gf["managed"] = self.grafana.managed
        return {"server": asdict(self.server), "database": db, "grafana": gf}


def _dsn(user: str, password: str, host: str, port: int, name: str) -> str:
    from urllib.parse import quote

    return f"postgresql://{quote(user)}:{quote(password)}@{host}:{port}/{name}"


def _build(cls, raw: Any):
    if not isinstance(raw, dict):
        return cls()
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in raw.items() if k in known})
