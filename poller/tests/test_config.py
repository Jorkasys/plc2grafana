import textwrap

import pytest

from modbus_logger.config import ConfigError, load


def write(tmp_path, config_text, tags_text=None):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(textwrap.dedent(config_text), encoding="utf-8")
    if tags_text is not None:
        (tmp_path / "tags.yaml").write_text(textwrap.dedent(tags_text), encoding="utf-8")
    return cfg


BASE = """
    database:
      host: db
    devices:
      - name: plc-01
        host: 10.0.0.1
        tags:
          - name: temp
            address: 0
            register: holding
            datatype: float32
            unit: "°C"
"""


def test_minimal_config(tmp_path):
    cfg = load(write(tmp_path, BASE))
    assert cfg.devices[0].name == "plc-01"
    tag = cfg.devices[0].tags[0]
    assert tag.datatype == "float32"
    assert tag.count == 2
    assert cfg.database.host == "db"


def test_env_substitution(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_PLC_HOST", "192.168.5.5")
    cfg = load(write(tmp_path, """
        devices:
          - name: p
            host: ${MY_PLC_HOST:1.1.1.1}
            port: ${MY_PLC_PORT:5020}
            tags:
              - {name: a, address: 0, register: holding, datatype: int16}
    """))
    assert cfg.devices[0].host == "192.168.5.5"
    assert cfg.devices[0].port == 5020  # приведён к int


def test_tags_from_separate_file(tmp_path):
    cfg = load(write(tmp_path, """
        devices:
          - name: p
            host: 1.2.3.4
            tags_file: tags.yaml
    """, """
        tags:
          - {name: a, address: 0, register: holding, datatype: int16}
          - {name: b, address: 1, register: holding, datatype: int16}
    """))
    assert len(cfg.devices[0].tags) == 2


def test_defaults_inherited(tmp_path):
    cfg = load(write(tmp_path, """
        devices:
          - name: p
            host: 1.2.3.4
            defaults:
              poll_interval_ms: 250
              word_order: little
              deadband: 0.5
            tags:
              - {name: a, address: 0, register: holding, datatype: float32}
              - {name: b, address: 2, register: holding, datatype: float32, poll_interval_ms: 5000}
    """))
    a, b = cfg.devices[0].tags
    assert a.poll_interval_ms == 250
    assert a.word_order == "little"
    assert a.deadband == 0.5
    assert b.poll_interval_ms == 5000


def test_duplicate_tag_names_rejected(tmp_path):
    with pytest.raises(ConfigError, match="дублирующееся"):
        load(write(tmp_path, """
            devices:
              - name: p
                host: 1.2.3.4
                tags:
                  - {name: a, address: 0, register: holding, datatype: int16}
                  - {name: a, address: 1, register: holding, datatype: int16}
        """))


def test_bit_on_coil_rejected(tmp_path):
    with pytest.raises(ConfigError, match="bit"):
        load(write(tmp_path, """
            devices:
              - name: p
                host: 1.2.3.4
                tags:
                  - {name: a, address: 0, register: coil, datatype: bool, bit: 2}
        """))


def test_string_without_length_rejected(tmp_path):
    with pytest.raises(ConfigError, match="length"):
        load(write(tmp_path, """
            devices:
              - name: p
                host: 1.2.3.4
                tags:
                  - {name: a, address: 0, register: holding, datatype: string}
        """))


def test_unknown_datatype_rejected(tmp_path):
    with pytest.raises(ConfigError, match="datatype"):
        load(write(tmp_path, """
            devices:
              - name: p
                host: 1.2.3.4
                tags:
                  - {name: a, address: 0, register: holding, datatype: real}
        """))


def test_missing_host_rejected(tmp_path):
    with pytest.raises(ConfigError, match="host"):
        load(write(tmp_path, """
            devices:
              - name: p
                tags:
                  - {name: a, address: 0, register: holding, datatype: int16}
        """))


def test_shipped_example_config_is_valid():
    from pathlib import Path
    example = Path(__file__).resolve().parents[1] / "config" / "config.yaml"
    cfg = load(example)
    assert cfg.devices
    assert cfg.devices[0].tags
