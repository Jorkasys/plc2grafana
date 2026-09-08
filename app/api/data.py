"""Чтение значений: история, текущие значения и живой поток по WebSocket."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from ..context import ctx
from ..store import latest_values, series

log = logging.getLogger("app.api.data")

router = APIRouter(tags=["data"])

# Как часто отдаём накопленные значения в браузер: 10 раз в секунду достаточно
# для «живого» ощущения и не нагружает вкладку сотнями сообщений.
LIVE_FLUSH_S = 0.1
# Держим соединение живым, когда данные не меняются (мёртвая зона)
PING_INTERVAL_S = 15.0


@router.get("/latest")
async def get_latest(device_id: int | None = None) -> list[dict]:
    if not ctx.db.connected:
        raise HTTPException(status_code=409, detail="нет соединения с базой данных")
    rows = await latest_values(ctx.db, device_id)
    # Значения из памяти свежее, чем то, что уже успело попасть в БД
    for row in rows:
        live = ctx.runtime.bus.latest(row["tag_id"])
        if live and (row["ts"] is None or live["ts"] > row["ts"]):
            row.update(ts=live["ts"], value=live["value"],
                       value_text=live["text"], quality=live["quality"],
                       quality_name=live["quality_name"], age_s=0.0)
    return rows


@router.get("/series")
async def get_series(
    tag_ids: str = Query(..., description="идентификаторы тегов через запятую"),
    minutes: float = Query(30, gt=0, le=60 * 24 * 400),
    to: str | None = None,
    max_points: int = Query(1500, ge=10, le=20000),
) -> dict:
    if not ctx.db.connected:
        raise HTTPException(status_code=409, detail="нет соединения с базой данных")
    try:
        ids = [int(part) for part in tag_ids.split(",") if part.strip()]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="tag_ids: ожидались числа") from exc
    if not ids:
        return {"series": {}}
    if len(ids) > 60:
        raise HTTPException(status_code=400, detail="слишком много тегов за раз (максимум 60)")

    end = _parse_ts(to) if to else datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)
    data = await series(ctx.db, ids, start, end, max_points)
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "series": {str(k): v for k, v in data.items()},
    }


@router.websocket("/live")
async def live(websocket: WebSocket) -> None:
    """Поток текущих значений. Клиент только слушает."""
    await websocket.accept()
    bus = ctx.runtime.bus
    queue = bus.subscribe()
    try:
        await websocket.send_text(json.dumps(
            {"type": "snapshot", "items": bus.snapshot()}, ensure_ascii=False))
        pending: list[dict] = []
        last_sent = time.monotonic()
        while True:
            try:
                pending.append(await asyncio.wait_for(queue.get(), timeout=LIVE_FLUSH_S))
                # Добираем всё, что уже пришло, одним пакетом
                while not queue.empty() and len(pending) < 2000:
                    pending.append(queue.get_nowait())
            except asyncio.TimeoutError:
                pass
            if pending:
                await websocket.send_text(json.dumps(
                    {"type": "values", "items": pending}, ensure_ascii=False))
                pending = []
                last_sent = time.monotonic()
            elif time.monotonic() - last_sent > PING_INTERVAL_S:
                # Тишина бывает нормальной (мёртвая зона), но соединение
                # должно оставаться живым и через прокси
                await websocket.send_text('{"type":"ping"}')
                last_sent = time.monotonic()
    except (WebSocketDisconnect, RuntimeError, ConnectionResetError):
        pass
    except Exception as exc:  # noqa: BLE001
        log.debug("live: %s", exc)
    finally:
        bus.unsubscribe(queue)


def _parse_ts(text: str) -> datetime:
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"некорректная дата: {text}") from exc
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
