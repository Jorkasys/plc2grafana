"""Запись значений в БД и раздача их в браузер «вживую».

Пишет в базу тот же буферизованный ``Storage`` из ядра ``modbus_logger``
(пачками через COPY, с переживанием обрыва связи с БД). Дополнительно каждое
значение попадает в ``LiveBus`` — оттуда его забирает WebSocket веб-интерфейса.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Iterable

from modbus_logger.storage import QUALITY_NAMES, Sample, Storage

log = logging.getLogger("app.store")

MAX_SUBSCRIBER_QUEUE = 500


class LiveBus:
    """Последние значения тегов + рассылка подписчикам WebSocket."""

    def __init__(self) -> None:
        self._latest: dict[int, dict[str, Any]] = {}
        self._subscribers: set[asyncio.Queue] = set()

    def publish(self, tag_id: int, ts: datetime, value: float | None,
                text: str | None, quality: int) -> None:
        item = {
            "tag_id": tag_id,
            "ts": ts.isoformat(),
            "value": value,
            "text": text,
            "quality": quality,
            "quality_name": QUALITY_NAMES.get(quality, str(quality)),
        }
        self._latest[tag_id] = item
        dead: list[asyncio.Queue] = []
        for queue in self._subscribers:
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                dead.append(queue)
        for queue in dead:
            # Подписчик не успевает читать — отцепляем, браузер переподключится
            self._subscribers.discard(queue)

    def snapshot(self) -> list[dict[str, Any]]:
        return list(self._latest.values())

    def latest(self, tag_id: int) -> dict[str, Any] | None:
        return self._latest.get(tag_id)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_SUBSCRIBER_QUEUE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def forget(self, tag_ids: Iterable[int]) -> None:
        for tag_id in tag_ids:
            self._latest.pop(tag_id, None)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


class ValueWriter(Storage):
    """Storage ядра + публикация в LiveBus."""

    def __init__(self, cfg, bus: LiveBus) -> None:
        super().__init__(cfg)
        self.bus = bus
        self.samples_total = 0
        self.started_at = time.time()

    def enqueue(self, sample: Sample) -> None:  # type: ignore[override]
        super().enqueue(sample)
        self.samples_total += 1
        self.bus.publish(sample.tag_id, sample.ts, sample.value,
                         sample.value_text, sample.quality)


# ---------------------------------------------------------------------------
#  Чтение истории
# ---------------------------------------------------------------------------
async def series(db, tag_ids: list[int], start: datetime, end: datetime,
                 max_points: int = 1500) -> dict[int, list[list[float | None]]]:
    """История нескольких тегов, прорежённая до max_points на тег.

    Прореживание делает SQL: панель одинаково быстро открывается и на часе,
    и на месяце.
    """
    if not tag_ids:
        return {}
    span = max((end - start).total_seconds(), 1.0)
    step = max(span / max_points, 0.001)

    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT tag_id,
                   to_timestamp(floor(extract(epoch FROM ts) / $4) * $4) AS bucket,
                   avg(value)                            AS avg_value,
                   min(value)                            AS min_value,
                   max(value)                            AS max_value,
                   max(quality)                          AS quality
            FROM tag_value
            WHERE tag_id = ANY($1::int[]) AND ts >= $2 AND ts <= $3
            GROUP BY tag_id, bucket
            ORDER BY tag_id, bucket
            """,
            tag_ids, start, end, step,
        )

    result: dict[int, list[list[float | None]]] = {tid: [] for tid in tag_ids}
    for row in rows:
        result[row["tag_id"]].append([
            row["bucket"].timestamp(),
            _num(row["avg_value"]),
            _num(row["min_value"]),
            _num(row["max_value"]),
            int(row["quality"] or 0),
        ])
    return result


async def latest_values(db, device_id: int | None = None) -> list[dict[str, Any]]:
    """Текущее значение каждого тега вместе с его «возрастом»."""
    clause, args = "", []
    if device_id is not None:
        args.append(device_id)
        clause = "WHERE t.device_id = $1"
    async with db.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT DISTINCT ON (t.tag_id)
                   t.tag_id, t.device_id, t.device, t.name, t.description, t.unit,
                   t.datatype, t.role, t.group_name, t.node_id,
                   t.min_value, t.max_value,
                   v.ts, v.value, v.value_text, v.quality,
                   EXTRACT(EPOCH FROM (now() - v.ts)) AS age_s
            FROM tag t
            LEFT JOIN tag_value v ON v.tag_id = t.tag_id
            {clause}
            ORDER BY t.tag_id, v.ts DESC
            """,
            *args,
        )
    out = []
    for row in rows:
        item = dict(row)
        item["ts"] = row["ts"].isoformat() if row["ts"] else None
        item["age_s"] = _num(row["age_s"])
        item["quality_name"] = QUALITY_NAMES.get(row["quality"], None)
        out.append(item)
    return out


async def storage_stats(db) -> dict[str, Any]:
    """Сколько данных накоплено — показывается на странице «Система»."""
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT count(*) AS rows, min(ts) AS first_ts, max(ts) AS last_ts FROM tag_value")
        size = await conn.fetchval(
            "SELECT pg_size_pretty(pg_total_relation_size('tag_value'))")
        tags = await conn.fetchval("SELECT count(*) FROM tag")
        devices = await conn.fetchval("SELECT count(*) FROM device")
    return {
        "rows": int(row["rows"] or 0),
        "first_ts": row["first_ts"].isoformat() if row["first_ts"] else None,
        "last_ts": row["last_ts"].isoformat() if row["last_ts"] else None,
        "table_size": size,
        "tags": int(tags or 0),
        "devices": int(devices or 0),
    }


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _num(value) -> float | None:
    return None if value is None else float(value)
