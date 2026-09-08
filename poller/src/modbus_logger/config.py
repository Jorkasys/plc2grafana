"""Загрузка и валидация конфигурации (YAML + подстановка переменных окружения)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import codec

# ${VAR} или ${VAR:значение-по-умолчанию}
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")

REGISTER_TYPES = {"holding", "input", "coil", "discrete"}
BIT_REGISTERS = {"coil", "discrete"}
NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


class ConfigError(Exception):
    """Некорректная конфигурация — сервис не должен стартовать."""


# ---------------------------------------------------------------------------
#  Подстановка переменных окружения
# ---------------------------------------------------------------------------
def _expand(value: Any) -> Any:
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            var, default = m.group(1), m.group(2)
            env = os.environ.get(var)
            if env is not None and env != "":
                return env
            if default is not None:
                return default
            raise ConfigError(f"переменная окружения {var} не задана и не имеет значения по умолчанию")
        expanded = _ENV_RE.sub(repl, value)
        return _coerce_scalar(expanded) if expanded != value else value
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _coerce_scalar(text: str) -> Any:
    """'8080' -> 8080, 'true' -> True, '1.5' -> 1.5, иначе строка."""
    low = text.strip().lower()
    if low in {"true", "yes", "on"}:
        return True
    if low in {"false", "no", "off"}:
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


# ---------------------------------------------------------------------------
#  Датаклассы
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class LoggingConfig:
    level: str = "INFO"
    format: str = "text"


@dataclass(slots=True)
class HttpConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass(slots=True)
class DatabaseConfig:
    host: str = "timescaledb"
    port: int = 5432
    name: str = "plc"
    user: str = "plc"
    password: str = "plc"
    min_pool_size: int = 1
    max_pool_size: int = 4
    batch_max_rows: int = 500
    batch_max_delay_ms: int = 1000
    buffer_max_rows: int = 200_000

    @property
    def dsn(self) -> str:
        return (
            f"postgresql://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.name}"
        )

    def safe_dsn(self) -> str:
        return f"postgresql://{self.user}:***@{self.host}:{self.port}/{self.name}"


@dataclass(slots=True)
class BlockConfig:
    max_registers: int = 100
    max_gap: int = 8


@dataclass(slots=True)
class TagDefaults:
    poll_interval_ms: int = 1000
    word_order: str = "big"
    byte_order: str = "big"
    deadband: float = 0.0
    deadband_mode: str = "absolute"
    heartbeat_s: float = 60.0
    scale: float = 1.0
    offset: float = 0.0


@dataclass(slots=True)
class Tag:
    device: str
    name: str
    address: int
    register: str
    datatype: str
    description: str | None = None
    unit: str | None = None
    bit: int | None = None
    length: int | None = None
    scale: float = 1.0
    offset: float = 0.0
    min: float | None = None
    max: float | None = None
    poll_interval_ms: int = 1000
    deadband: float = 0.0
    deadband_mode: str = "absolute"
    heartbeat_s: float = 60.0
    word_order: str = "big"
    byte_order: str = "big"
    labels: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    # Заполняется после регистрации тега в БД
    tag_id: int | None = None

    @property
    def count(self) -> int:
        """Сколько регистров/бит читать."""
        if self.register in BIT_REGISTERS:
            return 1
        return codec.register_count(self.datatype, self.length)

    @property
    def key(self) -> str:
        return f"{self.device}.{self.name}"


@dataclass(slots=True)
class Device:
    name: str
    host: str
    port: int = 502
    unit_id: int = 1
    transport: str = "tcp"
    description: str | None = None
    enabled: bool = True
    timeout_s: float = 3.0
    retries: int = 2
    reconnect_delay_s: float = 2.0
    reconnect_delay_max_s: float = 30.0
    inter_request_delay_ms: int = 0
    block: BlockConfig = field(default_factory=BlockConfig)
    tags: list[Tag] = field(default_factory=list)
    # только для transport: rtu
    serial_port: str | None = None
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: int = 1


@dataclass(slots=True)
class AppConfig:
    logging: LoggingConfig
    http: HttpConfig
    database: DatabaseConfig
    devices: list[Device]


# ---------------------------------------------------------------------------
#  Разбор
# ---------------------------------------------------------------------------
def _require(data: dict, key: str, where: str) -> Any:
    if key not in data or data[key] is None:
        raise ConfigError(f"{where}: обязательное поле '{key}' отсутствует")
    return data[key]


def _parse_tag(raw: dict, device_name: str, defaults: TagDefaults) -> Tag:
    where = f"устройство '{device_name}', тег {raw.get('name', '<без имени>')!r}"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: ожидался словарь")

    name = str(_require(raw, "name", where))
    if not NAME_RE.match(name):
        raise ConfigError(f"{where}: имя может содержать только буквы, цифры, . _ -")

    register = str(raw.get("register", "holding")).lower()
    if register not in REGISTER_TYPES:
        raise ConfigError(f"{where}: register должен быть одним из {sorted(REGISTER_TYPES)}")

    datatype = str(raw.get("datatype", "bool" if register in BIT_REGISTERS else "int16")).lower()
    if datatype not in codec.ALL_TYPES:
        raise ConfigError(f"{where}: неизвестный datatype {datatype!r}")

    if register in BIT_REGISTERS and datatype != "bool":
        raise ConfigError(f"{where}: для register '{register}' datatype должен быть 'bool'")

    address = _require(raw, "address", where)
    if not isinstance(address, int) or address < 0 or address > 65535:
        raise ConfigError(f"{where}: address должен быть целым 0..65535")

    bit = raw.get("bit")
    if bit is not None:
        if register in BIT_REGISTERS:
            raise ConfigError(f"{where}: поле bit неприменимо к '{register}'")
        if datatype != "bool":
            raise ConfigError(f"{where}: поле bit применимо только к datatype 'bool'")
        if not isinstance(bit, int) or not 0 <= bit <= 15:
            raise ConfigError(f"{where}: bit должен быть целым 0..15")

    length = raw.get("length")
    if datatype == "string" and (not isinstance(length, int) or length < 1):
        raise ConfigError(f"{where}: для datatype 'string' задайте length (число регистров)")

    deadband_mode = str(raw.get("deadband_mode", defaults.deadband_mode)).lower()
    if deadband_mode not in {"absolute", "percent"}:
        raise ConfigError(f"{where}: deadband_mode должен быть 'absolute' или 'percent'")

    for key in ("word_order", "byte_order"):
        val = str(raw.get(key, getattr(defaults, key))).lower()
        if val not in {"big", "little"}:
            raise ConfigError(f"{where}: {key} должен быть 'big' или 'little'")

    poll_interval_ms = int(raw.get("poll_interval_ms", defaults.poll_interval_ms))
    if poll_interval_ms < 10:
        raise ConfigError(f"{where}: poll_interval_ms слишком мал (минимум 10 мс)")

    tag = Tag(
        device=device_name,
        name=name,
        address=int(address),
        register=register,
        datatype=datatype,
        description=raw.get("description"),
        unit=raw.get("unit"),
        bit=bit,
        length=length,
        scale=float(raw.get("scale", defaults.scale)),
        offset=float(raw.get("offset", defaults.offset)),
        min=None if raw.get("min") is None else float(raw["min"]),
        max=None if raw.get("max") is None else float(raw["max"]),
        poll_interval_ms=poll_interval_ms,
        deadband=float(raw.get("deadband", defaults.deadband)),
        deadband_mode=deadband_mode,
        heartbeat_s=float(raw.get("heartbeat_s", defaults.heartbeat_s)),
        word_order=str(raw.get("word_order", defaults.word_order)).lower(),
        byte_order=str(raw.get("byte_order", defaults.byte_order)).lower(),
        labels=dict(raw.get("labels") or {}),
        enabled=bool(raw.get("enabled", True)),
    )

    if tag.min is not None and tag.max is not None and tag.min > tag.max:
        raise ConfigError(f"{where}: min больше max")
    if tag.address + tag.count - 1 > 65535:
        raise ConfigError(f"{where}: блок регистров выходит за 65535")
    return tag


def parse_tag(raw: dict, device_name: str, defaults: TagDefaults | None = None) -> Tag:
    """Разобрать и проверить описание одного тега.

    Публичная обёртка: тем же валидатором пользуется веб-приложение, когда
    принимает теги из браузера, — правила проверки нигде не дублируются.
    """
    return _parse_tag(raw, device_name, defaults or TagDefaults())


def _parse_device(raw: dict, config_dir: Path) -> Device:
    name = str(_require(raw, "name", "devices[]"))
    where = f"устройство '{name}'"

    transport = str(raw.get("transport", "tcp")).lower()
    if transport not in {"tcp", "rtu", "udp"}:
        raise ConfigError(f"{where}: transport должен быть 'tcp', 'udp' или 'rtu'")
    if transport in {"tcp", "udp"} and not raw.get("host"):
        raise ConfigError(f"{where}: для transport '{transport}' обязателен host")
    if transport == "rtu" and not raw.get("serial_port"):
        raise ConfigError(f"{where}: для transport 'rtu' обязателен serial_port")

    defaults = TagDefaults(**{
        k: v for k, v in (raw.get("defaults") or {}).items()
        if k in TagDefaults.__dataclass_fields__
    })
    block = BlockConfig(**{
        k: v for k, v in (raw.get("block") or {}).items()
        if k in BlockConfig.__dataclass_fields__
    })

    # Теги: либо inline-список, либо отдельный файл
    raw_tags = raw.get("tags")
    if raw_tags is None:
        tags_file = raw.get("tags_file")
        if not tags_file:
            raise ConfigError(f"{where}: задайте либо 'tags', либо 'tags_file'")
        path = (config_dir / str(tags_file)).resolve()
        if not path.is_file():
            raise ConfigError(f"{where}: файл тегов не найден: {path}")
        loaded = _expand(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
        raw_tags = loaded.get("tags") if isinstance(loaded, dict) else loaded
        if not raw_tags:
            raise ConfigError(f"{where}: в {path} нет ключа 'tags' или он пуст")

    tags = [_parse_tag(t, name, defaults) for t in raw_tags]

    seen: set[str] = set()
    for t in tags:
        if t.name in seen:
            raise ConfigError(f"{where}: дублирующееся имя тега {t.name!r}")
        seen.add(t.name)

    device = Device(
        name=name,
        host=str(raw.get("host", "")),
        port=int(raw.get("port", 502)),
        unit_id=int(raw.get("unit_id", 1)),
        transport=transport,
        description=raw.get("description"),
        enabled=bool(raw.get("enabled", True)),
        timeout_s=float(raw.get("timeout_s", 3.0)),
        retries=int(raw.get("retries", 2)),
        reconnect_delay_s=float(raw.get("reconnect_delay_s", 2.0)),
        reconnect_delay_max_s=float(raw.get("reconnect_delay_max_s", 30.0)),
        inter_request_delay_ms=int(raw.get("inter_request_delay_ms", 0)),
        block=block,
        tags=tags,
        serial_port=raw.get("serial_port"),
        baudrate=int(raw.get("baudrate", 9600)),
        bytesize=int(raw.get("bytesize", 8)),
        parity=str(raw.get("parity", "N")).upper(),
        stopbits=int(raw.get("stopbits", 1)),
    )
    if not 0 <= device.unit_id <= 247:
        raise ConfigError(f"{where}: unit_id должен быть 0..247")
    if device.block.max_registers < 1 or device.block.max_registers > 125:
        raise ConfigError(f"{where}: block.max_registers должен быть 1..125")
    return device


def load(path: str | Path) -> AppConfig:
    """Прочитать config.yaml и вернуть готовую конфигурацию."""
    path = Path(path).resolve()
    if not path.is_file():
        raise ConfigError(f"файл конфигурации не найден: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: ожидался YAML-словарь верхнего уровня")
    raw = _expand(raw)

    devices_raw = raw.get("devices") or []
    if not devices_raw:
        raise ConfigError(f"{path}: список 'devices' пуст")

    devices = [_parse_device(d, path.parent) for d in devices_raw]
    names = [d.name for d in devices]
    if len(names) != len(set(names)):
        raise ConfigError("имена устройств должны быть уникальны")

    return AppConfig(
        logging=LoggingConfig(**{
            k: v for k, v in (raw.get("logging") or {}).items()
            if k in LoggingConfig.__dataclass_fields__
        }),
        http=HttpConfig(**{
            k: v for k, v in (raw.get("http") or {}).items()
            if k in HttpConfig.__dataclass_fields__
        }),
        database=DatabaseConfig(**{
            k: v for k, v in (raw.get("database") or {}).items()
            if k in DatabaseConfig.__dataclass_fields__
        }),
        devices=devices,
    )
