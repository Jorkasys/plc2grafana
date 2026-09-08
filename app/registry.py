"""Реестр: устройства, теги и дерево цифрового двойника.

Единственный источник правды — база данных, а не YAML. Веб-интерфейс правит
записи здесь, а менеджер опроса собирает из них датаклассы ``modbus_logger``.
"""

from __future__ import annotations

import json
import logging
from typing import Iterable

from modbus_logger.config import BlockConfig, Device, Tag

log = logging.getLogger("app.registry")

DEVICE_COLUMNS = (
    "device_id, name, description, node_id, transport, host, port, unit_id, "
    "serial_port, baudrate, bytesize, parity, stopbits, timeout_s, retries, "
    "reconnect_delay_s, reconnect_delay_max_s, inter_request_delay_ms, "
    "block_max_registers, block_max_gap, enabled, created_at, updated_at"
)

TAG_COLUMNS = (
    "tag_id, device_id, device, name, description, unit, datatype, register, "
    "address, bit, length, scale, \"offset\", min_value, max_value, word_order, "
    "byte_order, poll_interval_ms, deadband, deadband_mode, heartbeat_s, "
    "labels, enabled, node_id, group_name, role"
)

# Поля тега, которые разрешено менять через API
TAG_EDITABLE = {
    "name", "description", "unit", "datatype", "register", "address", "bit",
    "length", "scale", "offset", "min_value", "max_value", "word_order",
    "byte_order", "poll_interval_ms", "deadband", "deadband_mode",
    "heartbeat_s", "enabled", "node_id", "group_name", "role", "labels",
}

DEVICE_EDITABLE = {
    "name", "description", "node_id", "transport", "host", "port", "unit_id",
    "serial_port", "baudrate", "bytesize", "parity", "stopbits", "timeout_s",
    "retries", "reconnect_delay_s", "reconnect_delay_max_s",
    "inter_request_delay_ms", "block_max_registers", "block_max_gap", "enabled",
}


class RegistryError(ValueError):
    """Некорректные данные от пользователя."""


