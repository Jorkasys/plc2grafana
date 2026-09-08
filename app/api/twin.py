"""Цифровой двойник: дерево площадка → участок → линия → машина.

Пока это структура и живая сводка по каждому узлу. На неё же опирается
генерация дашбордов: линия получает свой дашборд, машина — свою группу
панелей. Дальше сюда логично лягут мнемосхемы и расчёт OEE.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from ..context import ctx
from ..registry import RegistryError
from ..store import latest_values

log = logging.getLogger("app.api.twin")

router = APIRouter(prefix="/twin", tags=["twin"])

KIND_ORDER = {"site": 0, "area": 1, "line": 2, "machine": 3}


def _require_db() -> None:
    if not ctx.db.connected:
        raise HTTPException(status_code=409, detail="нет соединения с базой данных")


@router.get("/nodes")
async def list_nodes() -> list[dict]:
    _require_db()
    return await ctx.registry.list_nodes()


@router.post("/nodes", status_code=201)
async def create_node(payload: dict) -> dict:
    _require_db()
    try:
        return await ctx.registry.create_node(payload)
    except RegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/nodes/{node_id}")
async def update_node(node_id: int, payload: dict) -> dict:
    _require_db()
    try:
        return await ctx.registry.update_node(node_id, payload)
    except RegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/nodes/{node_id}")
async def delete_node(node_id: int) -> dict:
    _require_db()
    try:
        await ctx.registry.delete_node(node_id)
    except RegistryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/nodes/{node_id}/tags")
async def assign_tags(node_id: int, payload: dict) -> dict:
    """Привязать теги к узлу двойника (или отвязать, если node_id = 0)."""
    _require_db()
    tag_ids = [int(t) for t in payload.get("tag_ids") or []]
    if not tag_ids:
        raise HTTPException(status_code=400, detail="не переданы теги")
    target = node_id or None
    async with ctx.db.acquire() as conn:
        await conn.execute(
            "UPDATE tag SET node_id = $1, updated_at = now() WHERE tag_id = ANY($2::int[])",
            target, tag_ids)
    return {"ok": True, "tags": len(tag_ids), "node_id": target}


@router.get("/overview")
async def overview() -> dict[str, Any]:
    """Дерево с живой сводкой: сколько тегов, есть ли авария, ключевые значения."""
    _require_db()
    nodes = await ctx.registry.list_nodes()
    values = await latest_values(ctx.db)
    devices = await ctx.registry.list_devices()

    by_node: dict[int | None, list[dict]] = {}
    for row in values:
        live = ctx.runtime.bus.latest(row["tag_id"])
        if live:
            row = {**row, "value": live["value"], "quality": live["quality"],
                   "value_text": live["text"], "ts": live["ts"], "age_s": 0.0}
        by_node.setdefault(row["node_id"], []).append(row)

    children: dict[int | None, list[dict]] = {}
    for node in nodes:
        children.setdefault(node["parent_id"], []).append(node)

    def build(node: dict) -> dict:
        own = by_node.get(node["node_id"], [])
        kids = [build(c) for c in sorted(
            children.get(node["node_id"], []),
            key=lambda n: (KIND_ORDER.get(n["kind"], 9), n["sort_order"], n["name"]))]
        tags = list(own)
        for kid in kids:
            tags.extend(kid["_all_tags"])
        alarms = [t for t in tags if t.get("role") == "alarm" and (t.get("value") or 0) > 0]
        bad = [t for t in tags if (t.get("quality") or 0) != 0]
        return {
            "node_id": node["node_id"],
            "parent_id": node["parent_id"],
            "kind": node["kind"],
            "name": node["name"],
            "description": node["description"],
            "meta": node["meta"],
            "children": kids,
            "tag_count": len(tags),
            "own_tags": [_slim(t) for t in sorted(own, key=lambda t: t["name"])][:12],
            "alarm_count": len(alarms),
            "alarms": [t["name"] for t in alarms][:10],
            "bad_quality": len(bad),
            "state": ("alarm" if alarms else
                      "degraded" if bad and len(bad) == len(tags) else
                      "warn" if bad else
                      "ok" if tags else "empty"),
            "_all_tags": tags,
        }

    roots = [build(n) for n in sorted(
        children.get(None, []),
        key=lambda n: (KIND_ORDER.get(n["kind"], 9), n["sort_order"], n["name"]))]
    _strip(roots)

    unassigned = by_node.get(None, [])
    return {
        "tree": roots,
        "unassigned": {
            "tag_count": len(unassigned),
            "tags": [_slim(t) for t in sorted(unassigned, key=lambda t: t["name"])][:50],
        },
        "devices": [{"device_id": d["device_id"], "name": d["name"],
                     "node_id": d["node_id"], "enabled": d["enabled"]} for d in devices],
    }


def _slim(tag: dict) -> dict:
    return {
        "tag_id": tag["tag_id"],
        "name": tag["name"],
        "description": tag.get("description"),
        "unit": tag.get("unit"),
        "value": tag.get("value"),
        "value_text": tag.get("value_text"),
        "quality": tag.get("quality"),
        "role": tag.get("role"),
        "datatype": tag.get("datatype"),
        "age_s": tag.get("age_s"),
    }


def _strip(nodes: list[dict]) -> None:
    """Убрать служебное поле перед отправкой в браузер."""
    for node in nodes:
        node.pop("_all_tags", None)
        _strip(node["children"])
