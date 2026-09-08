"""Служебный HTTP-эндпоинт: /healthz, /readyz, /metrics, /tags.

Нужен для healthcheck в Docker и (опционально) для Prometheus.
"""

from __future__ import annotations

import logging
import time

from aiohttp import web

log = logging.getLogger(__name__)


def _esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


class HealthServer:
    def __init__(self, app_ref, storage, host: str = "0.0.0.0", port: int = 8080) -> None:
        self.app_ref = app_ref
        self.storage = storage
        self.host = host
        self.port = port
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.add_routes([
            web.get("/healthz", self.healthz),
            web.get("/readyz", self.readyz),
            web.get("/metrics", self.metrics),
            web.get("/tags", self.tags),
        ])
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        log.info("HTTP: http://%s:%d/healthz | /readyz | /metrics | /tags", self.host, self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    # ---------------------------------------------------------------------
    async def healthz(self, _request: web.Request) -> web.Response:
        """Процесс жив. Всегда 200, пока event loop крутится."""
        return web.json_response({"status": "ok"})

    async def readyz(self, _request: web.Request) -> web.Response:
        """Готовность: есть БД и хотя бы одно устройство отвечает."""
        devices = {
            p.device.name: {
                "connected": p.stats.connected,
                "polls": p.stats.polls,
                "poll_errors": p.stats.poll_errors,
                "seconds_since_success": (
                    round(time.monotonic() - p.stats.last_success, 1)
                    if p.stats.last_success else None
                ),
            }
            for p in self.app_ref.pollers
        }
        ready = self.storage.db_connected and any(d["connected"] for d in devices.values())
        return web.json_response(
            {
                "status": "ready" if ready else "degraded",
                "database": {
                    "connected": self.storage.db_connected,
                    "pending_rows": self.storage.pending,
                    "rows_written": self.storage.rows_written,
                    "rows_dropped": self.storage.rows_dropped,
                },
                "devices": devices,
            },
            status=200 if ready else 503,
        )

    async def tags(self, _request: web.Request) -> web.Response:
        """Текущая карта тегов — удобно проверить, что конфиг разобран верно."""
        payload = []
        for poller in self.app_ref.pollers:
            for tag in poller.device.tags:
                state = poller.states.get(tag.name)
                payload.append({
                    "device": tag.device,
                    "name": tag.name,
                    "tag_id": tag.tag_id,
                    "register": tag.register,
                    "address": tag.address,
                    "datatype": tag.datatype,
                    "unit": tag.unit,
                    "poll_interval_ms": tag.poll_interval_ms,
                    "last_value": state.last_value if state else None,
                    "last_quality": state.last_quality if state else None,
                })
        return web.json_response({"tags": payload})

    async def metrics(self, _request: web.Request) -> web.Response:
        """Метрики в формате Prometheus (если захотите добавить его рядом)."""
        lines = [
            "# HELP modbus_rows_written_total Строк записано в БД",
            "# TYPE modbus_rows_written_total counter",
            f"modbus_rows_written_total {self.storage.rows_written}",
            "# HELP modbus_rows_dropped_total Строк потеряно из-за переполнения буфера",
            "# TYPE modbus_rows_dropped_total counter",
            f"modbus_rows_dropped_total {self.storage.rows_dropped}",
            "# HELP modbus_buffer_rows Текущий размер буфера записи",
            "# TYPE modbus_buffer_rows gauge",
            f"modbus_buffer_rows {self.storage.pending}",
            "# HELP modbus_db_connected Есть ли соединение с БД",
            "# TYPE modbus_db_connected gauge",
            f"modbus_db_connected {int(self.storage.db_connected)}",
            "# HELP modbus_device_connected Есть ли соединение с устройством",
            "# TYPE modbus_device_connected gauge",
            "# HELP modbus_polls_total Всего обращений к устройству",
            "# TYPE modbus_polls_total counter",
            "# HELP modbus_poll_errors_total Ошибок обращения к устройству",
            "# TYPE modbus_poll_errors_total counter",
        ]
        for poller in self.app_ref.pollers:
            label = f'{{device="{_esc(poller.device.name)}"}}'
            lines += [
                f"modbus_device_connected{label} {int(poller.stats.connected)}",
                f"modbus_polls_total{label} {poller.stats.polls}",
                f"modbus_poll_errors_total{label} {poller.stats.poll_errors}",
            ]
        return web.Response(text="\n".join(lines) + "\n",
                            content_type="text/plain", charset="utf-8")
