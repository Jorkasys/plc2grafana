"""Менеджер опроса: запускает и останавливает поллеры устройств.

Веб-интерфейс правит устройства и теги в базе, затем зовёт ``reload()`` —
менеджер пересобирает конфигурацию и перезапускает опрос, не роняя процесс.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from modbus_logger.blocks import build_blocks
from modbus_logger.poller import DevicePoller

from .db import Database
from .registry import Registry
from .settings import Settings
from .store import LiveBus, ValueWriter

log = logging.getLogger("app.runtime")

RETENTION_INTERVAL_S = 3600


class Runtime:
    def __init__(self, settings: Settings, db: Database, registry: Registry) -> None:
        self.settings = settings
        self.db = db
        self.registry = registry
        self.bus = LiveBus()
        self.writer: ValueWriter | None = None
        self.pollers: dict[str, DevicePoller] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._maintenance: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.started_at: float | None = None
        self.last_error: str | None = None

    # ------------------------------------------------------------ жизненный цикл
    async def start(self) -> None:
        if self.writer is None:
            self.writer = ValueWriter(self.settings.database, self.bus)
            await self.writer.start(retry_forever=False)
        if self._maintenance is None:
            self._maintenance = asyncio.create_task(self._maintenance_loop(),
                                                    name="maintenance")
        await self.reload()
        self.started_at = time.time()

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_pollers()
        if self._maintenance is not None:
            self._maintenance.cancel()
            await asyncio.gather(self._maintenance, return_exceptions=True)
            self._maintenance = None
        if self.writer is not None:
            await self.writer.stop()
            self.writer = None
        self.started_at = None

    async def reload(self) -> dict[str, Any]:
        """Перечитать устройства из БД и перезапустить опрос."""
        async with self._lock:
            await self._stop_pollers()
            self.last_error = None
            try:
                devices = await self.registry.build_devices()
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                log.exception("не удалось собрать конфигурацию опроса")
                return {"devices": 0, "tags": 0, "error": self.last_error}

            if self.writer is None:
                self.writer = ValueWriter(self.settings.database, self.bus)
                await self.writer.start(retry_forever=False)

            total_tags = 0
            for device in devices:
                poller = DevicePoller(device, self.writer)
                self.pollers[device.name] = poller
                self._tasks[device.name] = asyncio.create_task(
                    self._run_poller(poller), name=f"poll-{device.name}")
                total_tags += len(device.tags)
            log.info("опрос запущен: %d устройств, %d тегов", len(devices), total_tags)
            return {"devices": len(devices), "tags": total_tags, "error": None}

    async def _run_poller(self, poller: DevicePoller) -> None:
        try:
            await poller.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s] опрос аварийно остановлен: %s", poller.device.name, exc)

    async def _stop_pollers(self) -> None:
        for poller in self.pollers.values():
            poller.stop()
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self.pollers.clear()
        self._tasks.clear()

    async def _maintenance_loop(self) -> None:
        """Раз в час чистим старые данные согласно сроку хранения."""
        while True:
            try:
                await asyncio.sleep(RETENTION_INTERVAL_S)
                if self.db.connected:
                    deleted = await self.db.apply_retention()
                    if deleted:
                        log.info("срок хранения: удалено %d строк", deleted)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("обслуживание БД: %s", exc)

    # ------------------------------------------------------------------ статус
    def status(self) -> dict[str, Any]:
        devices = []
        now = time.monotonic()
        for name, poller in self.pollers.items():
            stats = poller.stats
            devices.append({
                "device": name,
                "host": poller.device.host,
                "port": poller.device.port,
                "unit_id": poller.device.unit_id,
                "connected": stats.connected,
                "polls": stats.polls,
                "poll_errors": stats.poll_errors,
                "decode_errors": stats.decode_errors,
                "samples_written": stats.samples_written,
                "tags": len(poller.device.tags),
                "blocks": len(poller.blocks),
                "seconds_since_success": (
                    round(now - stats.last_success, 1) if stats.last_success else None),
            })
        writer = self.writer
        return {
            "running": bool(self.pollers),
            "started_at": self.started_at,
            "last_error": self.last_error,
            "devices": devices,
            "storage": {
                "db_connected": bool(writer and writer.db_connected),
                "pending_rows": writer.pending if writer else 0,
                "rows_written": writer.rows_written if writer else 0,
                "rows_dropped": writer.rows_dropped if writer else 0,
                "flush_errors": writer.flush_errors if writer else 0,
            },
            "live_subscribers": self.bus.subscriber_count,
        }

    def plan(self) -> list[dict[str, Any]]:
        """Во что превратились теги: какие Modbus-запросы реально уходят."""
        out = []
        for name, poller in self.pollers.items():
            for block in poller.blocks:
                out.append({
                    "device": name,
                    "register": block.register,
                    "address": block.address,
                    "count": block.count,
                    "poll_interval_ms": block.poll_interval_ms,
                    "tags": [t.name for t in block.tags],
                })
        return out

    async def preview_plan(self) -> list[dict[str, Any]]:
        """То же самое, но по текущему содержимому БД, без перезапуска опроса."""
        out = []
        for device in await self.registry.build_devices():
            for block in build_blocks(device.tags, device.block):
                out.append({
                    "device": device.name,
                    "register": block.register,
                    "address": block.address,
                    "count": block.count,
                    "poll_interval_ms": block.poll_interval_ms,
                    "tags": [t.name for t in block.tags],
                })
        return out
