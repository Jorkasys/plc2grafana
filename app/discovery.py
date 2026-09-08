"""Разведка устройства: проверка связи, сканирование карты регистров
и автоматическое предположение типов данных.

Идея простая: прочитать окно регистров несколько раз с паузой, посмотреть,
что и как меняется, и предложить пользователю готовый список тегов. Человеку
остаётся переименовать их и поправить единицы измерения.
"""

from __future__ import annotations

import asyncio
import logging
import math
import struct
import time
from dataclasses import dataclass, field
from typing import Any

from modbus_logger import codec
from modbus_logger.config import Device
from modbus_logger.modbus import ModbusReader, ModbusReadError

log = logging.getLogger("app.discovery")

REGISTER_TYPES = ("holding", "input", "coil", "discrete")
WORD_REGISTERS = ("holding", "input")
BIT_REGISTERS = ("coil", "discrete")

# Сколько раз читаем окно, чтобы понять, что меняется
DEFAULT_SAMPLES = 4
DEFAULT_SAMPLE_DELAY_S = 0.8

# Диапазон, в котором float32 «похож на физическую величину»
FLOAT_MIN_ABS = 1e-3
FLOAT_MAX_ABS = 1e7


def _probe_device(host: str, port: int, unit_id: int, timeout_s: float,
                  transport: str = "tcp", **serial: Any) -> Device:
    """Одноразовое устройство без тегов — только чтобы подключиться."""
    return Device(
        name="__probe__",
        host=host,
        port=port,
        unit_id=unit_id,
        transport=transport,
        timeout_s=timeout_s,
        retries=0,
        reconnect_delay_s=0.5,
        serial_port=serial.get("serial_port"),
        baudrate=int(serial.get("baudrate", 9600)),
        bytesize=int(serial.get("bytesize", 8)),
        parity=str(serial.get("parity", "N")),
        stopbits=int(serial.get("stopbits", 1)),
    )


# ---------------------------------------------------------------------------
#  Проверка связи
# ---------------------------------------------------------------------------
async def test_connection(host: str, port: int = 502, unit_id: int = 1,
                          timeout_s: float = 3.0, transport: str = "tcp",
                          **serial: Any) -> dict[str, Any]:
    """Подключиться и выяснить, какие типы регистров устройство отдаёт."""
    device = _probe_device(host, port, unit_id, timeout_s, transport, **serial)
    reader = ModbusReader(device)
    started = time.monotonic()
    result: dict[str, Any] = {
        "ok": False,
        "endpoint": f"{host}:{port}" if transport != "rtu" else str(device.serial_port),
        "unit_id": unit_id,
        "latency_ms": None,
        "registers": {},
        "error": None,
    }

    try:
        if not await reader.connect():
            result["error"] = (
                "не удалось открыть TCP-соединение. Проверьте IP, порт и то, "
                "что на устройстве включён Modbus TCP"
            )
            return result
        result["latency_ms"] = round((time.monotonic() - started) * 1000, 1)

        for register in REGISTER_TYPES:
            probe = await _probe_register(reader, register)
            result["registers"][register] = probe
        result["ok"] = any(r["supported"] for r in result["registers"].values())
        if not result["ok"]:
            result["error"] = (
                "соединение есть, но устройство не отвечает ни на один тип чтения — "
                "проверьте unit_id (частые значения: 1, 0, 255)"
            )
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
    finally:
        await reader.close()
    return result


async def _probe_register(reader: ModbusReader, register: str) -> dict[str, Any]:
    """Поддерживается ли тип чтения и до какого адреса он работает."""
    try:
        await reader.read_block(register, 0, 1)
    except ModbusReadError as exc:
        return {"supported": False, "max_address": None, "error": str(exc)}

    # Верхнюю границу карты ищем удвоением, затем половинным делением —
    # это 15–20 запросов вместо тысяч.
    low, high = 0, 1
    while high < 65535:
        try:
            await reader.read_block(register, high, 1)
        except ModbusReadError:
            break
        low, high = high, min(high * 2, 65535)
    else:
        return {"supported": True, "max_address": 65535, "error": None}

    while low + 1 < high:
        mid = (low + high) // 2
        try:
            await reader.read_block(register, mid, 1)
            low = mid
        except ModbusReadError:
            high = mid
    return {"supported": True, "max_address": low, "error": None}


