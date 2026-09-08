"""Проверка подготовки тегов и устройств перед записью в БД."""

from __future__ import annotations

import json

import pytest

from app.registry import RegistryError, _prepare_tag, _validate_device


def prepare(**overrides) -> dict:
    raw = {"name": "temp", "register": "holding", "address": 10, "datatype": "float32"}
    raw.update(overrides)
    return _prepare_tag(raw, "plc-01", 1)


# --- теги -------------------------------------------------------------------
def test_prepare_tag_fills_columns():
    row = prepare(unit="°C", description="Температура")
    assert row["device"] == "plc-01"
    assert row["device_id"] == 1
    assert row["datatype"] == "float32"
    assert row["address"] == 10
    assert row["unit"] == "°C"
    assert json.loads(row["labels"]) == {}


def test_min_max_renamed_to_db_columns():
    row = prepare(min_value=-50, max_value=400)
    assert row["min_value"] == -50
    assert row["max_value"] == 400


def test_invalid_datatype_rejected():
    with pytest.raises(RegistryError):
        prepare(datatype="float24")


def test_bit_requires_bool():
    with pytest.raises(RegistryError):
        prepare(datatype="uint16", bit=3)


def test_string_requires_length():
    with pytest.raises(RegistryError):
        prepare(datatype="string")
    assert prepare(datatype="string", length=8)["length"] == 8


def test_coil_must_be_bool():
    with pytest.raises(RegistryError):
        prepare(register="coil", datatype="float32")


def test_too_small_poll_interval_rejected():
    with pytest.raises(RegistryError):
        prepare(poll_interval_ms=5)


def test_role_guessed_for_alarm_bits():
    assert prepare(name="pump_fault", datatype="bool", bit=3)["role"] == "alarm"
    assert prepare(name="pump_run", datatype="bool", bit=0)["role"] == "state"
    assert prepare(name="total_count", datatype="uint32")["role"] == "counter"
    assert prepare(name="temp")["role"] == "value"


def test_explicit_role_wins_over_guess():
    assert prepare(name="pump_fault", datatype="bool", bit=3,
                   role="state")["role"] == "state"


def test_name_charset_is_validated():
    with pytest.raises(RegistryError):
        prepare(name="темп ратура")


# --- устройства -------------------------------------------------------------
def test_tcp_device_requires_host():
    with pytest.raises(RegistryError):
        _validate_device({"transport": "tcp", "port": 502})
    _validate_device({"transport": "tcp", "host": "192.168.0.10", "port": 502})


def test_rtu_device_requires_serial_port():
    with pytest.raises(RegistryError):
        _validate_device({"transport": "rtu"})
    _validate_device({"transport": "rtu", "serial_port": "COM3"})


def test_unit_id_range_checked():
    with pytest.raises(RegistryError):
        _validate_device({"transport": "tcp", "host": "h", "unit_id": 300})


def test_port_range_checked():
    with pytest.raises(RegistryError):
        _validate_device({"transport": "tcp", "host": "h", "port": 0})


def test_partial_update_does_not_demand_host():
    # PATCH меняет только таймаут — адрес трогать не обязаны
    _validate_device({"transport": "tcp", "timeout_s": 5}, partial=True)
