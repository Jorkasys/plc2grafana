#!/usr/bin/env python3
"""Запуск приложения одной командой:

    python run.py                 # веб-интерфейс на http://127.0.0.1:8000
    python run.py --port 8080
    python run.py --host 0.0.0.0  # доступ с других машин в сети
    python run.py --no-browser
    python run.py --log-file runtime/logs/app.log   # для работы службой
    python run.py --install-grafana                 # скачать Grafana и выйти
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Русские сообщения в консоли Windows иначе превращаются в кракозябры:
# по умолчанию там cp866, а не UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from app import __version__  # noqa: E402
from app.settings import Settings  # noqa: E402

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)-22s %(message)s"


def parse_args() -> argparse.Namespace:
    settings = Settings.load()
    parser = argparse.ArgumentParser(prog="plc2grafana", description=__doc__)
    parser.add_argument("--host", default=settings.server.host,
                        help="адрес веб-интерфейса (по умолчанию: %(default)s)")
    parser.add_argument("--port", type=int, default=settings.server.port,
                        help="порт веб-интерфейса (по умолчанию: %(default)s)")
    parser.add_argument("--no-browser", action="store_true",
                        help="не открывать браузер при старте")
    parser.add_argument("--reload", action="store_true",
                        help="перезапуск при правке кода (для разработки)")
    parser.add_argument("--log-level", default="info",
                        choices=["debug", "info", "warning", "error"])
    parser.add_argument("--log-file", default=None,
                        help="дополнительно писать журнал в файл (с ротацией)")
    parser.add_argument("--install-grafana", action="store_true",
                        help="скачать portable-сборку Grafana и выйти")
    parser.add_argument("--version", action="version", version=f"plc2grafana {__version__}")
    args = parser.parse_args()
    args._settings = settings
    return args


def open_browser_later(url: str, delay: float = 1.5) -> None:
    def worker() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=worker, daemon=True).start()


def setup_logging(level: str, log_file: str | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        path = Path(log_file)
        if not path.is_absolute():
            path = ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"))
    logging.basicConfig(level=level.upper(), format=LOG_FORMAT, handlers=handlers)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def lan_addresses() -> list[str]:
    """Адреса этой машины в локальной сети — чтобы подсказать, куда заходить."""
    found: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    try:
        # Адрес интерфейса, через который идёт маршрут «наружу»; пакеты не шлются
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            found.add(probe.getsockname()[0])
    except OSError:
        pass
    return sorted(ip for ip in found if not ip.startswith("127."))


def install_grafana(settings: Settings) -> int:
    from app.grafana.manager import GrafanaError, GrafanaManager

    manager = GrafanaManager(settings)
    if manager.installed:
        print(f"Grafana {settings.grafana.version} уже установлена: {manager.install_dir}")
        return 0
    print(f"Скачиваем Grafana {settings.grafana.version} ({manager.archive_name()})…")
    try:
        asyncio.run(manager.install())
    except GrafanaError as exc:
        print(f"Не удалось: {exc}", file=sys.stderr)
        return 1
    print(f"Готово: {manager.install_dir}")
    return 0


def main() -> int:
    args = parse_args()
    setup_logging(args.log_level, args.log_file)

    settings: Settings = args._settings
    if args.install_grafana:
        return install_grafana(settings)
    settings.server.host = args.host
    settings.server.port = args.port
    if args.no_browser:
        settings.server.open_browser = False
    settings.save()

    shown = "localhost" if args.host in {"127.0.0.1", "0.0.0.0"} else args.host
    url = f"http://{shown}:{args.port}"
    print()
    print(f"  plc2grafana {__version__}")
    print(f"  Интерфейс:  {url}")
    print(f"  API-доки:   {url}/api/docs")
    if args.host in {"0.0.0.0", "::"}:
        for ip in lan_addresses():
            print(f"  По сети:    http://{ip}:{args.port}")
    print("  Остановка:  Ctrl+C")
    # Под systemd и Планировщиком stdout буферизуется — без flush адреса
    # появились бы в журнале только при остановке
    print(flush=True)

    if settings.server.open_browser and not args.reload:
        open_browser_later(url)

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
        access_log=False,
        # Логированием управляем сами: uvicorn пишет в те же обработчики,
        # включая файл из --log-file
        log_config=None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