# ---------------------------------------------------------------------------
#  Сканирование
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ScanResult:
    register: str
    start: int
    count: int
    samples: list[list[Any]] = field(default_factory=list)
    unreadable: list[int] = field(default_factory=list)
    error: str | None = None


async def scan(host: str, port: int, unit_id: int, register: str, start: int,
               count: int, *, samples: int = DEFAULT_SAMPLES,
               delay_s: float = DEFAULT_SAMPLE_DELAY_S, timeout_s: float = 3.0,
               transport: str = "tcp", **serial: Any) -> ScanResult:
    """Прочитать окно регистров несколько раз подряд."""
    if register not in REGISTER_TYPES:
        raise ValueError(f"неизвестный тип регистра: {register}")
    count = max(1, min(int(count), 500))
    device = _probe_device(host, port, unit_id, timeout_s, transport, **serial)
    reader = ModbusReader(device)
    result = ScanResult(register=register, start=start, count=count)
    chunk = 100 if register in WORD_REGISTERS else 800

    try:
        if not await reader.connect():
            result.error = "нет соединения с устройством"
            return result
        for pass_no in range(max(1, samples)):
            if pass_no:
                await asyncio.sleep(delay_s)
            row: list[Any] = [None] * count
            unreadable: list[int] = []
            for offset in range(0, count, chunk):
                size = min(chunk, count - offset)
                try:
                    values = await reader.read_block(register, start + offset, size)
                except ModbusReadError:
                    # Дырка в карте регистров — не повод бросать всё сканирование
                    unreadable.extend(range(start + offset, start + offset + size))
                    continue
                for i, value in enumerate(values[:size]):
                    row[offset + i] = value
            result.samples.append(row)
            if pass_no == 0:
                result.unreadable = unreadable
    except Exception as exc:  # noqa: BLE001
        result.error = str(exc)
    finally:
        await reader.close()
    return result


# ---------------------------------------------------------------------------
#  Разбор результата: угадываем типы
# ---------------------------------------------------------------------------
def analyze(result: ScanResult) -> list[dict[str, Any]]:
    """Превратить сырые регистры в список предполагаемых тегов."""
    if result.register in BIT_REGISTERS:
        return _analyze_bits(result)
    return _analyze_words(result)


def _analyze_bits(result: ScanResult) -> list[dict[str, Any]]:
    out = []
    for i in range(result.count):
        address = result.start + i
        if address in result.unreadable:
            continue
        values = [bool(s[i]) for s in result.samples if s[i] is not None]
        if not values:
            continue
        changing = len(set(values)) > 1
        out.append({
            "address": address,
            "register": result.register,
            "datatype": "bool",
            "name": f"{'coil' if result.register == 'coil' else 'di'}_{address}",
            "description": None,
            "role": "state",
            "preview": ["1" if v else "0" for v in values],
            "changing": changing,
            "confidence": 1.0,
            "selected": True,
            "reason": "дискретный вход/выход",
        })
    return out


def _analyze_words(result: ScanResult) -> list[dict[str, Any]]:
    words = [
        [None if s[i] is None else int(s[i]) & 0xFFFF for s in result.samples]
        for i in range(result.count)
    ]
    n = result.count
    prefix = "hr" if result.register == "holding" else "ir"

    # 1. Строки — их видно по печатным ASCII-байтам в нескольких подряд словах
    string_spans = _find_strings(words)

    candidates: list[dict[str, Any]] = []
    i = 0
    while i < n:
        address = result.start + i
        if address in result.unreadable or words[i][0] is None:
            i += 1
            continue

        span = string_spans.get(i)
        if span:
            candidates.append(_string_candidate(result, words, i, span, prefix))
            i += span
            continue

        best = _best_word_candidate(result, words, i, prefix)
        candidates.append(best)
        i += codec.register_count(best["datatype"])
    return candidates


