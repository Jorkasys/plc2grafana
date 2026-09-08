"""Точка входа сервиса.

    python -m modbus_logger --config config/config.yaml
    python -m modbus_logger --config config/config.yaml --check   # только проверить конфиг
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from . import __version__, logging_conf
from .blocks import build_blocks, summarize
from .config import ConfigError, load
from .http_api import HealthServer
from .poller import Application
from .storage import Storage

log = logging.getLogger("modbus_logger")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="modbus_logger",
        description="Опрос Modbus-устройств и запись значений в PostgreSQL/TimescaleDB",
    )
    parser.add_argument(
        "-c", "--config",
        default=os.environ.get("CONFIG_PATH", "config/config.yaml"),
        help="путь к config.yaml (по умолчанию: %(default)s)",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="разобрать конфигурацию, показать план опроса и выйти",
    )
    parser.add_argument("--version", action="version", version=f"modbus_logger {__version__}")
    return parser.parse_args(argv)


async def run(config) -> int:
    storage = Storage(config.database)
    app = Application(config, storage)
    health: HealthServer | None = None

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # Windows
            signal.signal(sig, lambda *_: stop_event.set())

    try:
        await storage.start()
        await app.start()
        if config.http.enabled:
            health = HealthServer(app, storage, config.http.host, config.http.port)
            try:
                await health.start()
            except OSError as exc:
                health = None
                log.error(
                    "не удалось занять порт %s:%d для health-эндпоинта (%s). "
                    "Опрос продолжится, но /healthz и /metrics будут недоступны — "
                    "смените http.port в конфигурации или освободите порт.",
                    config.http.host, config.http.port, exc,
                )

        log.info("сервис запущен, для остановки — Ctrl+C / SIGTERM")
        await stop_event.wait()
        log.info("получен сигнал остановки, завершаем работу…")
    finally:
        if health is not None:
            await health.stop()
        await app.stop()
        await storage.stop()
        log.info("остановлено. Записано строк: %d, потеряно: %d",
                 storage.rows_written, storage.rows_dropped)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load(args.config)
    except ConfigError as exc:
        logging_conf.setup("INFO", "text")
        log.error("ошибка конфигурации: %s", exc)
        return 2

    logging_conf.setup(config.logging.level, config.logging.format)
    log.info("modbus_logger %s, конфигурация: %s", __version__, args.config)

    if args.check:
        for device in config.devices:
            status = "включено" if device.enabled else "ОТКЛЮЧЕНО"
            endpoint = (device.serial_port if device.transport == "rtu"
                        else f"{device.host}:{device.port}")
            log.info("устройство %s (%s, %s, unit=%d)",
                     device.name, endpoint, status, device.unit_id)
            log.info("%s", summarize(build_blocks(device.tags, device.block)))
        log.info("конфигурация корректна")
        return 0

    try:
        return asyncio.run(run(config))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        log.exception("аварийная остановка: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
