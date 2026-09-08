"""Состояние системы, первичная настройка БД, настройки приложения."""

from __future__ import annotations

import logging
import platform
import sys
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..context import ctx
from ..db import DatabaseError
from ..store import storage_stats

log = logging.getLogger("app.api.system")

router = APIRouter(tags=["system"])
STARTED_AT = time.time()


class DbSetupRequest(BaseModel):
    host: str = "127.0.0.1"
    port: int = 5432
    name: str = "plc"
    admin_user: str = "postgres"
    admin_password: str = ""
    admin_db: str = "postgres"
    user: str = "plc"
    password: str = ""
    retention_days: int = Field(default=90, ge=0, le=36500)


class SettingsPatch(BaseModel):
    retention_days: int | None = Field(default=None, ge=0, le=36500)
    grafana_port: int | None = Field(default=None, ge=1, le=65535)
    grafana_enabled: bool | None = None
    grafana_external_url: str | None = None
    grafana_external_token: str | None = None
    open_browser: bool | None = None


@router.get("/status")
async def status() -> dict:
    """Одним запросом — всё, что показывает шапка интерфейса."""
    db_info = ctx.db.info
    return {
        "configured": ctx.settings.database.configured,
        "ready": ctx.ready,
        "startup_error": ctx.startup_error,
        "uptime_s": round(time.time() - STARTED_AT, 1),
        "database": {
            "connected": ctx.db.connected,
            "error": ctx.db.last_error,
            "server_version": db_info.server_version if db_info else None,
            "timescaledb": db_info.timescaledb if db_info else False,
            "dsn": ctx.settings.database.safe_dsn(),
        },
        "runtime": ctx.runtime.status(),
        "grafana": ctx.grafana.status(),
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.release()}",
    }


@router.get("/settings")
async def get_settings() -> dict:
    return ctx.settings.public_dict()


@router.patch("/settings")
async def patch_settings(payload: SettingsPatch) -> dict:
    settings = ctx.settings
    if payload.retention_days is not None:
        settings.database.retention_days = payload.retention_days
    if payload.grafana_port is not None:
        settings.grafana.port = payload.grafana_port
    if payload.grafana_enabled is not None:
        settings.grafana.enabled = payload.grafana_enabled
    if payload.grafana_external_url is not None:
        settings.grafana.external_url = payload.grafana_external_url.strip()
    if payload.grafana_external_token is not None:
        settings.grafana.external_token = payload.grafana_external_token.strip()
    if payload.open_browser is not None:
        settings.server.open_browser = payload.open_browser
    settings.save()
    if ctx.db.connected:
        ctx.grafana.write_provisioning()
        if ctx.db.info:
            ctx.grafana.set_timescale(ctx.db.info.timescaledb)
    return settings.public_dict()


@router.post("/setup/test-db")
async def test_db(payload: DbSetupRequest) -> dict:
    """Проверить, что до PostgreSQL вообще получается достучаться."""
    import asyncpg

    from urllib.parse import quote

    dsn = (f"postgresql://{quote(payload.admin_user)}:{quote(payload.admin_password)}"
           f"@{payload.host}:{payload.port}/{payload.admin_db}")
    try:
        conn = await asyncpg.connect(dsn=dsn, timeout=8)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": _explain(exc)}
    try:
        version = await conn.fetchval("SELECT version()")
        can_create_db = await conn.fetchval(
            "SELECT rolcreatedb OR rolsuper FROM pg_roles WHERE rolname = current_user")
        has_timescale = await conn.fetchval(
            "SELECT count(*) > 0 FROM pg_available_extensions WHERE name = 'timescaledb'")
        db_exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", payload.name)
    finally:
        await conn.close()
    return {
        "ok": True,
        "version": version,
        "can_create_db": bool(can_create_db),
        "timescaledb_available": bool(has_timescale),
        "database_exists": bool(db_exists),
    }


@router.post("/setup/init-db")
async def init_db(payload: DbSetupRequest) -> dict:
    """Создать роли, базу и схему, затем запустить опрос."""
    cfg = ctx.settings.database
    cfg.host = payload.host
    cfg.port = payload.port
    cfg.name = payload.name
    cfg.admin_user = payload.admin_user
    cfg.admin_password = payload.admin_password
    cfg.admin_db = payload.admin_db
    cfg.user = payload.user
    cfg.retention_days = payload.retention_days
    if payload.password:
        cfg.password = payload.password

    try:
        steps = await ctx.db.bootstrap()
    except DatabaseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await ctx.after_db_ready()
    return {"ok": True, "steps": steps, "timescaledb":
            bool(ctx.db.info and ctx.db.info.timescaledb)}


@router.post("/setup/reconnect")
async def reconnect() -> dict:
    try:
        await ctx.db.connect()
    except DatabaseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await ctx.after_db_ready()
    return {"ok": True}


@router.get("/system/stats")
async def system_stats() -> dict:
    if not ctx.db.connected:
        raise HTTPException(status_code=409, detail="нет соединения с базой данных")
    stats = await storage_stats(ctx.db)
    stats["retention_days"] = ctx.settings.database.retention_days
    return stats


@router.post("/runtime/reload")
async def runtime_reload() -> dict:
    if not ctx.db.connected:
        raise HTTPException(status_code=409, detail="нет соединения с базой данных")
    result = await ctx.runtime.reload()
    return result


@router.get("/runtime/plan")
async def runtime_plan() -> dict:
    """Во что превратились теги: реальные Modbus-запросы."""
    if not ctx.db.connected:
        raise HTTPException(status_code=409, detail="нет соединения с базой данных")
    return {"active": ctx.runtime.plan(), "preview": await ctx.runtime.preview_plan()}


def _explain(exc: Exception) -> str:
    text = str(exc) or exc.__class__.__name__
    low = text.lower()
    if "password authentication failed" in low:
        return "неверные имя пользователя или пароль PostgreSQL"
    if "connection refused" in low or "connect call failed" in low:
        return "PostgreSQL не отвечает: проверьте, что сервер запущен, адрес и порт"
    if "timeout" in low:
        return "сервер PostgreSQL не ответил вовремя"
    if 'role "' in low and "does not exist" in low:
        return text
    return text