def _all_zero(samples: list) -> bool:
    return all(v == 0 for v in samples if v is not None)


def _best_word_candidate(result: ScanResult, words, i: int, prefix: str) -> dict[str, Any]:
    address = result.start + i
    pair_ok = i + 1 < len(words) and words[i + 1][0] is not None
    options: list[tuple[float, dict[str, Any]]] = []
    # У живого float32 первое слово почти никогда не бывает всё время нулевым:
    # в ABCD это старшее слово, в CDAB — младшее. Нулём оно оказывается там,
    # где разбор «съехал» на один регистр, — такие варианты сильно штрафуем,
    # иначе после каждой незанятой ячейки карта читается как мусор.
    dead_first = _all_zero(words[i])

    if pair_ok:
        for word_order, suffix in (("big", "ABCD"), ("little", "CDAB")):
            values = [_decode_pair(words, i, s, "float32", word_order)
                      for s in range(len(result.samples))]
            score = _float_score(values) * (0.2 if dead_first else 1.0)
            if score > 0:
                options.append((score, {
                    "datatype": "float32",
                    "word_order": word_order,
                    "reason": f"похоже на float32 {suffix}",
                    "values": values,
                }))

        for word_order in ("big", "little"):
            values = [_decode_pair(words, i, s, "uint32", word_order)
                      for s in range(len(result.samples))]
            score = _counter_score(values)
            if score > 0:
                options.append((score, {
                    "datatype": "uint32",
                    "word_order": word_order,
                    "reason": "растущий 32-битный счётчик",
                    "values": values,
                }))

    raw = [w for w in words[i] if w is not None]
    signed = [v - 0x10000 if v > 0x7FFF else v for v in raw]
    bit_score = _bitword_score(raw)
    if bit_score > 0:
        options.append((bit_score, {
            "datatype": "uint16",
            "word_order": "big",
            "reason": "слово состояния: меняются отдельные биты",
            "values": raw,
            "role": "state_word",
        }))
    if any(v < 0 for v in signed) and all(abs(v) < 30000 for v in signed):
        options.append((0.35, {
            "datatype": "int16",
            "word_order": "big",
            "reason": "знаковое 16-битное значение",
            "values": signed,
        }))
    options.append((0.3, {
        "datatype": "uint16",
        "word_order": "big",
        "reason": "16-битное целое (по умолчанию)",
        "values": raw,
    }))

    score, best = max(options, key=lambda o: o[0])
    changing = len(set(best["values"])) > 1
    all_zero = all(v == 0 for v in best["values"])
    state_word = best.pop("role", None) == "state_word"
    multi = codec.REGISTER_COUNT.get(best["datatype"], 1) > 1
    if dead_first and multi:
        best["reason"] += "; первый регистр всегда нулевой — проверьте выравнивание"
        score = min(score, 0.6)

    return {
        "address": address,
        "register": result.register,
        "datatype": best["datatype"],
        "word_order": best["word_order"],
        "byte_order": "big",
        "name": f"{prefix}_{address}",
        "description": None,
        "role": "state" if state_word else "value",
        "preview": [_fmt(v) for v in best["values"]],
        "changing": changing,
        "confidence": round(min(score, 1.0), 2),
        # Мёртвые нули почти всегда — незанятая область карты регистров
        "selected": not all_zero,
        "reason": best["reason"] + ("" if not all_zero else "; всё время ноль"),
        "bits": _changing_bits(raw) if state_word else None,
    }


def _string_candidate(result: ScanResult, words, i: int, span: int,
                      prefix: str) -> dict[str, Any]:
    address = result.start + i
    texts = []
    for s in range(len(result.samples)):
        raw = b"".join(struct.pack(">H", words[i + k][s] or 0) for k in range(span))
        texts.append(raw.split(b"\x00", 1)[0].decode("latin-1", "replace").strip())
    return {
        "address": address,
        "register": result.register,
        "datatype": "string",
        "length": span,
        "word_order": "big",
        "byte_order": "big",
        "name": f"{prefix}_{address}_text",
        "description": None,
        "role": "value",
        "preview": texts,
        "changing": len(set(texts)) > 1,
        "confidence": 0.9,
        "selected": True,
        "reason": f"текст из {span} регистров",
        "bits": None,
    }


