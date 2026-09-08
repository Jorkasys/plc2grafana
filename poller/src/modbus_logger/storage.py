"""Запись значений в PostgreSQL/TimescaleDB.

Значения не пишутся по одному: они складываются в буфер и уходят пачкой
через COPY. Если БД недоступна, буфер держит данные в памяти (кольцевой,
самые старые вытесняются) и сбрасывает их, как только связь вернётся.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime

import asyncpg

from .config import DatabaseConfig, Tag

log = logging.getLogger(__name__)

# Коды качества — должны совпадать с комментарием в db/001_core.sql
QUALITY_GOOD = 0
QUALITY_BAD_COMM = 1
QUALITY_BAD_RANGE = 2
QUALITY_BAD_DECODE = 3
QUALITY_STALE = 4

QUALITY_NAMES = {
    QUALITY_GOOD: "GOOD",
    QUALITY_BAD_COMM: "BAD_COMM",
    QUALITY_BAD_RANGE: "BAD_RANGE",
    QUALITY_BAD_DECODE: "BAD_DECODE",
    QUALITY_STALE: "STALE",
}


@dataclass(slots=True)
class Sample:
    ts: datetime
    tag_id: int
    value: float | None
    value_text: str | None
    quality: int

    def as_record(self) -> tuple:
        return (self.ts, self.tag_id, self.value, self.value_text, self.quality)


class Storage:
    def __init__(self, cfg: DatabaseConfig) -> None:
        self.cfg = cfg
        self._pool: asyncpg.Pool | None = None
        self._buffer: deque[Sample] = deque()
        self._flush_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._wake = asyncio.Event()
        # Статистика
        self.rows_written = 0
        self.rows_dropped = 0
        self.flush_errors = 0
        self.db_connected = False

    # -- жизненный цикл ----------------------------------------------------
    async def start(self, retry_forever: bool = True) -> None:
        await self._connect(retry_forever=retry_forever)
        self._flush_task = asyncio.create_task(self._flush_loop(), name="storage-flush")

    async def _connect(self, retry_forever: bool = True) -> None:
        delay = 1.0
        while not self._stopping.is_set():
            try:
                self._pool = await asyncpg.create_pool(
                    dsn=self.cfg.dsn,
                    min_size=self.cfg.min_pool_size,
                    max_size=self.cfg.max_pool_size,
                    command_timeout=30,
                )
                async with self._pool.acquire() as conn:
                    await conn.fetchval("SELECT 1")
                self.db_connected = True
                log.info("БД подключена: %s", self.cfg.safe_dsn())
                return
            except Exception as exc:  # noqa: BLE001
                self.db_connected = False
                log.warning("БД недоступна (%s), повтор через %.0f с", exc, delay)
                if not retry_forever:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)

    async def stop(self) -> None:
        self._stopping.set()
        self._wake.set()
        if self._flush_task:
            await asyncio.gather(self._flush_task, return_exceptions=True)
        if self._pool is not None:
            # последняя попытка сохранить остатки
            try:
                await self._flush_once()
            except Exception as exc:  # noqa: BLE001
                log.error("не удалось сохранить остаток буфера (%d строк): %s",
                          len(self._buffer), exc)
            await self._pool.close()
        self.db_connected = False

    # -- справочник тегов --------------------------------------------------
    async def sync_tags(self, tags: list[Tag]) -> None:
        """Записать метаданные тегов в таблицу tag и проставить tag_id.

        Идемпотентно: повторный запуск обновляет описания и не плодит дублей.
        """
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                for tag in tags:
                    tag.tag_id = await conn.fetchval(
                        """
                        INSERT INTO tag (device, name, description, unit, datatype,
                                         register, address, bit, scale, "offset",
                                         min_value, max_value, labels, enabled, updated_at)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,$14, now())
                        ON CONFLICT (device, name) DO UPDATE SET
                            description = EXCLUDED.description,
                            unit        = EXCLUDED.unit,
                            datatype    = EXCLUDED.datatype,
                            register    = EXCLUDED.register,
                            address     = EXCLUDED.address,
                            bit         = EXCLUDED.bit,
                            scale       = EXCLUDED.scale,
                            "offset"    = EXCLUDED."offset",
                            min_value   = EXCLUDED.min_value,
                            max_value   = EXCLUDED.max_value,
                            labels      = EXCLUDED.labels,
                            enabled     = EXCLUDED.enabled,
                            updated_at  = now()
                        RETURNING tag_id
                        """,
                        tag.device, tag.name, tag.description, tag.unit, tag.datatype,
                        tag.register, tag.address, tag.bit, tag.scale, tag.offset,
                        tag.min, tag.max, _json(tag.labels), tag.enabled,
                    )
        log.info("справочник тегов синхронизирован: %d шт.", len(tags))

    # -- запись значений ---------------------------------------------------
    def enqueue(self, sample: Sample) -> None:
        if len(self._buffer) >= self.cfg.buffer_max_rows:
            self._buffer.popleft()
            self.rows_dropped += 1
            if self.rows_dropped % 1000 == 1:
                log.error(
                    "буфер переполнен (%d строк), отброшено всего %d — БД не успевает",
                    self.cfg.buffer_max_rows, self.rows_dropped,
                )
        self._buffer.append(sample)
        if len(self._buffer) >= self.cfg.batch_max_rows:
            self._wake.set()

    @property
    def pending(self) -> int:
        return len(self._buffer)

    async def _flush_loop(self) -> None:
        delay = self.cfg.batch_max_delay_ms / 1000
        reconnect_backoff = 1.0

        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

            # Пул мог быть закрыт после сбоя — восстанавливаем его здесь,
            # иначе буфер будет расти вечно и данные потеряются.
            if self._pool is None:
                if await self._reconnect_quietly():
                    reconnect_backoff = 1.0
                    log.info("соединение с БД восстановлено, дописываем буфер (%d строк)",
                             len(self._buffer))
                else:
                    await asyncio.sleep(min(reconnect_backoff, 30.0))
                    reconnect_backoff = min(reconnect_backoff * 2, 30.0)
                    continue

            if not self._buffer:
                continue

            try:
                # Пока есть накопленный буфер — выгребаем его пачками,
                # не дожидаясь следующего тика таймера.
                while self._buffer and not self._stopping.is_set():
                    await self._flush_once()
                    if len(self._buffer) < self.cfg.batch_max_rows:
                        break
            except Exception as exc:  # noqa: BLE001
                self.flush_errors += 1
                self.db_connected = False
                log.error("ошибка записи в БД: %s (в буфере %d строк)", exc, len(self._buffer))
                await self._close_pool()

    async def _flush_once(self) -> None:
        if not self._buffer:
            return
        if self._pool is None:
            raise ConnectionError("нет пула соединений с БД")
        batch = [self._buffer.popleft() for _ in range(min(len(self._buffer),
                                                          self.cfg.batch_max_rows))]
        records = [s.as_record() for s in batch]
        try:
            async with self._pool.acquire() as conn:
                await conn.copy_records_to_table(
                    "tag_value",
                    records=records,
                    columns=["ts", "tag_id", "value", "value_text", "quality"],
                )
        except Exception:
            # вернуть пачку в начало буфера, чтобы не потерять данные
            self._buffer.extendleft(reversed(batch))
            raise
        self.rows_written += len(records)
        self.db_connected = True
        log.debug("записано %d строк (в буфере ещё %d)", len(records), len(self._buffer))

    async def _close_pool(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            try:
                await asyncio.wait_for(pool.close(), timeout=5)
            except Exception:  # noqa: BLE001
                pool.terminate()

    async def _reconnect_quietly(self) -> bool:
        """Одна попытка поднять пул заново. True — получилось."""
        await self._close_pool()
        try:
            await self._connect(retry_forever=False)
            return True
        except Exception:  # noqa: BLE001
            return False


def _json(obj) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)
