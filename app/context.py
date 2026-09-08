"""Общий контекст приложения — то, что разделяют все обработчики HTTP."""

from __future__ import annotations

import logging

from .db import Database, DatabaseError
from .grafana.client import GrafanaClient
from .grafana.manager import GrafanaManager
from .registry import Registry
from .runtime import Runtime
from .settings import Settings

log = logging.getLogger("app")


class AppContext:
    def __init__(self) -> None:
        self.settings = Settings.load()
        self.db = Database(self.settings)
        self.registry = Registry(self.db)
        self.runtime = Runtime(self.settings, self.db, self.registry)
        self.grafana = GrafanaManager(self.settings)
        self.ready = False
        self.startup_error: str | None = None

    # ------------------------------------------------------------------ старт
    async def startup(self) -> None:
        """Поднять то, что уже настроено. Отсутствие БД — не повод падать:
        приложение откроет мастер первичной настройки."""
        if not self.settings.database.configured:
            log.info("база данных ещё не настроена — открой мастер в браузере")
            return
        try:
            await self.db.connect()
        except DatabaseError as exc:
            self.startup_error = str(exc)
            log.warning("нет соединения с БД: %s", exc)
            return
        await self.after_db_ready()

    async def after_db_ready(self) -> None:
        self.ready = True
        self.startup_error = None
        if self.db.info is not None:
            self.grafana.write_provisioning()
            self.grafana.set_timescale(self.db.info.timescaledb)
        try:
            await self.runtime.start()
        except Exception as exc:  # noqa: BLE001
            log.exception("не удалось запустить опрос: %s", exc)
            self.runtime.last_error = str(exc)

    async def shutdown(self) -> None:
        await self.runtime.stop()
        await self.grafana.stop()
        await self.db.close()

    # --------------------------------------------------------------- Grafana
    def grafana_client(self) -> GrafanaClient:
        cfg = self.settings.grafana
        if cfg.managed:
            return GrafanaClient(cfg.url, user=cfg.admin_user,
                                 password=cfg.admin_password)
        return GrafanaClient(cfg.url, token=cfg.external_token)


ctx = AppContext()
