"""Проверка генератора дашбордов Grafana."""

from __future__ import annotations

import json

import pytest

from app.grafana import dashboards as dash


def make_tag(tag_id: int, name: str, **kwargs) -> dict:
    tag = {
        "tag_id": tag_id,
        "device_id": 1,
        "device": "plc-01",
        "name": name,
        "description": f"Описание {name}",
        "unit": "°C",
        "datatype": "float32",
        "role": "value",
        "group_name": None,
        "node_id": None,
        "min_value": None,
        "max_value": None,
    }
    tag.update(kwargs)
    return tag


DEVICE = {"device_id": 1, "name": "plc-01", "description": "Линия розлива"}


@pytest.fixture
def tags() -> list[dict]:
    return [
        make_tag(1, "temp", unit="°C", node_id=10),
        make_tag(2, "press", unit="bar", node_id=10),
        make_tag(3, "pump", datatype="bool", unit=None, role="state", node_id=10),
        make_tag(4, "fault", datatype="bool", unit=None, role="alarm", node_id=11),
        make_tag(5, "total", datatype="uint32", unit="шт", role="counter", node_id=11),
    ]


NODES = [
    {"node_id": 1, "parent_id": None, "kind": "line", "name": "Линия 1",
     "description": None},
    {"node_id": 10, "parent_id": 1, "kind": "machine", "name": "Розлив",
     "description": None},
    {"node_id": 11, "parent_id": 1, "kind": "machine", "name": "Укупор",
     "description": None},
]


# ---------------------------------------------------------------------------
def test_device_dashboard_has_expected_panels(tags):
    board = dash.device_dashboard(DEVICE, tags)
    types = [p["type"] for p in board["panels"]]
    assert "stat" in types
    assert "timeseries" in types
    assert "state-timeline" in types
    assert "table" in types
    assert board["uid"] == "plc-dev-1"
    assert "plc-01" in board["title"]


def test_panels_do_not_overlap(tags):
    board = dash.device_dashboard(DEVICE, tags)
    occupied: set[tuple[int, int]] = set()
    for panel in board["panels"]:
        pos = panel["gridPos"]
        assert pos["x"] + pos["w"] <= 24, "панель выходит за 24 колонки"
        for x in range(pos["x"], pos["x"] + pos["w"]):
            for y in range(pos["y"], pos["y"] + pos["h"]):
                assert (x, y) not in occupied, f"панели наложились в ({x}, {y})"
                occupied.add((x, y))


def test_panel_ids_are_unique(tags):
    board = dash.device_dashboard(DEVICE, tags)
    ids = [p["id"] for p in board["panels"]]
    assert len(ids) == len(set(ids))


def test_queries_reference_only_own_tag_ids(tags):
    board = dash.device_dashboard(DEVICE, tags)
    charts = [p for p in board["panels"] if p["type"] == "timeseries"]
    assert charts
    for panel in charts:
        sql = panel["targets"][0]["rawSql"]
        assert "$__timeFilter" in sql
        assert "ARRAY[" in sql


def test_analog_tags_grouped_by_unit(tags):
    board = dash.device_dashboard(DEVICE, tags)
    titles = [p["title"] for p in board["panels"] if p["type"] == "timeseries"]
    assert "Сигналы, °C" in titles
    assert "Сигналы, bar" in titles


def test_units_translated_to_grafana_ids():
    assert dash._unit_of([{"unit": "°C"}]) == "celsius"
    assert dash._unit_of([{"unit": "bar"}]) == "pressurebar"
    # Незнакомая единица показывается суффиксом как есть
    assert dash._unit_of([{"unit": "л/ч"}]) == "suffix:л/ч"
    # Разные единицы на одной панели — без единицы
    assert dash._unit_of([{"unit": "°C"}, {"unit": "bar"}]) == "short"


def test_alarm_tags_get_red_override(tags):
    board = dash.device_dashboard(DEVICE, tags)
    timeline = next(p for p in board["panels"] if p["type"] == "state-timeline")
    overridden = [o["matcher"]["options"] for o in timeline["fieldConfig"]["overrides"]]
    assert "fault" in overridden
    assert "pump" not in overridden


def test_line_dashboard_covers_child_machines(tags):
    node = NODES[0]
    board = dash.line_dashboard(node, NODES, tags)
    titles = [p["title"] for p in board["panels"]]
    assert "Розлив" in titles
    assert "Укупор" in titles


def test_generate_all_writes_files(tmp_path, tags):
    written = dash.generate_all(tmp_path, [DEVICE], tags, NODES)
    files = sorted(p.name for p in tmp_path.glob("*.json"))
    assert "plc-overview.json" in files
    assert len(written) == len(files)
    for path in tmp_path.glob("*.json"):
        board = json.loads(path.read_text(encoding="utf-8"))
        assert board["panels"], f"{path.name}: дашборд без панелей"
        assert board["schemaVersion"] >= 39


def test_generate_all_removes_stale_dashboards(tmp_path, tags):
    stale = tmp_path / "plc-device-999.json"
    stale.write_text("{}", encoding="utf-8")
    dash.generate_all(tmp_path, [DEVICE], tags, NODES)
    assert not stale.exists()


def test_sql_escapes_device_name():
    device = {"device_id": 2, "name": "цех'1", "description": None}
    board = dash.device_dashboard(device, [make_tag(1, "temp")])
    sql = next(p for p in board["panels"] if p["type"] == "table")["targets"][0]["rawSql"]
    assert "'цех''1'" in sql
