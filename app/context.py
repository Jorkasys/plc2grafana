"""Общий контекст приложения — то, что разделяют все обработчики HTTP."""

from __future__ import annotations

import asyncio
import logging
import os

from .db import Database, DatabaseError
from .grafana.client import GrafanaClient
from .grafana.manager import GrafanaManager
from .registry import Registry
from .runtime import Runtime
from .settings import Settings

log = logging.getLogger("app")

# Пауза между попытками достучаться до БД, если при старте её ещё нет
# (служба PostgreSQL после перезагрузки поднимается не мгновенно).
DB_RETRY_S = 10


class AppContext:
    def __init__(self) -> None:
        self.settings = Settings.load()
        self.db = Database(self.settings)
        self.registry = Registry(self.db)
        self.runtime = Runtime(self.settings, self.db, self.registry)
        self.grafana = GrafanaManager(self.settings)
        self.ready = False
        self.startup_error: str | None = None
        self._background: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ старт
    async def startup(self) -> None:
        """Поднять то, что уже настроено. Отсутствие БД — не повод падать:
        приложение откроет мастер первичной настройки или будет ждать базу."""
        cfg = self.settings.database
        # Датасорс зависит только от настроек, не от состояния БД. Пишем его
        # сразу: Grafana (особенно соседний docker-контейнер) читает
        # provisioning при своём старте и может опередить настройку базы.
        try:
            self.grafana.write_provisioning()
        except OSError as exc:
            log.warning("не удалось записать provisioning Grafana: %s", exc)
        if not cfg.configured:
            if _auto_setup_requested() and cfg.admin_password:
                self._spawn(self._auto_setup(), "db-auto-setup")
            else:
                log.info("база данных ещё не настроена — открой мастер в браузере")
            return
        try:
            await self.db.connect()
        except DatabaseError as exc:
            self.startup_error = str(exc)
            log.warning("нет соединения с БД: %s — повтор каждые %d с", exc, DB_RETRY_S)
            self._spawn(self._reconnect_loop(), "db-reconnect")
            return
        await self.after_db_ready()

    async def after_db_ready(self) -> None:
        self.ready = True
        self.startup_error = None
        try:
            await self.db.migrate()
        except DatabaseError as exc:
            log.warning("обновление схемы пропущено: %s", exc)
        if self.db.info is not None:
            self.grafana.write_provisioning()
            self.grafana.set_timescale(self.db.info.timescaledb)
        try:
            await self.runtime.start()
        except Exception as exc:  # noqa: BLE001
            log.exception("не удалось запустить опрос: %s", exc)
            self.runtime.last_error = str(exc)
        # Grafana поднимается до минуты — не держим ради неё старт приложения
        self._spawn(self.grafana.autostart(), "grafana-autostart")

    async def shutdown(self) -> None:
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        await self.runtime.stop()
        await self.grafana.stop()
        await self.db.close()

    # ------------------------------------------------------- фоновые задачи
    def _spawn(self, coro, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _reconnect_loop(self) -> None:
        while not self.db.connected:
            await asyncio.sleep(DB_RETRY_S)
            try:
                await self.db.connect()
            except DatabaseError as exc:
                self.startup_error = str(exc)
                log.debug("БД всё ещё недоступна: %s", exc)
                continue
            log.info("соединение с БД установлено")
            await self.after_db_ready()

    async def _auto_setup(self) -> None:
        """Первичная настройка без браузера — для docker и установочных скриптов.

        Включается переменной PLC_DB_AUTO_SETUP=1, учётка администратора
        PostgreSQL берётся из PLC_DB_ADMIN_USER / PLC_DB_ADMIN_PASSWORD.
        Сервер БД может ещё подниматься — пробуем, пока не получится.
        """
        while True:
            try:
                steps = await self.db.bootstrap()
            except DatabaseError as exc:
                self.startup_error = str(exc)
                log.warning("автонастройка БД: %s — повтор через %d с", exc, DB_RETRY_S)
                await asyncio.sleep(DB_RETRY_S)
                continue
            for step in steps:
                log.info("автонастройка БД: %s", step)
            await self.after_db_ready()
            return

    # --------------------------------------------------------------- Grafana
    def grafana_client(self) -> GrafanaClient:
        cfg = self.settings.grafana
        if cfg.managed:
            return GrafanaClient(self.grafana.api_url, user=cfg.admin_user,
                                 password=cfg.admin_password)
        return GrafanaClient(cfg.url, token=cfg.external_token)


def _auto_setup_requested() -> bool:
    return os.environ.get("PLC_DB_AUTO_SETUP", "").strip().lower() in {"1", "true", "yes", "on"}


ctx = AppContext()
