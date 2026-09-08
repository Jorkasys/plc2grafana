"""Цикл опроса устройства: читаем блоки, декодируем теги, пишем в БД."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from . import codec
from .blocks import ReadBlock, build_blocks, summarize
from .config import Device, Tag
from .modbus import ModbusConnectionLost, ModbusReader, ModbusReadError
from .storage import (
    QUALITY_BAD_COMM,
    QUALITY_BAD_DECODE,
    QUALITY_BAD_RANGE,
    QUALITY_GOOD,
    Sample,
    Storage,
)

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class TagState:
    """Что мы записали в БД в последний раз — нужно для мёртвой зоны."""

    last_value: float | str | None = None
    last_quality: int | None = None
    last_written: float = 0.0  # time.monotonic()


@dataclass(slots=True)
class DeviceStats:
    polls: int = 0
    poll_errors: int = 0
    decode_errors: int = 0
    samples_written: int = 0
    last_success: float = 0.0
    connected: bool = False


class DevicePoller:
    """Опрашивает одно устройство. На каждое устройство — своя задача."""

    def __init__(self, device: Device, storage: Storage) -> None:
        self.device = device
        self.storage = storage
        self.reader = ModbusReader(device)
        self.blocks: list[ReadBlock] = build_blocks(device.tags, device.block)
        self.states: dict[str, TagState] = {t.name: TagState() for t in device.tags}
        self.stats = DeviceStats()
        self._stop = asyncio.Event()

    # ----------------------------------------------------------------- run
    async def run(self) -> None:
        log.info("[%s] %s", self.device.name, summarize(self.blocks))
        if not self.blocks:
            log.warning("[%s] нет включённых тегов — опрос не запускается", self.device.name)
            return

        now = time.monotonic()
        for i, block in enumerate(self.blocks):
            # разносим старты блоков, чтобы не бить по ПЛК всеми запросами разом
            block.next_due = now + i * 0.02

        await self.reader.connect()

        while not self._stop.is_set():
            now = time.monotonic()
            due = [b for b in self.blocks if b.next_due <= now]

            if not due:
                sleep_for = min(b.next_due for b in self.blocks) - now
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=max(sleep_for, 0.001))
                except asyncio.TimeoutError:
                    pass
                continue

            for block in due:
                if self._stop.is_set():
                    break
                await self._poll_block(block)
                # следующий срок считаем от «идеальной» сетки, но не допускаем
                # накопления задолженности, если ПЛК отвечал дольше периода
                period = block.poll_interval_ms / 1000
                block.next_due += period
                if block.next_due < time.monotonic():
                    block.next_due = time.monotonic() + period

        await self.reader.close()

    def stop(self) -> None:
        self._stop.set()

    # --------------------------------------------------------------- poll
    async def _poll_block(self, block: ReadBlock) -> None:
        self.stats.polls += 1
        try:
            values = await self.reader.read_block(block.register, block.address, block.count)
        except ModbusConnectionLost as exc:
            self.stats.poll_errors += 1
            self.stats.connected = False
            log.warning("[%s] %s: связь потеряна — %s", self.device.name, block, exc)
            self._mark_bad(block, QUALITY_BAD_COMM)
            await self.reader.reconnect_with_backoff()
            return
        except ModbusReadError as exc:
            self.stats.poll_errors += 1
            log.warning("[%s] %s: ошибка чтения — %s", self.device.name, block, exc)
            self._mark_bad(block, QUALITY_BAD_COMM)
            return

        self.stats.connected = True
        self.stats.last_success = time.monotonic()
        ts = _now()
        for tag in block.tags:
            self._handle_tag(tag, block.slice_for(tag, values), ts)

    def _handle_tag(self, tag: Tag, raw: list, ts: datetime) -> None:
        try:
            decoded = codec.decode(
                raw,
                tag.datatype,
                word_order=tag.word_order,
                byte_order=tag.byte_order,
                bit=tag.bit,
            )
        except codec.DecodeError as exc:
            self.stats.decode_errors += 1
            log.warning("[%s] тег %s: %s (сырые данные: %s)", self.device.name, tag.name, exc, raw)
            self._store(tag, None, None, QUALITY_BAD_DECODE, ts)
            return

        if tag.datatype == "string":
            self._store(tag, None, str(decoded), QUALITY_GOOD, ts)
            return

        if tag.datatype == "bool":
            self._store(tag, 1.0 if decoded else 0.0, None, QUALITY_GOOD, ts)
            return

        value = float(decoded) * tag.scale + tag.offset
        quality = QUALITY_GOOD
        if (tag.min is not None and value < tag.min) or (tag.max is not None and value > tag.max):
            quality = QUALITY_BAD_RANGE
            log.debug("[%s] тег %s: значение %g вне [%s; %s]",
                      self.device.name, tag.name, value, tag.min, tag.max)
        self._store(tag, value, None, quality, ts)

    # -------------------------------------------------------------- запись
    def _mark_bad(self, block: ReadBlock, quality: int) -> None:
        ts = _now()
        for tag in block.tags:
            self._store(tag, None, None, quality, ts)

    def _store(self, tag: Tag, value: float | None, text: str | None,
               quality: int, ts: datetime) -> None:
        if tag.tag_id is None:
            return
        state = self.states[tag.name]
        current = text if text is not None else value

        if not self._should_write(tag, state, current, quality):
            return

        self.storage.enqueue(Sample(ts=ts, tag_id=tag.tag_id, value=value,
                                    value_text=text, quality=quality))
        state.last_value = current
        state.last_quality = quality
        state.last_written = time.monotonic()
        self.stats.samples_written += 1

    @staticmethod
    def _should_write(tag: Tag, state: TagState, value, quality: int) -> bool:
        # Первое значение пишем всегда
        if state.last_quality is None:
            return True
        # Смена качества — важное событие, фиксируем немедленно
        if quality != state.last_quality:
            return True
        # «Сердцебиение»: раз в heartbeat_s пишем даже неизменившееся значение
        if tag.heartbeat_s > 0 and (time.monotonic() - state.last_written) >= tag.heartbeat_s:
            return True
        if quality != QUALITY_GOOD:
            return False
        if isinstance(value, str) or isinstance(state.last_value, str):
            return value != state.last_value
        if value is None or state.last_value is None:
            return value != state.last_value
        if tag.deadband <= 0:
            return True
        delta = abs(float(value) - float(state.last_value))
        if tag.deadband_mode == "percent":
            base = abs(float(state.last_value)) or 1.0
            return delta / base * 100.0 >= tag.deadband
        return delta >= tag.deadband


class Application:
    """Связывает конфигурацию, хранилище и опрос всех устройств."""

    def __init__(self, config, storage: Storage) -> None:
        self.config = config
        self.storage = storage
        self.pollers: list[DevicePoller] = []
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        all_tags: list[Tag] = []
        for device in self.config.devices:
            if not device.enabled:
                log.info("[%s] отключено в конфигурации — пропускаем", device.name)
                continue
            all_tags.extend(device.tags)
        if not all_tags:
            raise RuntimeError("нет ни одного включённого тега")

        await self.storage.sync_tags(all_tags)

        for device in self.config.devices:
            if not device.enabled:
                continue
            poller = DevicePoller(device, self.storage)
            self.pollers.append(poller)
            self._tasks.append(asyncio.create_task(poller.run(), name=f"poll-{device.name}"))

    async def wait(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def stop(self) -> None:
        for poller in self.pollers:
            poller.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
