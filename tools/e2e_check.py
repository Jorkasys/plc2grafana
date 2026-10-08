#!/usr/bin/env python3
"""Сквозная проверка развёрнутого стека: от опроса устройства до SQL в Grafana.

Создаёт тестовое устройство «e2e-plc», поэтому запускайте на стенде или в CI,
а не на рабочей базе (флаг --cleanup удалит устройство в конце).

    python tools/simulator.py --port 5020 &
    python tools/e2e_check.py --app http://127.0.0.1:8000 --plc-host 127.0.0.1

Только стандартная библиотека — работает системным Python без зависимостей.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

DEVICE = "e2e-plc"


class CheckFailed(Exception):
    pass


def request(method: str, url: str, body=None, timeout: float = 60):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            text = response.read().decode() or "null"
            return response.status, json.loads(text)
    except urllib.error.HTTPError as exc:
        text = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(text)
        except json.JSONDecodeError:
            return exc.code, text


def wait_for(what: str, probe, timeout: float, interval: float = 2.0):
    """Повторять probe(), пока она не вернёт истинное значение."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = probe()
            if last:
                return last
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
            last = exc
        time.sleep(interval)
    raise CheckFailed(f"не дождались: {what} (последний ответ: {str(last)[:300]})")


def step(title: str) -> None:
    print(f"\n— {title}", flush=True)


