import struct

import pytest

from modbus_logger import codec


def regs_from_float(value: float, word_order="big", byte_order="big"):
    return codec.encode(value, "float32", word_order=word_order, byte_order=byte_order)


@pytest.mark.parametrize("value", [0.0, 1.0, -1.0, 23.7, -273.15, 1234.5678])
@pytest.mark.parametrize("word_order", ["big", "little"])
@pytest.mark.parametrize("byte_order", ["big", "little"])
def test_float32_roundtrip(value, word_order, byte_order):
    regs = codec.encode(value, "float32", word_order=word_order, byte_order=byte_order)
    back = codec.decode(regs, "float32", word_order=word_order, byte_order=byte_order)
    assert back == pytest.approx(value, rel=1e-6)


def test_float32_abcd_known_value():
    # 23.7 в IEEE754 = 0x41BD999A -> ABCD: [0x41BD, 0x999A]
    assert codec.decode([0x41BD, 0x999A], "float32") == pytest.approx(23.7, rel=1e-6)
    # CDAB — те же байты, слова местами
    assert codec.decode([0x999A, 0x41BD], "float32", word_order="little") == pytest.approx(23.7, rel=1e-6)


def test_int16_signed():
    assert codec.decode([0xFFFF], "int16") == -1
    assert codec.decode([0xFFFF], "uint16") == 65535
    assert codec.decode([0x8000], "int16") == -32768


def test_int32_word_order():
    assert codec.decode([0x0001, 0x0000], "int32") == 65536
    assert codec.decode([0x0000, 0x0001], "int32", word_order="little") == 65536


def test_int64_roundtrip():
    value = -1234567890123
    regs = codec.encode(value, "int64")
    assert codec.decode(regs, "int64") == value


def test_bool_from_bit():
    word = 0b0000_0000_0000_1001
    assert codec.decode([word], "bool", bit=0) is True
    assert codec.decode([word], "bool", bit=1) is False
    assert codec.decode([word], "bool", bit=3) is True
    assert codec.decode([word], "bool", bit=15) is False


def test_bool_from_coil():
    assert codec.decode([True], "bool") is True
    assert codec.decode([False], "bool") is False


def test_bool_bit_out_of_range():
    with pytest.raises(codec.DecodeError):
        codec.decode([1], "bool", bit=16)


def test_string_roundtrip():
    regs = codec.encode("RECIPE-A", "string", length=8)
    assert codec.decode(regs, "string") == "RECIPE-A"


def test_string_needs_length():
    with pytest.raises(codec.DecodeError):
        codec.register_count("string", None)


def test_not_enough_registers():
    with pytest.raises(codec.DecodeError):
        codec.decode([0x0001], "float32")


def test_nan_rejected():
    nan_regs = [struct.unpack(">H", struct.pack(">f", float("nan"))[i : i + 2])[0]
                for i in (0, 2)]
    with pytest.raises(codec.DecodeError):
        codec.decode(nan_regs, "float32")


def test_register_counts():
    assert codec.register_count("int16") == 1
    assert codec.register_count("float32") == 2
    assert codec.register_count("float64") == 4
    assert codec.register_count("string", 8) == 8
