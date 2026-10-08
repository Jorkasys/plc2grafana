"""Логика, от которой зависит развёртывание на сервере и в docker."""

from __future__ import annotations

import pytest

from app.db import split_sql, _read_sql
from app.grafana.manager import GrafanaManager
from app.settings import GrafanaSettings, Settings, ServerSettings


# --- адрес Grafana для браузера ---------------------------------------------
@pytest.mark.parametrize("external,public,expected", [
    # Встроенная Grafana: браузер берёт свой хост, так работает доступ по сети
    ("", "", ""),
    # docker: имя сервиса понятно только серверу
    ("http://grafana:3000", "", ""),
    ("http://127.0.0.1:3000", "", ""),
    ("http://localhost:3000", "", ""),
    # Настоящая внешняя Grafana с нормальным именем — её адрес и отдаём
    ("https://grafana.plant.local", "", "https://grafana.plant.local"),
    # Явно заданный публичный адрес важнее всего
    ("http://grafana:3000", "https://gf.example.com/", "https://gf.example.com"),
    ("", "http://10.0.0.5:3001", "http://10.0.0.5:3001"),
])
def test_browser_url(external, public, expected):
    assert GrafanaSettings(external_url=external, public_url=public).browser_url == expected


def manager(server_host: str, bind: str = "") -> GrafanaManager:
    # Без конструктора: он ищет Grafana прошлого запуска, тестам это не нужно
    m = GrafanaManager.__new__(GrafanaManager)
    m.settings = Settings(server=ServerSettings(host=server_host),
                          grafana=GrafanaSettings(bind_host=bind))
    return m


def test_grafana_follows_web_interface_host():
    # Интерфейс открыт в сеть — Grafana тоже, иначе дашборд в iframe не загрузится
    m = manager("0.0.0.0")
    assert m.bind_host == "0.0.0.0"
    assert m.exposed
    assert m.api_url == "http://127.0.0.1:3000"


def test_grafana_stays_local_by_default():
    m = manager("127.0.0.1")
    assert m.bind_host == "127.0.0.1"
    assert not m.exposed


def test_explicit_bind_overrides_server_host():
    m = manager("0.0.0.0", bind="127.0.0.1")
    assert m.bind_host == "127.0.0.1"
    assert not m.exposed


def test_specific_interface_is_used_for_health_checks():
    # Grafana слушает только этот адрес — на 127.0.0.1 её не достать
    m = manager("192.168.1.10")
    assert m.api_url == "http://192.168.1.10:3000"


# --- переменные окружения ---------------------------------------------------
def test_env_overrides(monkeypatch):
    monkeypatch.setenv("PLC_GRAFANA_BIND", "0.0.0.0")
    monkeypatch.setenv("PLC_GRAFANA_PUBLIC_URL", "https://gf.example.com")
    monkeypatch.setenv("PLC_GRAFANA_AUTOSTART", "no")
    settings = Settings()
    settings.apply_env()
    assert settings.grafana.bind_host == "0.0.0.0"
    assert settings.grafana.public_url == "https://gf.example.com"
    assert settings.grafana.autostart is False


# --- схема TimescaleDB по одному оператору ----------------------------------
def test_split_sql_separates_statements_and_drops_comments():
    sql = """
    -- комментарий; с точкой с запятой
    CREATE TABLE a (x int);   -- хвост
    SELECT 1;

    SELECT 2
    """
    assert split_sql(sql) == ["CREATE TABLE a (x int)", "SELECT 1", "SELECT 2"]


def test_timescale_schema_splits_into_separate_statements():
    statements = split_sql(_read_sql("002_timescale.sql"))
    # Непрерывные агрегаты нельзя создавать внутри транзакции — поэтому важно,
    # что каждый CREATE MATERIALIZED VIEW идёт отдельным запросом
    views = [s for s in statements if s.startswith("CREATE MATERIALIZED VIEW")]
    assert len(views) == 2
    assert all(";" not in s for s in statements)
    assert statements[0].startswith("SELECT create_hypertable")
