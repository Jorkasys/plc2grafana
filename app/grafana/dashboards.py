"""Генерация дашбордов Grafana по карте тегов.

Дашборды не пишутся руками: приложение знает типы тегов, единицы измерения,
границы и место машины в иерархии — этого достаточно, чтобы собрать
осмысленный дашборд на каждое устройство и на каждую линию.

Файлы кладутся в ``runtime/grafana-dashboards``; provisioning-провайдер
Grafana подхватывает изменения сам, перезапуск не нужен.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("app.grafana.dashboards")

DS = {"type": "postgres", "uid": "plc-postgres"}

# Единицы измерения → идентификаторы Grafana. Всё остальное показываем
# суффиксом как есть: suffix:кПа, suffix:л/ч и т. п.
UNIT_MAP = {
    "°c": "celsius", "c": "celsius", "°f": "fahrenheit", "k": "kelvin",
    "bar": "pressurebar", "па": "pressurepa", "pa": "pressurepa",
    "кпа": "pressurekpa", "kpa": "pressurekpa", "мпа": "pressurehpa",
    "a": "amp", "а": "amp", "ma": "amp", "ма": "amp",
    "v": "volt", "в": "volt", "kv": "kvolt",
    "w": "watt", "вт": "watt", "kw": "kwatt", "квт": "kwatt",
    "kwh": "kwatth", "квтч": "kwatth",
    "%": "percent",
    "hz": "hertz", "гц": "hertz",
    "rpm": "rotrpm", "об/мин": "rotrpm",
    "s": "s", "с": "s", "ms": "ms", "мс": "ms",
    "kg": "kg", "кг": "kg", "t": "kg", "т": "kg",
    "m": "lengthm", "м": "lengthm", "mm": "lengthmm", "мм": "lengthmm",
    "l": "litre", "л": "litre", "m3": "m3", "м3": "m3",
    "шт": "short", "pcs": "short",
}

QUALITY_MAPPINGS = [{
    "type": "value",
    "options": {
        "0": {"text": "GOOD", "color": "green", "index": 0},
        "1": {"text": "НЕТ СВЯЗИ", "color": "red", "index": 1},
        "2": {"text": "ВНЕ ГРАНИЦ", "color": "orange", "index": 2},
        "3": {"text": "ОШИБКА ДЕКОДА", "color": "purple", "index": 3},
        "4": {"text": "УСТАРЕЛО", "color": "yellow", "index": 4},
    },
}]


# ---------------------------------------------------------------------------
#  Точка входа
# ---------------------------------------------------------------------------
def generate_all(dashboard_dir: Path, devices: list[dict], tags: list[dict],
                 nodes: list[dict]) -> list[dict[str, Any]]:
    """Перегенерировать все дашборды. Возвращает список созданных файлов."""
    dashboard_dir.mkdir(parents=True, exist_ok=True)
    for stale in dashboard_dir.glob("plc-*.json"):
        stale.unlink()

    by_device: dict[int, list[dict]] = {}
    for tag in tags:
        by_device.setdefault(tag["device_id"], []).append(tag)

    written: list[dict[str, Any]] = []
    for device in devices:
        device_tags = by_device.get(device["device_id"], [])
        if not device_tags:
            continue
        dash = device_dashboard(device, device_tags)
        written.append(_write(dashboard_dir, f"plc-device-{device['device_id']}", dash))

    lines = [n for n in nodes if n["kind"] in {"line", "area", "site"}]
    for node in lines:
        node_tags = _tags_under(node, nodes, tags)
        if not node_tags:
            continue
        dash = line_dashboard(node, nodes, node_tags)
        written.append(_write(dashboard_dir, f"plc-node-{node['node_id']}", dash))

    if tags:
        written.append(_write(dashboard_dir, "plc-overview",
                              overview_dashboard(devices, tags, nodes)))
    log.info("сгенерировано дашбордов: %d", len(written))
    return written


def _write(directory: Path, slug: str, dashboard: dict) -> dict[str, Any]:
    path = directory / f"{slug}.json"
    path.write_text(json.dumps(dashboard, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"file": path.name, "title": dashboard["title"], "uid": dashboard["uid"]}


# ---------------------------------------------------------------------------
#  Дашборд устройства
# ---------------------------------------------------------------------------
def device_dashboard(device: dict, tags: list[dict]) -> dict[str, Any]:
    layout = _Layout()
    panels: list[dict] = []

    device_name = device["name"]
    analog = [t for t in tags if t["datatype"] not in {"bool", "string"}]
    booleans = [t for t in tags if t["datatype"] == "bool"]
    strings = [t for t in tags if t["datatype"] == "string"]

    panels.append(_row("Состояние", layout))
    panels += _health_panels(device_name, tags, layout)

    if analog:
        panels.append(_row("Аналоговые сигналы", layout))
        for group, group_tags in _group(analog):
            panels.append(_timeseries(group, group_tags, layout))

    if booleans:
        panels.append(_row("Дискретные сигналы", layout))
        panels.append(_state_timeline("Состояния и аварии", booleans, layout))

    panels.append(_row("Текущие значения", layout))
    panels.append(_latest_table(f"t.device = {_sql_str(device_name)}", layout, height=10))
    if strings:
        panels.append(_latest_table(
            f"t.device = {_sql_str(device_name)} AND t.datatype = 'string'",
            layout, title="Текстовые теги", height=6))

    return _dashboard(
        uid=f"plc-dev-{device['device_id']}",
        title=f"{device_name} — обзор устройства",
        description=device.get("description") or "",
        tags=["plc2grafana", "device", device_name],
        panels=panels,
    )


def _health_panels(device_name: str, tags: list[dict], layout: "_Layout") -> list[dict]:
    where = f"t.device = {_sql_str(device_name)}"
    stats = [
        ("Тегов в опросе", f"SELECT count(*)::float AS value FROM tag t WHERE {where}",
         "short", None),
        ("Годных значений",
         "SELECT count(*) FILTER (WHERE l.quality = 0)::float AS value "
         f"FROM v_tag_latest l JOIN tag t USING (tag_id) WHERE {where}", "short", None),
        ("Проблемных значений",
         "SELECT count(*) FILTER (WHERE l.quality <> 0)::float AS value "
         f"FROM v_tag_latest l JOIN tag t USING (tag_id) WHERE {where}", "short", "warn"),
        ("Возраст данных, с",
         "SELECT COALESCE(EXTRACT(EPOCH FROM min(now() - l.ts)), 0)::float AS value "
         f"FROM v_tag_latest l JOIN tag t USING (tag_id) WHERE {where}", "s", "age"),
    ]
    panels = []
    for title, sql, unit, mode in stats:
        thresholds = {"mode": "absolute", "steps": [{"color": "green", "value": None}]}
        if mode == "warn":
            thresholds["steps"].append({"color": "red", "value": 1})
        elif mode == "age":
            thresholds["steps"] += [{"color": "yellow", "value": 30},
                                    {"color": "red", "value": 120}]
        panels.append({
            "type": "stat",
            "title": title,
            "datasource": DS,
            "gridPos": layout.place(6, 4),
            "fieldConfig": {
                "defaults": {"unit": unit, "thresholds": thresholds,
                             "mappings": [], "decimals": 0},
                "overrides": [],
            },
            "options": {
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                "colorMode": "value", "graphMode": "none",
                "textMode": "auto", "justifyMode": "auto",
            },
            "targets": [_target(sql, fmt="table")],
        })
    return panels


# ---------------------------------------------------------------------------
#  Дашборд линии/участка и общий обзор
# ---------------------------------------------------------------------------
def line_dashboard(node: dict, nodes: list[dict], tags: list[dict]) -> dict[str, Any]:
    layout = _Layout()
    panels: list[dict] = []
    children = _children(node["node_id"], nodes)
    machines = [c for c in children if c["kind"] == "machine"] or [node]

    panels.append(_row("Машины линии", layout))
    for machine in machines:
        machine_tags = [t for t in tags if t.get("node_id") == machine["node_id"]]
        if not machine_tags:
            continue
        panels.append(_machine_stat(machine, machine_tags, layout))

    analog = [t for t in tags if t["datatype"] not in {"bool", "string"}]
    if analog:
        panels.append(_row("Ключевые показатели", layout))
        for group, group_tags in _group(analog):
            panels.append(_timeseries(group, group_tags, layout))

    booleans = [t for t in tags if t["datatype"] == "bool"]
    if booleans:
        panels.append(_row("Состояния и аварии", layout))
        panels.append(_state_timeline("Дискретные сигналы", booleans, layout))

    tag_ids = [t["tag_id"] for t in tags]
    panels.append(_row("Текущие значения", layout))
    panels.append(_latest_table(f"t.tag_id = ANY(ARRAY{_sql_ids(tag_ids)})", layout,
                                height=10))

    return _dashboard(
        uid=f"plc-node-{node['node_id']}",
        title=f"{node['name']} — {_kind_name(node['kind'])}",
        description=node.get("description") or "",
        tags=["plc2grafana", "twin", node["kind"]],
        panels=panels,
    )


def overview_dashboard(devices: list[dict], tags: list[dict],
                       nodes: list[dict]) -> dict[str, Any]:
    layout = _Layout()
    panels: list[dict] = []

    panels.append(_row("Производство в целом", layout))
    panels.append({
        "type": "stat",
        "title": "Устройств на связи",
        "datasource": DS,
        "gridPos": layout.place(6, 4),
        "fieldConfig": {"defaults": {
            "unit": "short",
            "thresholds": {"mode": "absolute", "steps": [
                {"color": "red", "value": None}, {"color": "green", "value": 1}]},
        }, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "",
                                      "values": False},
                    "colorMode": "value", "graphMode": "none", "textMode": "auto"},
        "targets": [_target(
            "SELECT count(DISTINCT t.device)::float AS value "
            "FROM v_tag_latest l JOIN tag t USING (tag_id) "
            "WHERE l.quality = 0 AND l.ts > now() - interval '2 minutes'", fmt="table")],
    })
    panels.append({
        "type": "stat",
        "title": "Активных аварий",
        "datasource": DS,
        "gridPos": layout.place(6, 4),
        "fieldConfig": {"defaults": {
            "unit": "short",
            "thresholds": {"mode": "absolute", "steps": [
                {"color": "green", "value": None}, {"color": "red", "value": 1}]},
        }, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "",
                                      "values": False},
                    "colorMode": "background", "graphMode": "none", "textMode": "auto"},
        "targets": [_target(
            "SELECT count(*)::float AS value FROM v_tag_latest l "
            "JOIN tag t USING (tag_id) WHERE t.role = 'alarm' AND l.value > 0",
            fmt="table")],
    })
    panels.append({
        "type": "stat",
        "title": "Тегов всего",
        "datasource": DS,
        "gridPos": layout.place(6, 4),
        "fieldConfig": {"defaults": {"unit": "short"}, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "",
                                      "values": False},
                    "colorMode": "none", "graphMode": "none", "textMode": "auto"},
        "targets": [_target("SELECT count(*)::float AS value FROM tag WHERE enabled",
                            fmt="table")],
    })
    panels.append({
        "type": "stat",
        "title": "Записей за час",
        "datasource": DS,
        "gridPos": layout.place(6, 4),
        "fieldConfig": {"defaults": {"unit": "short"}, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "",
                                      "values": False},
                    "colorMode": "none", "graphMode": "none", "textMode": "auto"},
        "targets": [_target(
            "SELECT count(*)::float AS value FROM tag_value "
            "WHERE ts > now() - interval '1 hour'", fmt="table")],
    })

    alarms = [t for t in tags if t.get("role") == "alarm"]
    if alarms:
        panels.append(_row("Аварии", layout))
        panels.append(_state_timeline("Лента аварий", alarms, layout, height=8))

    counters = [t for t in tags if t.get("role") == "counter"]
    if counters:
        panels.append(_row("Выпуск", layout))
        panels.append(_timeseries("Счётчики выпуска", counters, layout, height=8))

    panels.append(_row("Все текущие значения", layout))
    panels.append(_latest_table("true", layout, height=12))

    return _dashboard(
        uid="plc-overview",
        title="Цифровой двойник — обзор производства",
        description="Сводка по всем устройствам, линиям и машинам",
        tags=["plc2grafana", "overview"],
        panels=panels,
    )


def _machine_stat(machine: dict, tags: list[dict], layout: "_Layout") -> dict:
    tag_ids = [t["tag_id"] for t in tags]
    return {
        "type": "stat",
        "title": machine["name"],
        "datasource": DS,
        "gridPos": layout.place(6, 5),
        "fieldConfig": {"defaults": {
            "mappings": [], "unit": "short", "decimals": 2,
            "thresholds": {"mode": "absolute", "steps": [{"color": "text", "value": None}]},
        }, "overrides": []},
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": True},
            "colorMode": "value", "graphMode": "area", "textMode": "value_and_name",
            "orientation": "horizontal",
        },
        "targets": [_target(
            "SELECT l.name AS metric, l.value FROM v_tag_latest l "
            f"WHERE l.tag_id = ANY(ARRAY{_sql_ids(tag_ids)}) AND l.value IS NOT NULL "
            "ORDER BY l.name LIMIT 6", fmt="table")],
    }


# ---------------------------------------------------------------------------
#  Кирпичики панелей
# ---------------------------------------------------------------------------
def _timeseries(title: str, tags: list[dict], layout: "_Layout",
                height: int = 9) -> dict:
    tag_ids = [t["tag_id"] for t in tags]
    unit = _unit_of(tags)
    sql = (
        "SELECT\n"
        "  $__timeGroupAlias(v.ts, $__interval),\n"
        "  t.name AS metric,\n"
        "  avg(v.value) AS value\n"
        "FROM tag_value v\n"
        "JOIN tag t USING (tag_id)\n"
        f"WHERE $__timeFilter(v.ts) AND v.quality = 0\n"
        f"  AND t.tag_id = ANY(ARRAY{_sql_ids(tag_ids)})\n"
        "GROUP BY 1, 2\n"
        "ORDER BY 1"
    )
    overrides = []
    for tag in tags:
        if tag.get("min_value") is None and tag.get("max_value") is None:
            continue
        props = []
        if tag.get("min_value") is not None:
            props.append({"id": "min", "value": tag["min_value"]})
        if tag.get("max_value") is not None:
            props.append({"id": "max", "value": tag["max_value"]})
        overrides.append({
            "matcher": {"id": "byName", "options": tag["name"]},
            "properties": props,
        })
    return {
        "type": "timeseries",
        "title": title,
        "datasource": DS,
        "gridPos": layout.place(12, height),
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {
                    "drawStyle": "line", "lineWidth": 2, "fillOpacity": 8,
                    "showPoints": "never", "spanNulls": True,
                    "lineInterpolation": "linear",
                    "scaleDistribution": {"type": "linear"},
                    "axisPlacement": "auto", "gradientMode": "opacity",
                },
                "color": {"mode": "palette-classic"},
            },
            "overrides": overrides,
        },
        "options": {
            "legend": {"displayMode": "table", "placement": "bottom",
                       "showLegend": True,
                       "calcs": ["lastNotNull", "min", "max", "mean"]},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
        "targets": [_target(sql)],
    }


def _state_timeline(title: str, tags: list[dict], layout: "_Layout",
                    height: int = 9) -> dict:
    tag_ids = [t["tag_id"] for t in tags]
    sql = (
        "SELECT\n"
        "  $__timeGroupAlias(v.ts, $__interval),\n"
        "  t.name AS metric,\n"
        "  max(v.value) AS value\n"
        "FROM tag_value v\n"
        "JOIN tag t USING (tag_id)\n"
        "WHERE $__timeFilter(v.ts)\n"
        f"  AND t.tag_id = ANY(ARRAY{_sql_ids(tag_ids)})\n"
        "GROUP BY 1, 2\n"
        "ORDER BY 1"
    )
    return {
        "type": "state-timeline",
        "title": title,
        "datasource": DS,
        "gridPos": layout.place(24, height),
        "fieldConfig": {
            "defaults": {
                "custom": {"lineWidth": 0, "fillOpacity": 80},
                "mappings": [{
                    "type": "value",
                    "options": {
                        "0": {"text": "выкл", "color": "#4a5568", "index": 0},
                        "1": {"text": "вкл", "color": "green", "index": 1},
                    },
                }],
                "color": {"mode": "thresholds"},
                "thresholds": {"mode": "absolute", "steps": [
                    {"color": "#4a5568", "value": None},
                    {"color": "green", "value": 1}]},
            },
            "overrides": [{
                "matcher": {"id": "byName", "options": tag["name"]},
                "properties": [{"id": "thresholds", "value": {
                    "mode": "absolute",
                    "steps": [{"color": "#4a5568", "value": None},
                              {"color": "red", "value": 1}]}}],
            } for tag in tags if tag.get("role") == "alarm"],
        },
        "options": {
            "showValue": "never", "mergeValues": True, "rowHeight": 0.9,
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "single"},
        },
        "targets": [_target(sql)],
    }


def _latest_table(where: str, layout: "_Layout", title: str = "Текущие значения",
                  height: int = 10) -> dict:
    sql = (
        "SELECT\n"
        "  t.device AS \"Устройство\",\n"
        "  t.name AS \"Тег\",\n"
        "  COALESCE(t.description, '') AS \"Описание\",\n"
        "  COALESCE(l.value, 0) AS \"Значение\",\n"
        "  COALESCE(t.unit, '') AS \"Ед.\",\n"
        "  l.quality AS \"Качество\",\n"
        "  EXTRACT(EPOCH FROM (now() - l.ts)) AS \"Возраст, с\"\n"
        "FROM v_tag_latest l\n"
        "JOIN tag t USING (tag_id)\n"
        f"WHERE {where}\n"
        "ORDER BY 1, 2"
    )
    return {
        "type": "table",
        "title": title,
        "datasource": DS,
        "gridPos": layout.place(24, height),
        "fieldConfig": {
            "defaults": {"custom": {"align": "auto", "filterable": True}},
            "overrides": [
                {"matcher": {"id": "byName", "options": "Качество"},
                 "properties": [
                     {"id": "mappings", "value": QUALITY_MAPPINGS},
                     {"id": "custom.cellOptions",
                      "value": {"type": "color-background", "mode": "basic"}},
                     {"id": "custom.width", "value": 140},
                 ]},
                {"matcher": {"id": "byName", "options": "Возраст, с"},
                 "properties": [
                     {"id": "unit", "value": "s"},
                     {"id": "decimals", "value": 1},
                     {"id": "custom.width", "value": 120},
                     {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                         {"color": "green", "value": None},
                         {"color": "yellow", "value": 30},
                         {"color": "red", "value": 120}]}},
                     {"id": "custom.cellOptions",
                      "value": {"type": "color-text"}},
                 ]},
                {"matcher": {"id": "byName", "options": "Значение"},
                 "properties": [{"id": "decimals", "value": 3}]},
            ],
        },
        "options": {"showHeader": True, "footer": {"show": False},
                    "sortBy": [{"displayName": "Устройство", "desc": False}]},
        "targets": [_target(sql, fmt="table")],
    }


def _row(title: str, layout: "_Layout") -> dict:
    return {
        "type": "row", "title": title, "collapsed": False,
        "gridPos": layout.row(), "panels": [],
    }


def _target(sql: str, fmt: str = "time_series") -> dict:
    return {
        "refId": "A", "format": fmt, "rawQuery": True, "rawSql": sql,
        "editorMode": "code", "datasource": DS,
    }


def _dashboard(uid: str, title: str, description: str, tags: list[str],
               panels: list[dict]) -> dict[str, Any]:
    for i, panel in enumerate(panels, start=1):
        panel["id"] = i
    return {
        "uid": uid,
        "title": title,
        "description": description,
        "tags": tags,
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "editable": True,
        "refresh": "5s",
        "time": {"from": "now-30m", "to": "now"},
        "timepicker": {"refresh_intervals": ["1s", "5s", "10s", "30s", "1m", "5m"]},
        "graphTooltip": 1,
        "panels": panels,
        "annotations": {"list": []},
        "templating": {"list": []},
    }


class _Layout:
    """Раскладка панелей по сетке Grafana: 24 колонки, слева направо."""

    def __init__(self) -> None:
        self.x = 0
        self.y = 0
        self._row_height = 0

    def place(self, w: int, h: int) -> dict[str, int]:
        if self.x + w > 24:
            self._newline()
        pos = {"h": h, "w": w, "x": self.x, "y": self.y}
        self.x += w
        self._row_height = max(self._row_height, h)
        if self.x >= 24:
            self._newline()
        return pos

    def row(self) -> dict[str, int]:
        if self.x:
            self._newline()
        pos = {"h": 1, "w": 24, "x": 0, "y": self.y}
        self.y += 1
        return pos

    def _newline(self) -> None:
        self.y += self._row_height
        self.x = 0
        self._row_height = 0


# ---------------------------------------------------------------------------
#  Вспомогательное
# ---------------------------------------------------------------------------
def _group(tags: list[dict]) -> list[tuple[str, list[dict]]]:
    """Разложить теги по панелям: по явной группе, иначе по единице измерения."""
    groups: dict[str, list[dict]] = {}
    for tag in tags:
        key = tag.get("group_name") or (
            f"Сигналы, {tag['unit']}" if tag.get("unit") else "Сигналы без единиц")
        groups.setdefault(key, []).append(tag)

    # Слишком много линий на одной панели читать невозможно — режем по 8
    result: list[tuple[str, list[dict]]] = []
    for name, items in sorted(groups.items()):
        if len(items) <= 8:
            result.append((name, items))
            continue
        for i in range(0, len(items), 8):
            chunk = items[i:i + 8]
            suffix = f" ({i // 8 + 1})" if len(items) > 8 else ""
            result.append((name + suffix, chunk))
    return result


def _unit_of(tags: list[dict]) -> str:
    units = {(t.get("unit") or "").strip() for t in tags}
    units.discard("")
    if len(units) != 1:
        return "short"
    unit = units.pop()
    return UNIT_MAP.get(unit.lower(), f"suffix:{unit}")


def _children(node_id: int, nodes: list[dict]) -> list[dict]:
    return [n for n in nodes if n.get("parent_id") == node_id]


def _descendants(node_id: int, nodes: list[dict]) -> set[int]:
    found = {node_id}
    frontier = [node_id]
    while frontier:
        current = frontier.pop()
        for child in _children(current, nodes):
            if child["node_id"] not in found:
                found.add(child["node_id"])
                frontier.append(child["node_id"])
    return found


def _tags_under(node: dict, nodes: list[dict], tags: list[dict]) -> list[dict]:
    ids = _descendants(node["node_id"], nodes)
    return [t for t in tags if t.get("node_id") in ids]


def _kind_name(kind: str) -> str:
    return {"site": "площадка", "area": "участок",
            "line": "линия", "machine": "машина"}.get(kind, kind)


def _sql_str(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _sql_ids(ids: Iterable[int]) -> str:
    safe = [int(i) for i in ids]
    return "[" + ",".join(str(i) for i in safe) + "]::int[]"


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-") or "item"