# --- эвристики -------------------------------------------------------------
def _decode_pair(words, i: int, sample: int, datatype: str, word_order: str):
    regs = [words[i][sample] or 0, words[i + 1][sample] or 0]
    try:
        return codec.decode(regs, datatype, word_order=word_order)
    except codec.DecodeError:
        return None


def _float_score(values: list) -> float:
    if any(v is None for v in values):
        return 0.0
    finite = [v for v in values if isinstance(v, float) and math.isfinite(v)]
    if len(finite) != len(values):
        return 0.0
    plausible = [v for v in finite if v == 0.0 or FLOAT_MIN_ABS <= abs(v) <= FLOAT_MAX_ABS]
    if len(plausible) != len(finite):
        return 0.0
    if all(v == 0.0 for v in finite):
        return 0.25
    score = 0.7
    # Физическая величина меняется плавно, а не прыгает на порядки
    changes = [abs(b - a) / max(abs(a), 1e-6)
               for a, b in zip(finite, finite[1:]) if a is not None]
    if changes and max(changes) < 0.5:
        score += 0.25
    elif changes and max(changes) > 100:
        score -= 0.4
    return max(score, 0.0)


def _counter_score(values: list) -> float:
    if any(v is None for v in values) or len(values) < 2:
        return 0.0
    if not all(isinstance(v, (int, float)) for v in values):
        return 0.0
    if any(v > 4_000_000_000 for v in values):
        return 0.0
    growing = all(b >= a for a, b in zip(values, values[1:]))
    moved = values[-1] != values[0]
    if growing and moved:
        return 0.95
    return 0.0


def _bitword_score(raw: list[int]) -> float:
    if len(raw) < 2 or len(set(raw)) < 2:
        return 0.0
    changed = 0
    for a, b in zip(raw, raw[1:]):
        changed |= a ^ b
    bits = bin(changed).count("1")
    # Меняется несколько независимых бит, а само число «не растёт» — это статус
    if bits >= 2 and max(raw) <= 0xFFFF:
        return 0.8
    return 0.0


def _changing_bits(raw: list[int]) -> list[int]:
    changed = 0
    for a, b in zip(raw, raw[1:]):
        changed |= a ^ b
    return [b for b in range(16) if (changed >> b) & 1]


def _find_strings(words) -> dict[int, int]:
    """Индексы начала и длины участков, похожих на текст.

    Проверяем печатность во ВСЕХ проходах чтения, а не только в первом: мантисса
    float32 то и дело случайно попадает в диапазон печатных ASCII, и по одному
    снимку пара чисел легко выдаёт себя за строку.
    """
    printable: list[bool] = []
    for w in words:
        printable.append(bool(w) and all(_printable_word(v) for v in w))

    spans: dict[int, int] = {}
    i = 0
    while i < len(printable):
        if not printable[i]:
            i += 1
            continue
        j = i
        while j < len(printable) and printable[j]:
            j += 1
        length = j - i
        # Одиночное «печатное» слово — почти наверняка обычное число.
        # И текст должен читаться как текст: буквы или цифры, а не мусор.
        if length >= 3 and _looks_like_text(words, i, min(length, 32)):
            spans[i] = min(length, 32)
        i = j
    return spans


def _printable_word(value) -> bool:
    if value is None:
        return False
    hi, lo = (int(value) >> 8) & 0xFF, int(value) & 0xFF
    return all(c == 0 or 32 <= c < 127 for c in (hi, lo)) and (hi or lo)


def _looks_like_text(words, i: int, span: int) -> bool:
    for sample in range(len(words[i])):
        raw = b"".join(struct.pack(">H", words[i + k][sample] or 0) for k in range(span))
        text = raw.split(b"\x00", 1)[0].decode("latin-1", "replace").strip()
        if sum(c.isalnum() for c in text) < 3:
            return False
    return True


def _fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)