def ok(text: str) -> None:
    print(f"  ✓ {text}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--app", default="http://127.0.0.1:8000")
    parser.add_argument("--grafana-url", default="http://127.0.0.1:3000",
                        help="адрес Grafana с этой машины")
    parser.add_argument("--plc-host", default="127.0.0.1",
                        help="адрес эмулятора, каким его видит приложение")
    parser.add_argument("--plc-port", type=int, default=5020)
    parser.add_argument("--external-grafana", action="store_true",
                        help="Grafana поднята отдельно (docker), а не приложением")
    parser.add_argument("--cleanup", action="store_true",
                        help="удалить тестовое устройство в конце")
    args = parser.parse_args()
    api = args.app.rstrip("/") + "/api"
    grafana = args.grafana_url.rstrip("/")

    try:
        step("Приложение отвечает")
        wait_for("/healthz", lambda: request("GET", f"{args.app}/healthz")[0] == 200, 120)
        ok(args.app)

        step("База данных подключена (автонастройка или мастер уже пройден)")
        status = wait_for(
            "database.connected",
            lambda: (lambda s: s if s[1]["database"]["connected"] else None)(
                request("GET", f"{api}/status"))[1], 240, 3)
        db = status["database"]
        ok(f"PostgreSQL {db['server_version']}, TimescaleDB: "
           f"{'да' if db['timescaledb'] else 'нет'}")

        step("Связь с устройством")
        code, probe = request("POST", f"{api}/discovery/test", {
            "host": args.plc_host, "port": args.plc_port, "unit_id": 1})
        if code != 200 or not probe.get("ok"):
            raise CheckFailed(f"устройство не отвечает: {probe}")
        ok(f"{probe['endpoint']}, отклик {probe['latency_ms']} мс")

        step("Сканирование и автоопределение тегов")
        code, scan = request("POST", f"{api}/discovery/scan", {
            "host": args.plc_host, "port": args.plc_port, "unit_id": 1,
            "register": "holding", "start": 100, "count": 14, "samples": 4})
        if code != 200:
            raise CheckFailed(f"сканирование: {scan}")
        chosen = [c for c in scan["candidates"] if c["selected"]]
        floats = [c for c in chosen if c["datatype"] == "float32"]
        if len(floats) < 3:
            raise CheckFailed(f"распознано слишком мало float32: {chosen}")
        ok(f"предложено тегов: {len(chosen)}, из них float32: {len(floats)}")

        step("Устройство и теги")
        _, devices = request("GET", f"{api}/devices")
        for device in devices:
            if device["name"] == DEVICE:
                request("DELETE", f"{api}/devices/{device['device_id']}")
        code, device = request("POST", f"{api}/devices", {
            "name": DEVICE, "host": args.plc_host, "port": args.plc_port,
            "unit_id": 1, "description": "сквозная проверка"})
        if code != 201:
            raise CheckFailed(f"создание устройства: {device}")
        tags = [{
            "name": f"e2e_{c['address']}", "register": c["register"],
            "address": c["address"], "datatype": c["datatype"],
            "word_order": c.get("word_order") or "big",
            "length": c.get("length"), "poll_interval_ms": 500,
        } for c in chosen]
        code, saved = request("PUT", f"{api}/devices/{device['device_id']}/tags",
                              {"tags": tags})
        if code != 200:
            raise CheckFailed(f"сохранение тегов: {saved}")
        ok(f"{DEVICE}: тегов {saved['tags']}")

        step("Значения пишутся в базу")

        def good_values():
            _, rows = request("GET", f"{api}/latest?device_id={device['device_id']}")
            good = [r for r in rows if r["quality"] == 0 and r["value"] is not None]
            return good if len(good) >= len(tags) else None

        good = wait_for("GOOD-значения по всем тегам", good_values, 60)
        ok(f"GOOD: {len(good)} из {len(tags)}")
        ids = ",".join(str(r["tag_id"]) for r in good[:3])
        wait_for("история в БД", lambda: any(
            len(points) >= 2 for points in
            request("GET", f"{api}/series?tag_ids={ids}&minutes=5")[1]["series"].values()), 60)
        ok("история читается через /api/series")

        step("Grafana")
        if args.external_grafana:
            wait_for("внешняя Grafana", lambda: request(
                "GET", f"{api}/grafana/status")[1].get("healthy"), 180, 3)
            ok("внешняя Grafana отвечает")
        else:
            gstatus = request("GET", f"{api}/grafana/status")[1]
            if not gstatus["installed"]:
                request("POST", f"{api}/grafana/install")
                wait_for("установка Grafana", lambda: request(
                    "GET", f"{api}/grafana/status")[1]["installed"], 600, 5)
                ok("скачана и распакована")
            # Сразу после старта приложения Grafana может уже подниматься
            # автозапуском — тогда просто дожидаемся её
            wait_for("запуск Grafana", lambda: (
                request("GET", f"{api}/grafana/status")[1].get("healthy")
                or (request("POST", f"{api}/grafana/start", timeout=120)[0] == 200
                    and request("GET", f"{api}/grafana/status")[1].get("healthy"))),
                180, 5)
            ok("встроенная Grafana запущена")

        code, generated = request("POST", f"{api}/grafana/dashboards")
        if code != 200 or not generated["dashboards"]:
            raise CheckFailed(f"генерация дашбордов: {generated}")
        ok(f"сгенерировано дашбордов: {len(generated['dashboards'])}")

        listed = wait_for("дашборды в Grafana (провайдер перечитывает раз в 10 с)",
                          lambda: request("GET", f"{api}/grafana/dashboards")[1]["dashboards"],
                          90, 5)
        if not all(d.get("embed_path", "").startswith("/d/") for d in listed):
            raise CheckFailed(f"неверные пути для встраивания: {listed}")
        ok(f"Grafana видит дашбордов: {len(listed)}; пути для браузера — относительные")

        step("SQL через датасорс Grafana (роль только на чтение)")
        code, result = request("POST", f"{grafana}/api/ds/query", {
            "from": "now-15m", "to": "now",
            "queries": [{
                "refId": "A",
                "datasource": {"type": "grafana-postgresql-datasource", "uid": "plc-postgres"},
                "rawSql": "SELECT count(*)::float AS n FROM tag_value",
                "format": "table",
            }],
        })
        if code != 200:
            raise CheckFailed(f"запрос через Grafana: {code} {result}")
        frame = result["results"]["A"]["frames"][0]
        rows = frame["data"]["values"][0][0]
        if not rows:
            raise CheckFailed(f"Grafana не видит данных: {result}")
        ok(f"Grafana читает tag_value: {int(rows)} строк")

        if args.cleanup:
            request("DELETE", f"{api}/devices/{device['device_id']}")
            ok("тестовое устройство удалено")
    except CheckFailed as exc:
        print(f"\n✗ ПРОВАЛ: {exc}", file=sys.stderr)
        return 1

    print("\nВсё работает.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