class Registry:
    def __init__(self, db) -> None:
        self.db = db

    # ------------------------------------------------------------ устройства
    async def list_devices(self) -> list[dict]:
        async with self.db.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {DEVICE_COLUMNS}, "
                f"(SELECT count(*) FROM tag t WHERE t.device_id = d.device_id) AS tag_count "
                f"FROM device d ORDER BY name"
            )
        return [dict(r) for r in rows]

    async def get_device(self, device_id: int) -> dict | None:
        async with self.db.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {DEVICE_COLUMNS} FROM device WHERE device_id = $1", device_id)
        return dict(row) if row else None

    async def create_device(self, data: dict) -> dict:
        payload = _clean(data, DEVICE_EDITABLE, DEVICE_NULLABLE)
        name = str(payload.get("name") or "").strip()
        if not name:
            raise RegistryError("укажите имя устройства")
        payload["name"] = name
        _validate_device(payload)

        cols = list(payload)
        placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
        async with self.db.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    f'INSERT INTO device ({", ".join(cols)}) VALUES ({placeholders}) '
                    f"RETURNING {DEVICE_COLUMNS}",
                    *[payload[c] for c in cols],
                )
            except Exception as exc:  # noqa: BLE001
                if "device_name_key" in str(exc):
                    raise RegistryError(f"устройство с именем {name!r} уже есть") from exc
                raise
        return dict(row)

    async def update_device(self, device_id: int, data: dict) -> dict:
        payload = _clean(data, DEVICE_EDITABLE, DEVICE_NULLABLE)
        if not payload:
            device = await self.get_device(device_id)
            if device is None:
                raise RegistryError("устройство не найдено")
            return device
        _validate_device(payload, partial=True)

        sets = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(payload))
        async with self.db.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"UPDATE device SET {sets}, updated_at = now() "
                    f"WHERE device_id = $1 RETURNING {DEVICE_COLUMNS}",
                    device_id, *payload.values(),
                )
                if row is None:
                    raise RegistryError("устройство не найдено")
                # tag.device дублирует имя устройства ради удобных запросов
                # в Grafana — поддерживаем его в согласованном состоянии.
                if "name" in payload:
                    await conn.execute(
                        "UPDATE tag SET device = $2 WHERE device_id = $1",
                        device_id, payload["name"])
        return dict(row)

    async def delete_device(self, device_id: int) -> None:
        async with self.db.acquire() as conn:
            status = await conn.execute("DELETE FROM device WHERE device_id = $1", device_id)
        if status.endswith("0"):
            raise RegistryError("устройство не найдено")

    # ----------------------------------------------------------------- теги
    async def list_tags(self, device_id: int | None = None,
                        node_id: int | None = None) -> list[dict]:
        clauses, args = [], []
        if device_id is not None:
            args.append(device_id)
            clauses.append(f"device_id = ${len(args)}")
        if node_id is not None:
            args.append(node_id)
            clauses.append(f"node_id = ${len(args)}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self.db.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {TAG_COLUMNS} FROM tag {where} ORDER BY device, register, address, name",
                *args)
        return [_tag_row(r) for r in rows]

    async def replace_tags(self, device_id: int, tags: Iterable[dict]) -> int:
        """Полностью заменить набор тегов устройства.

        Существующие теги с теми же именами обновляются (tag_id сохраняется —
        историю значений не теряем), лишние удаляются.
        """
        device = await self.get_device(device_id)
        if device is None:
            raise RegistryError("устройство не найдено")
        prepared = [_prepare_tag(t, device["name"], device_id) for t in tags]
        _check_unique_names(prepared)

        async with self.db.acquire() as conn:
            async with conn.transaction():
                keep: list[str] = []
                for tag in prepared:
                    await _upsert_tag(conn, tag)
                    keep.append(tag["name"])
                if keep:
                    await conn.execute(
                        "DELETE FROM tag WHERE device_id = $1 AND name <> ALL($2::text[])",
                        device_id, keep)
                else:
                    await conn.execute("DELETE FROM tag WHERE device_id = $1", device_id)
        return len(prepared)

    async def add_tags(self, device_id: int, tags: Iterable[dict]) -> int:
        device = await self.get_device(device_id)
        if device is None:
            raise RegistryError("устройство не найдено")
        prepared = [_prepare_tag(t, device["name"], device_id) for t in tags]
        _check_unique_names(prepared)
        async with self.db.acquire() as conn:
            async with conn.transaction():
                for tag in prepared:
                    await _upsert_tag(conn, tag)
        return len(prepared)

    async def update_tag(self, tag_id: int, data: dict) -> dict:
        payload = _clean(data, TAG_EDITABLE, TAG_NULLABLE)
        if "labels" in payload:
            payload["labels"] = json.dumps(payload["labels"] or {}, ensure_ascii=False)
        if not payload:
            raise RegistryError("нечего обновлять")
        cols = []
        for i, key in enumerate(payload):
            column = '"offset"' if key == "offset" else key
            cast = "::jsonb" if key == "labels" else ""
            cols.append(f"{column} = ${i + 2}{cast}")
        async with self.db.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE tag SET {', '.join(cols)}, updated_at = now() "
                f"WHERE tag_id = $1 RETURNING {TAG_COLUMNS}",
                tag_id, *payload.values())
        if row is None:
            raise RegistryError("тег не найден")
        return _tag_row(row)

    async def delete_tag(self, tag_id: int) -> None:
        async with self.db.acquire() as conn:
            status = await conn.execute("DELETE FROM tag WHERE tag_id = $1", tag_id)
        if status.endswith("0"):
            raise RegistryError("тег не найден")

    # -------------------------------------------------- цифровой двойник
    async def list_nodes(self) -> list[dict]:
        async with self.db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT n.node_id, n.parent_id, n.kind, n.name, n.description, "
                "       n.sort_order, n.meta, p.path, "
                "       (SELECT count(*) FROM tag t WHERE t.node_id = n.node_id) AS tag_count "
                "FROM twin_node n LEFT JOIN v_twin_path p USING (node_id) "
                "ORDER BY p.path NULLS FIRST, n.sort_order, n.name")
        return [_node_row(r) for r in rows]

    async def create_node(self, data: dict) -> dict:
        name = str(data.get("name") or "").strip()
        if not name:
            raise RegistryError("укажите название узла")
        kind = str(data.get("kind") or "machine")
        if kind not in {"site", "area", "line", "machine"}:
            raise RegistryError("kind должен быть site, area, line или machine")
        async with self.db.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO twin_node (parent_id, kind, name, description, sort_order, meta) "
                "VALUES ($1,$2,$3,$4,$5,$6::jsonb) "
                "RETURNING node_id, parent_id, kind, name, description, sort_order, meta",
                data.get("parent_id"), kind, name, data.get("description"),
                int(data.get("sort_order") or 0),
                json.dumps(data.get("meta") or {}, ensure_ascii=False))
        return _node_row(row)

    async def update_node(self, node_id: int, data: dict) -> dict:
        allowed = {"parent_id", "kind", "name", "description", "sort_order"}
        payload = _clean(data, allowed)
        if "meta" in data:
            payload["meta"] = json.dumps(data["meta"] or {}, ensure_ascii=False)
        if not payload:
            raise RegistryError("нечего обновлять")
        if payload.get("parent_id") == node_id:
            raise RegistryError("узел не может быть родителем самому себе")
        sets = ", ".join(
            f"{c} = ${i + 2}{'::jsonb' if c == 'meta' else ''}"
            for i, c in enumerate(payload))
        async with self.db.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE twin_node SET {sets} WHERE node_id = $1 "
                "RETURNING node_id, parent_id, kind, name, description, sort_order, meta",
                node_id, *payload.values())
        if row is None:
            raise RegistryError("узел не найден")
        return _node_row(row)

    async def delete_node(self, node_id: int) -> None:
        async with self.db.acquire() as conn:
            status = await conn.execute("DELETE FROM twin_node WHERE node_id = $1", node_id)
        if status.endswith("0"):
            raise RegistryError("узел не найден")

    # ------------------------------------------- сборка датаклассов поллера
    async def build_devices(self) -> list[Device]:
        """Собрать конфигурацию опроса из БД."""
        devices = await self.list_devices()
        tags = await self.list_tags()
        by_device: dict[int, list[dict]] = {}
        for tag in tags:
            by_device.setdefault(tag["device_id"], []).append(tag)

        result: list[Device] = []
        for row in devices:
            if not row["enabled"]:
                continue
            device_tags = [t for t in by_device.get(row["device_id"], []) if t["enabled"]]
            if not device_tags:
                continue
            result.append(_to_device(row, device_tags))
        return result


