"""Декодирование Modbus-регистров в значения Python.

Реализовано на чистом ``struct`` и не зависит от версии pymodbus
(классы ``BinaryPayloadDecoder`` в разных версиях 3.x то есть, то нет).

Порядок слов/байт
-----------------
Modbus передаёт 16-битные слова, порядок байт внутри слова — big-endian.
Многословные значения (32/64 бита) стандарт не описывает, поэтому у каждого
вендора свой порядок. Обозначим байты 32-битного числа как ABCD:

    word_order=big,    byte_order=big     -> ABCD  (Siemens, Schneider, "Motorola")
    word_order=little, byte_order=big     -> CDAB  (Modicon, многие счётчики)
    word_order=big,    byte_order=little  -> BADC
    word_order=little, byte_order=little  -> DCBA  ("Intel", чистый little-endian)

Если float читается как мусор — почти всегда достаточно поменять word_order.
"""

from __future__ import annotations

import math
import struct
from typing import Sequence

# --- сколько 16-битных регистров занимает тип ------------------------------
REGISTER_COUNT = {
    "bool": 1,
    "int16": 1,
    "uint16": 1,
    "int32": 2,
    "uint32": 2,
    "float32": 2,
    "int64": 4,
    "uint64": 4,
    "float64": 4,
    # "string" — переменной длины, см. length
}

_STRUCT_FMT = {
    "int16": ">h",
    "uint16": ">H",
    "int32": ">i",
    "uint32": ">I",
    "float32": ">f",
    "int64": ">q",
    "uint64": ">Q",
    "float64": ">d",
}

NUMERIC_TYPES = frozenset(_STRUCT_FMT) | {"bool"}
ALL_TYPES = NUMERIC_TYPES | {"string"}


class DecodeError(ValueError):
    """Регистры не удалось преобразовать в значение."""


def register_count(datatype: str, length: int | None = None) -> int:
    """Сколько регистров нужно прочитать для тега данного типа."""
    if datatype == "string":
        if not length or length < 1:
            raise DecodeError("для datatype: string обязательно поле length (в регистрах)")
        return int(length)
    try:
        return REGISTER_COUNT[datatype]
    except KeyError:
        raise DecodeError(f"неизвестный datatype: {datatype!r}") from None


def registers_to_bytes(
    registers: Sequence[int],
    word_order: str = "big",
    byte_order: str = "big",
) -> bytes:
    """Собрать список 16-битных слов в байтовую строку с учётом порядка."""
    words = list(registers)
    if word_order == "little":
        words.reverse()
    fmt = ">H" if byte_order == "big" else "<H"
    try:
        return b"".join(struct.pack(fmt, int(w) & 0xFFFF) for w in words)
    except (struct.error, TypeError, ValueError) as exc:
        raise DecodeError(f"не удалось упаковать регистры {registers!r}: {exc}") from exc


def decode(
    registers: Sequence[int],
    datatype: str,
    *,
    word_order: str = "big",
    byte_order: str = "big",
    bit: int | None = None,
    encoding: str = "latin-1",
) -> float | bool | str:
    """Преобразовать регистры в значение.

    ``bit`` имеет смысл только для ``datatype='bool'``, прочитанного из
    holding/input-регистра: вернётся указанный бит слова (0 — младший).
    """
    if datatype == "bool":
        if not registers:
            raise DecodeError("пустой ответ для bool")
        raw = int(registers[0])
        # Из coil/discrete pymodbus отдаёт готовый True/False
        if isinstance(registers[0], bool):
            return bool(registers[0])
        if bit is None:
            return bool(raw)
        if not 0 <= bit <= 15:
            raise DecodeError(f"bit должен быть в диапазоне 0..15, получено {bit}")
        return bool((raw >> bit) & 1)

    if datatype == "string":
        raw = registers_to_bytes(registers, word_order, byte_order)
        text = raw.split(b"\x00", 1)[0].decode(encoding, errors="replace")
        return text.strip()

    fmt = _STRUCT_FMT.get(datatype)
    if fmt is None:
        raise DecodeError(f"неизвестный datatype: {datatype!r}")

    need = REGISTER_COUNT[datatype]
    if len(registers) < need:
        raise DecodeError(
            f"для {datatype} нужно {need} регистр(ов), получено {len(registers)}"
        )

    raw = registers_to_bytes(registers[:need], word_order, byte_order)
    try:
        (value,) = struct.unpack(fmt, raw)
    except struct.error as exc:
        raise DecodeError(f"struct.unpack({fmt}) на {raw!r}: {exc}") from exc

    if isinstance(value, float) and not math.isfinite(value):
        raise DecodeError(f"нечисловое значение float: {value}")
    return value


def encode(
    value: float | bool | str,
    datatype: str,
    *,
    word_order: str = "big",
    byte_order: str = "big",
    length: int | None = None,
    encoding: str = "latin-1",
) -> list[int]:
    """Обратное преобразование — пригодится, если будете писать в ПЛК."""
    if datatype == "bool":
        return [1 if value else 0]

    if datatype == "string":
        n = register_count("string", length)
        raw = str(value).encode(encoding, errors="replace")[: n * 2]
        raw = raw.ljust(n * 2, b"\x00")
    else:
        fmt = _STRUCT_FMT.get(datatype)
        if fmt is None:
            raise DecodeError(f"неизвестный datatype: {datatype!r}")
        if datatype.startswith(("int", "uint")):
            value = int(value)
        raw = struct.pack(fmt, value)

    unpack_fmt = ">H" if byte_order == "big" else "<H"
    words = [struct.unpack(unpack_fmt, raw[i : i + 2])[0] for i in range(0, len(raw), 2)]
    if word_order == "little":
        words.reverse()
    return words
