#!/usr/bin/env python3
"""Запуск приложения одной командой:

    python run.py                 # веб-интерфейс на http://127.0.0.1:8000
    python run.py --port 8080
    python run.py --host 0.0.0.0  # доступ с других машин в сети
    python run.py --no-browser
"""

from __future__ import annotations

import argparse
import logging
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


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=args.log_level.upper(), format=LOG_FORMAT)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    settings: Settings = args._settings
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
    print("  Остановка:  Ctrl+C")
    print()

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
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