# ---------------------------------------------------------------------------
#  Преобразования
# ---------------------------------------------------------------------------
def _to_device(row: dict, tags: list[dict]) -> Device:
    device = Device(
        name=row["name"],
        host=row["host"] or "",
        port=int(row["port"]),
        unit_id=int(row["unit_id"]),
        transport=row["transport"],
        description=row["description"],
        enabled=True,
        timeout_s=float(row["timeout_s"]),
        retries=int(row["retries"]),
        reconnect_delay_s=float(row["reconnect_delay_s"]),
        reconnect_delay_max_s=float(row["reconnect_delay_max_s"]),
        inter_request_delay_ms=int(row["inter_request_delay_ms"]),
        block=BlockConfig(max_registers=int(row["block_max_registers"]),
                          max_gap=int(row["block_max_gap"])),
        tags=[_to_tag(t, row["name"]) for t in tags],
        serial_port=row["serial_port"],
        baudrate=int(row["baudrate"]),
        bytesize=int(row["bytesize"]),
        parity=row["parity"],
        stopbits=int(row["stopbits"]),
    )
    return device


def _to_tag(row: dict, device_name: str) -> Tag:
    tag = Tag(
        device=device_name,
        name=row["name"],
        address=int(row["address"]),
        register=row["register"],
        datatype=row["datatype"],
        description=row["description"],
        unit=row["unit"],
        bit=row["bit"],
        length=row["length"],
        scale=float(row["scale"]),
        offset=float(row["offset"]),
        min=row["min_value"],
        max=row["max_value"],
        poll_interval_ms=int(row["poll_interval_ms"]),
        deadband=float(row["deadband"]),
        deadband_mode=row["deadband_mode"],
        heartbeat_s=float(row["heartbeat_s"]),
        word_order=row["word_order"],
        byte_order=row["byte_order"],
        labels=row["labels"] or {},
        enabled=True,
    )
    tag.tag_id = row["tag_id"]
    return tag


def _tag_row(row) -> dict:
    data = dict(row)
    labels = data.get("labels")
    if isinstance(labels, str):
        try:
            data["labels"] = json.loads(labels)
        except json.JSONDecodeError:
            data["labels"] = {}
    return data


def _node_row(row) -> dict:
    data = dict(row)
    meta = data.get("meta")
    if isinstance(meta, str):
        try:
            data["meta"] = json.loads(meta)
        except json.JSONDecodeError:
            data["meta"] = {}
    return data


