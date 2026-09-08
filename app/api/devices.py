"""Устройства, разведка карты регистров и теги."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .. import discovery
from ..context import ctx
from ..registry import RegistryError

log = logging.getLogger("app.api.devices")

router = APIRouter(tags=["devices"])


def _require_db() -> None:
    if not ctx.db.connected:
        raise HTTPException(status_code=409, detail="нет соединения с базой данных")


class ConnectionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    host: str = ""
    port: int = Field(default=502, ge=1, le=65535)
    unit_id: int = Field(default=1, ge=0, le=247)
    timeout_s: float = Field(default=3.0, gt=0, le=30)
    transport: str = "tcp"
    serial_port: str | None = None
    baudrate: int = 9600
    parity: str = "N"


class ScanRequest(ConnectionRequest):
    # Имя поля отличается от ключа JSON: `register` занято методом метакласса
    # pydantic (ABCMeta.register) и вызывает предупреждение при загрузке.
    register_type: str = Field(default="holding", alias="register")
    start: int = Field(default=0, ge=0, le=65535)
    count: int = Field(default=64, ge=1, le=500)
    samples: int = Field(default=4, ge=1, le=10)
    delay_s: float = Field(default=0.8, ge=0.1, le=10)


class TagPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    register_type: str = Field(default="holding", alias="register")
    address: int
    datatype: str = "uint16"
    description: str | None = None
    unit: str | None = None
    bit: int | None = None
    length: int | None = None
    scale: float = 1.0
    offset: float = 0.0
    min_value: float | None = None
    max_value: float | None = None
    word_order: str = "big"
    byte_order: str = "big"
    poll_interval_ms: int = 1000
    deadband: float = 0.0
    deadband_mode: str = "absolute"
    heartbeat_s: float = 60.0
    enabled: bool = True
    node_id: int | None = None
    group_name: str | None = None
    role: str | None = None
    labels: dict[str, Any] = Field(default_factory=dict)


class TagsPayload(BaseModel):
    tags: list[TagPayload]


# ---------------------------------------------------------------------------
#  Разведка
# ---------------------------------------------------------------------------
@router.post("/discovery/test")
async def discovery_test(payload: ConnectionRequest) -> dict:
    """Проверка связи: отвечает ли устройство и какие регистры отдаёт."""
    if payload.transport in {"tcp", "udp"} and not payload.host:
        raise HTTPException(status_code=400, detail="укажите IP-адрес устройства")
    return await discovery.test_connection(
        payload.host, payload.port, payload.unit_id, payload.timeout_s,
        payload.transport, serial_port=payload.serial_port,
        baudrate=payload.baudrate, parity=payload.parity)


@router.post("/discovery/scan")
async def discovery_scan(payload: ScanRequest) -> dict:
    """Прочитать окно регистров и предложить готовые теги."""
    try:
        result = await discovery.scan(
            payload.host, payload.port, payload.unit_id, payload.register_type,
            payload.start, payload.count, samples=payload.samples,
            delay_s=payload.delay_s, timeout_s=payload.timeout_s,
            transport=payload.transport, serial_port=payload.serial_port,
            baudrate=payload.baudrate, parity=payload.parity)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if result.error and not result.samples:
        raise HTTPException(status_code=502, detail=result.error)

    candidates = discovery.analyze(result)
    raw = [
        {
            "address": result.start + i,
            "values": [None if s[i] is None else int(s[i]) if not isinstance(s[i], bool)
                       else int(bool(s[i])) for s in result.samples],
        }
        for i in range(result.count)
    ]
    return {
        "register": result.register,
        "start": result.start,
        "count": result.count,
        "samples": len(result.samples),
        "unreadable": result.unreadable,
        "error": result.error,
        "candidates": candidates,
        "raw": raw,
    }


# ---------------------------------------------------------------------------
#  Устройства
# ---------------------------------------------------------------------------
@router.get("/devices")
async def list_devices() -> list[dict]:
    _require_db()
    devices = await ctx.registry.list_devices()
    status = {d["device"]: d for d in ctx.runtime.status()["devices"]}
    for device in devices:
        device["runtime"] = status.get(device["name"])
    return devices


@router.post("/devices", status_code=201)
async def create_device(payload: dict) -> dict:
    _require_db()
    try:
        device = await ctx.registry.create_device(payload)
    except RegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    tags = payload.get("tags")
    if tags:
        try:
            await ctx.registry.replace_tags(device["device_id"], tags)
        except RegistryError as exc:
            await ctx.registry.delete_device(device["device_id"])
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    await ctx.runtime.reload()
    return device


@router.get("/devices/{device_id}")
async def get_device(device_id: int) -> dict:
    _require_db()
    device = await ctx.registry.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="устройство не найдено")
    return device


@router.patch("/devices/{device_id}")
async def update_device(device_id: int, payload: dict) -> dict:
    _require_db()
    try:
        device = await ctx.registry.update_device(device_id, payload)
    except RegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await ctx.runtime.reload()
    return device


@router.delete("/devices/{device_id}")
async def delete_device(device_id: int) -> dict:
    _require_db()
    try:
        await ctx.registry.delete_device(device_id)
    except RegistryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await ctx.runtime.reload()
    return {"ok": True}


# ---------------------------------------------------------------------------
#  Теги
# ---------------------------------------------------------------------------
@router.get("/tags")
async def list_tags(device_id: int | None = None, node_id: int | None = None) -> list[dict]:
    _require_db()
    return await ctx.registry.list_tags(device_id, node_id)


@router.get("/devices/{device_id}/tags")
async def device_tags(device_id: int) -> list[dict]:
    _require_db()
    return await ctx.registry.list_tags(device_id)


@router.put("/devices/{device_id}/tags")
async def replace_tags(device_id: int, payload: TagsPayload) -> dict:
    _require_db()
    try:
        count = await ctx.registry.replace_tags(
            device_id, [t.model_dump(by_alias=True) for t in payload.tags])
    except RegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await ctx.runtime.reload()
    return {"ok": True, "tags": count}


@router.post("/devices/{device_id}/tags")
async def add_tags(device_id: int, payload: TagsPayload) -> dict:
    _require_db()
    try:
        count = await ctx.registry.add_tags(
            device_id, [t.model_dump(by_alias=True) for t in payload.tags])
    except RegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await ctx.runtime.reload()
    return {"ok": True, "tags": count}


@router.patch("/tags/{tag_id}")
async def update_tag(tag_id: int, payload: dict) -> dict:
    _require_db()
    try:
        tag = await ctx.registry.update_tag(tag_id, payload)
    except RegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await ctx.runtime.reload()
    return tag


@router.delete("/tags/{tag_id}")
async def delete_tag(tag_id: int) -> dict:
    _require_db()
    try:
        await ctx.registry.delete_tag(tag_id)
    except RegistryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await ctx.runtime.reload()
    return {"ok": True}
