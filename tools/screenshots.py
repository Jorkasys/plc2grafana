#!/usr/bin/env python3
"""Снимки интерфейса для README и витрины.

Поднимает headless-браузер (Chrome или Edge — тот, что найдётся), проходит по
страницам приложения, при необходимости кликает и сохраняет PNG в web/img.

    python tools/screenshots.py                       # всё
    python tools/screenshots.py --only monitor twin   # выборочно
    python tools/screenshots.py --app http://127.0.0.1:8000

Перед запуском должны работать: приложение с данными и (для снимков дашбордов)
Grafana. Проще всего — поднять эмулятор и пройти мастер подключения.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "web" / "img"

# Русский текст в консоли Windows иначе превращается в кракозябры:
# по умолчанию там cp866/cp1251, а не UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

WIDTH, HEIGHT = 1600, 1000


def find_browser() -> str:
    for candidate in BROWSERS:
        if Path(candidate).is_file():
            return candidate
    for name in ("chrome", "chromium", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    raise SystemExit("не найден Chrome или Edge — укажите путь через --browser")


class Devtools:
    """Минимальный клиент DevTools-протокола: навигация, клики, снимки."""

    def __init__(self, port: int = 9222) -> None:
        self.port = port
        self.socket = None
        self._id = 0

    async def attach(self) -> None:
        async with httpx.AsyncClient(timeout=20) as client:
            for _ in range(40):
                try:
                    response = await client.put(
                        f"http://127.0.0.1:{self.port}/json/new?about:blank")
                    if response.status_code == 405:      # старые сборки — GET
                        response = await client.get(
                            f"http://127.0.0.1:{self.port}/json/new?about:blank")
                    response.raise_for_status()
                    target = response.json()
                    break
                except Exception:  # noqa: BLE001 — браузер ещё поднимается
                    await asyncio.sleep(0.5)
            else:
                raise SystemExit("браузер не открыл порт отладки")
        self.socket = await websockets.connect(
            target["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024)
        await self.call("Page.enable")
        await self.call("Runtime.enable")

    async def call(self, method: str, **params):
        self._id += 1
        await self.socket.send(json.dumps({"id": self._id, "method": method,
                                           "params": params}))
        while True:
            message = json.loads(await self.socket.recv())
            if message.get("id") == self._id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})

    async def goto(self, url: str, settle: float = 3.0) -> None:
        await self.call("Page.navigate", url=url)
        await asyncio.sleep(settle)

    async def js(self, expression: str):
        result = await self.call("Runtime.evaluate", expression=expression,
                                 awaitPromise=True, returnByValue=True)
        return result.get("result", {}).get("value")

    async def click_text(self, text: str, settle: float = 1.0) -> bool:
        """Нажать первую кнопку/ссылку с таким текстом."""
        clicked = await self.js(f"""
            (() => {{
                const needle = {json.dumps(text)};
                const nodes = [...document.querySelectorAll('button, a, .step')];
                const target = nodes.find(n => n.textContent.trim().includes(needle));
                if (!target) return false;
                target.click();
                return true;
            }})()
        """)
        if clicked:
            await asyncio.sleep(settle)
        return bool(clicked)

    async def fill(self, selector: str, value: str) -> None:
        await self.js(f"""
            (() => {{
                const node = document.querySelector({json.dumps(selector)});
                if (!node) return false;
                node.value = {json.dumps(value)};
                node.dispatchEvent(new Event('input', {{bubbles: true}}));
                node.dispatchEvent(new Event('change', {{bubbles: true}}));
                return true;
            }})()
        """)

    async def shot(self, path: Path, full: bool = False) -> None:
        params = {"format": "png"}
        if full:
            metrics = await self.call("Page.getLayoutMetrics")
            size = metrics["cssContentSize"]
            params["clip"] = {"x": 0, "y": 0, "width": size["width"],
                              "height": min(size["height"], 4000), "scale": 1}
            params["captureBeyondViewport"] = True
        result = await self.call("Page.captureScreenshot", **params)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(result["data"]))
        print(f"  {path.relative_to(ROOT)}  ({path.stat().st_size // 1024} КБ)")


# ---------------------------------------------------------------------------
#  Сценарии
# ---------------------------------------------------------------------------
async def shot_monitor(dt: Devtools, app: str) -> None:
    await dt.goto(f"{app}/#/monitor", settle=6)
    # На пяти минутах видно форму сигналов, а не сплошную полосу колебаний
    await dt.js("""
        (() => {
            const picker = [...document.querySelectorAll('#topbar-actions select')].pop();
            if (!picker) return false;
            picker.value = '5';
            picker.dispatchEvent(new Event('change', {bubbles: true}));
            return true;
        })()
    """)
    await asyncio.sleep(6)
    await dt.shot(OUT_DIR / "monitor.png")


async def shot_twin(dt: Devtools, app: str) -> None:
    await dt.goto(f"{app}/#/twin", settle=5)
    await dt.shot(OUT_DIR / "twin.png")


async def shot_devices(dt: Devtools, app: str) -> None:
    await dt.goto(f"{app}/#/devices", settle=4)
    await dt.shot(OUT_DIR / "devices.png")


async def shot_system(dt: Devtools, app: str) -> None:
    await dt.goto(f"{app}/#/system", settle=4)
    await dt.shot(OUT_DIR / "system.png")


async def shot_wizard(dt: Devtools, app: str) -> None:
    """Мастер подключения до шага с предложенными тегами."""
    await dt.goto(f"{app}/#/connect", settle=3)
    inputs = "#view input"
    await dt.fill(f"{inputs}", "127.0.0.1")
    await dt.js("""
        (() => {
            const boxes = [...document.querySelectorAll('#view input')];
            boxes[0].value = '127.0.0.1';
            boxes[0].dispatchEvent(new Event('input', {bubbles: true}));
            boxes[1].value = '5020';
            boxes[1].dispatchEvent(new Event('input', {bubbles: true}));
            return true;
        })()
    """)
    if not await dt.click_text("Проверить связь", settle=6):
        raise SystemExit("не нашлась кнопка «Проверить связь»")
    await dt.shot(OUT_DIR / "wizard-connect.png")

    await dt.click_text("Дальше: сканировать регистры", settle=2)
    await dt.js("""
        (() => {
            const boxes = [...document.querySelectorAll('#view input')];
            boxes[0].value = '100';
            boxes[0].dispatchEvent(new Event('input', {bubbles: true}));
            boxes[1].value = '20';
            boxes[1].dispatchEvent(new Event('input', {bubbles: true}));
            return true;
        })()
    """)
    await dt.click_text("Сканировать", settle=10)
    await dt.click_text("Дальше: посмотреть теги", settle=3)
    await dt.shot(OUT_DIR / "wizard-tags.png")


async def shot_grafana_device(dt: Devtools, grafana: str) -> None:
    await dt.goto(f"{grafana}/d/plc-dev-1/?orgId=1&from=now-15m&to=now&kiosk", settle=12)
    await dt.shot(OUT_DIR / "grafana-device.png")


async def shot_grafana_overview(dt: Devtools, grafana: str) -> None:
    await dt.goto(f"{grafana}/d/plc-overview/?orgId=1&from=now-15m&to=now&kiosk", settle=12)
    await dt.shot(OUT_DIR / "grafana-overview.png")


SCENARIOS = {
    "monitor": ("app", shot_monitor),
    "twin": ("app", shot_twin),
    "devices": ("app", shot_devices),
    "system": ("app", shot_system),
    "wizard": ("app", shot_wizard),
    "grafana-device": ("grafana", shot_grafana_device),
    "grafana-overview": ("grafana", shot_grafana_overview),
}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", default="http://127.0.0.1:8000")
    parser.add_argument("--grafana", default="http://127.0.0.1:3000")
    parser.add_argument("--browser", default=None)
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--only", nargs="*", choices=sorted(SCENARIOS))
    args = parser.parse_args()

    browser = args.browser or find_browser()
    profile = tempfile.mkdtemp(prefix="plc-shots-")
    process = subprocess.Popen([  # noqa: S603
        browser,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        f"--remote-debugging-port={args.port}",
        f"--user-data-dir={profile}",
        f"--window-size={WIDTH},{HEIGHT}",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print(f"браузер: {browser}")
    devtools = Devtools(args.port)
    try:
        await devtools.attach()
        targets = args.only or list(SCENARIOS)
        for name in targets:
            base_key, fn = SCENARIOS[name]
            base = args.app if base_key == "app" else args.grafana
            print(f"снимок {name}…")
            await fn(devtools, base)
    finally:
        if devtools.socket is not None:
            await devtools.socket.close()
        process.terminate()
        try:
            process.wait(10)
        except Exception:  # noqa: BLE001
            process.kill()
        shutil.rmtree(profile, ignore_errors=True)
    print(f"готово: {OUT_DIR}")
    return 0


if __name__ == "__main__":
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.exit(asyncio.run(main()))