# Поля, которые можно осознанно сбросить в NULL из интерфейса.
# Остальные при значении null просто игнорируются: PATCH меняет только то,
# что реально прислали, и не может случайно затереть имя или адрес.
DEVICE_NULLABLE = {"node_id", "description", "serial_port", "host"}
TAG_NULLABLE = {"description", "unit", "bit", "length", "min_value", "max_value",
                "node_id", "group_name"}


def _clean(data: dict, allowed: set[str], nullable: set[str] = frozenset()) -> dict:
    return {
        k: v for k, v in (data or {}).items()
        if k in allowed and (v is not None or k in nullable)
    }


def _check_unique_names(tags: list[dict]) -> None:
    seen: set[str] = set()
    for tag in tags:
        if tag["name"] in seen:
            raise RegistryError(f"тег {tag['name']!r} встречается дважды")
        seen.add(tag["name"])


def _validate_device(payload: dict, partial: bool = False) -> None:
    transport = payload.get("transport", "tcp")
    if transport not in {"tcp", "udp", "rtu"}:
        raise RegistryError("transport должен быть tcp, udp или rtu")
    if transport in {"tcp", "udp"}:
        if not partial and not payload.get("host"):
            raise RegistryError("укажите IP-адрес или имя хоста устройства")
    elif not partial and not payload.get("serial_port"):
        raise RegistryError("для RTU укажите последовательный порт")
    port = int(payload.get("port", 502))
    if not 1 <= port <= 65535:
        raise RegistryError("порт должен быть в диапазоне 1..65535")
    unit = int(payload.get("unit_id", 1))
    if not 0 <= unit <= 247:
        raise RegistryError("unit_id должен быть 0..247")


def _prepare_tag(raw: dict, device_name: str, device_id: int) -> dict:
    """Проверить тег через валидатор ядра и вернуть строку для БД."""
    from modbus_logger.config import ConfigError, TagDefaults, parse_tag

    source = {k: v for k, v in raw.items() if v is not None}
    # В API поля называются как колонки БД, в ядре — как в YAML
    if "min_value" in source:
        source["min"] = source.pop("min_value")
    if "max_value" in source:
        source["max"] = source.pop("max_value")
    try:
        tag = parse_tag(source, device_name, TagDefaults())
    except ConfigError as exc:
        raise RegistryError(str(exc)) from exc

    return {
        "device_id": device_id,
        "device": device_name,
        "name": tag.name,
        "description": tag.description,
        "unit": tag.unit,
        "datatype": tag.datatype,
        "register": tag.register,
        "address": tag.address,
        "bit": tag.bit,
        "length": tag.length,
        "scale": tag.scale,
        "offset": tag.offset,
        "min_value": tag.min,
        "max_value": tag.max,
        "word_order": tag.word_order,
        "byte_order": tag.byte_order,
        "poll_interval_ms": tag.poll_interval_ms,
        "deadband": tag.deadband,
        "deadband_mode": tag.deadband_mode,
        "heartbeat_s": tag.heartbeat_s,
        "labels": json.dumps(tag.labels, ensure_ascii=False),
        "enabled": tag.enabled,
        "node_id": raw.get("node_id"),
        "group_name": raw.get("group_name"),
        "role": raw.get("role") or _guess_role(tag),
    }


def _guess_role(tag: Tag) -> str:
    if tag.datatype == "bool":
        name = (tag.name or "").lower()
        alarm_words = ("alarm", "fault", "error", "trip", "avar", "warn")
        return "alarm" if any(w in name for w in alarm_words) else "state"
    if tag.datatype in {"uint32", "uint64", "int32", "int64"} and tag.scale == 1:
        counter_words = ("count", "total", "counter", "produced", "schet")
        if any(w in (tag.name or "").lower() for w in counter_words):
            return "counter"
    return "value"


async def _upsert_tag(conn, tag: dict) -> None:
    cols = list(tag)
    quoted = ['"offset"' if c == "offset" else c for c in cols]
    placeholders = ", ".join(
        f"${i + 1}" + ("::jsonb" if c == "labels" else "") for i, c in enumerate(cols))
    updates = ", ".join(
        f"{q} = EXCLUDED.{q}" for q in quoted if q not in ("device_id", "device", "name"))
    quoted = ", ".join(quoted)
    await conn.execute(
        f"INSERT INTO tag ({quoted}) VALUES ({placeholders}) "
        f"ON CONFLICT (device, name) DO UPDATE SET {updates}, updated_at = now()",
        *[tag[c] for c in cols])
