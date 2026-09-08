"""Проверка автоопределения типов при сканировании карты регистров."""

from __future__ import annotations

import struct

import pytest

from app.discovery import ScanResult, analyze


def f32(value: float, word_order: str = "big") -> list[int]:
    raw = struct.pack(">f", value)
    words = [struct.unpack(">H", raw[0:2])[0], struct.unpack(">H", raw[2:4])[0]]
    return words[::-1] if word_order == "little" else words


def u32(value: int) -> list[int]:
    return [(value >> 16) & 0xFFFF, value & 0xFFFF]


def text(value: str, registers: int) -> list[int]:
    raw = value.encode("latin-1")[: registers * 2].ljust(registers * 2, b"\x00")
    return [struct.unpack(">H", raw[i * 2: i * 2 + 2])[0] for i in range(registers)]


def scan(rows: list[list[int]], register: str = "holding", start: int = 0) -> ScanResult:
    return ScanResult(register=register, start=start, count=len(rows[0]), samples=rows)


def by_address(candidates: list[dict]) -> dict[int, dict]:
    return {c["address"]: c for c in candidates}


# ---------------------------------------------------------------------------
def test_float32_big_endian_detected():
    rows = [f32(v) + [0, 0] for v in (25.5, 25.9, 26.4, 26.8)]
    found = by_address(analyze(scan(rows)))
    assert found[0]["datatype"] == "float32"
    assert found[0]["word_order"] == "big"
    assert found[0]["selected"] is True


def test_float32_little_endian_detected():
    rows = [f32(v, "little") + [0, 0] for v in (73.1, 73.4, 73.9, 74.2)]
    found = by_address(analyze(scan(rows)))
    assert found[0]["datatype"] == "float32"
    assert found[0]["word_order"] == "little"


def test_growing_counter_detected_as_uint32():
    rows = [u32(v) for v in (100_000, 100_140, 100_300, 100_480)]
    found = by_address(analyze(scan(rows)))
    assert found[0]["datatype"] == "uint32"
    assert "счётчик" in found[0]["reason"]


def test_counter_with_zero_high_word_still_detected():
    """Счётчик, не дошедший до 65536, — старшее слово всё время ноль."""
    rows = [u32(v) for v in (410, 415, 419, 422)]
    found = by_address(analyze(scan(rows)))
    assert found[0]["datatype"] == "uint32"


def test_status_word_detected_by_changing_bits():
    rows = [[0b0000_0000_0000_1011], [0b0000_0000_0010_0001],
            [0b0000_0000_0000_1001], [0b0000_0000_0010_1011]]
    found = by_address(analyze(scan(rows)))
    assert found[0]["datatype"] == "uint16"
    assert found[0]["role"] == "state"
    assert found[0]["bits"]


def test_all_zero_registers_are_not_selected():
    rows = [[0, 0, 0, 0] for _ in range(4)]
    for candidate in analyze(scan(rows)):
        assert candidate["selected"] is False


def test_string_span_detected():
    rows = [text("RECIPE-A", 4) + [0] for _ in range(4)]
    found = by_address(analyze(scan(rows)))
    assert found[0]["datatype"] == "string"
    assert found[0]["length"] == 4
    assert found[0]["preview"][0] == "RECIPE-A"


def test_float_after_dead_register_is_not_shifted():
    """Незанятая ячейка не должна «съезжать» на соседний float32.

    Раскладка: 0 — всегда ноль, 1..2 — настоящий float32 (значение колеблется,
    поэтому спутать пару 0..1 с растущим счётчиком нельзя).
    """
    rows = []
    for value in (12.5, 12.1, 12.7, 12.2):
        rows.append([0] + f32(value) + [0])
    found = by_address(analyze(scan(rows)))
    assert found[1]["datatype"] == "float32"
    assert found[1]["word_order"] == "big"


def test_multiword_value_on_dead_register_is_flagged():
    """Если значение всё-таки начинается с вечно нулевого регистра —
    предупреждаем и снижаем уверенность: пусть человек проверит выравнивание."""
    rows = []
    for value in (12.5, 12.9, 13.4, 13.8):     # монотонный рост: неоднозначно
        rows.append([0] + f32(value) + [0])
    candidate = by_address(analyze(scan(rows)))[0]
    assert candidate["confidence"] <= 0.6
    assert "выравнивание" in candidate["reason"]


def test_bit_registers_produce_bool_tags():
    rows = [[True, False, True], [False, False, True],
            [True, True, True], [False, True, True]]
    candidates = analyze(scan(rows, register="coil"))
    assert [c["datatype"] for c in candidates] == ["bool"] * 3
    assert candidates[0]["changing"] is True
    assert candidates[2]["changing"] is False


def test_start_address_offsets_candidates():
    rows = [f32(v) for v in (1.5, 1.6, 1.7, 1.8)]
    candidates = analyze(scan(rows, start=4096))
    assert candidates[0]["address"] == 4096


@pytest.mark.parametrize("register,prefix", [("holding", "hr"), ("input", "ir")])
def test_names_use_register_prefix(register, prefix):
    rows = [[7], [8], [9], [10]]
    candidate = analyze(scan(rows, register=register))[0]
    assert candidate["name"] == f"{prefix}_0"


def test_float_pair_is_not_mistaken_for_string():
    """Мантисса float32 иногда случайно попадает в печатный ASCII.

    По одному проходу пара чисел выглядит как текст; проверка по всем проходам
    и требование букв/цифр это отсекает.
    """
    rows = [f32(v) + f32(v * 1.3) for v in (12.34, 13.71, 15.02, 16.44)]
    found = by_address(analyze(scan(rows)))
    assert found[0]["datatype"] == "float32"
    assert all(c["datatype"] != "string" for c in found.values())


def test_real_string_survives_the_check():
    rows = [text("LINE-01 READY", 8) for _ in range(4)]
    found = by_address(analyze(scan(rows)))
    assert found[0]["datatype"] == "string"
    assert found[0]["preview"][0] == "LINE-01 READY"
