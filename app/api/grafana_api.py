"""Управление локальной Grafana и генерация дашбордов."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException

from ..context import ctx
from ..grafana import dashboards as dash
from ..grafana.manager import DASHBOARD_DIR, GrafanaError

log = logging.getLogger("app.api.grafana")

router = APIRouter(prefix="/grafana", tags=["grafana"])


@router.get("/status")
async def status() -> dict:
    state = ctx.grafana.status()
    state["healthy"] = await ctx.grafana.health()
    return state


@router.post("/install")
async def install(force: bool = False) -> dict:
    """Скачать portable-сборку Grafana. Возвращает управление сразу."""
    if ctx.grafana.download.active:
        return {"started": False, "reason": "установка уже идёт",
                "download": ctx.grafana.status()["download"]}

    async def job() -> None:
        try:
            await ctx.grafana.install(force=force)
        except GrafanaError as exc:
            log.error("установка Grafana: %s", exc)

    asyncio.create_task(job(), name="grafana-install")
    return {"started": True}


@router.post("/start")
async def start() -> dict:
    try:
        state = await ctx.grafana.start()
    except GrafanaError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Свежие дашборды к моменту первого открытия
    try:
        await _generate()
    except HTTPException:
        pass
    return state


@router.post("/stop")
async def stop() -> dict:
    return await ctx.grafana.stop()


@router.post("/dashboards")
async def generate() -> dict:
    return await _generate()


@router.get("/dashboards")
async def list_dashboards() -> dict:
    client = ctx.grafana_client()
    items = await client.dashboards()
    for item in items:
        # Браузер сам подставит хост: сервер знает Grafana как 127.0.0.1
        # или http://grafana:3000, а пользователь может сидеть на другом ПК
        item["embed_url"] = client.embed_url(item["path"])
        item["embed_path"] = item["embed_url"][len(client.url):]
    files = sorted(p.name for p in DASHBOARD_DIR.glob("plc-*.json")) \
        if DASHBOARD_DIR.is_dir() else []
    return {"dashboards": items, "files": files,
            "browser_url": ctx.settings.grafana.browser_url}


async def _generate() -> dict:
    if not ctx.db.connected:
        raise HTTPException(status_code=409, detail="нет соединения с базой данных")
    devices = await ctx.registry.list_devices()
    tags = await ctx.registry.list_tags()
    nodes = await ctx.registry.list_nodes()
    if not tags:
        raise HTTPException(status_code=400,
                            detail="нет ни одного тега — сначала добавьте устройство")
    ctx.grafana.write_provisioning()
    if ctx.db.info:
        ctx.grafana.set_timescale(ctx.db.info.timescaledb)
    written = dash.generate_all(DASHBOARD_DIR, devices, tags, nodes)
    return {"ok": True, "dashboards": written, "dir": str(DASHBOARD_DIR)}
