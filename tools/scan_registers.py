#!/usr/bin/env python3
"""Разведка неизвестного контроллера: читаем диапазон и показываем,
как он выглядит во всех типовых форматах.

    python tools/scan_registers.py --host 192.168.0.10 --start 0 --count 40
    python tools/scan_registers.py --host 192.168.0.10 --register input --start 100 --count 20
    python tools/scan_registers.py --host 192.168.0.10 --start 0 --count 40 --emit-yaml > draft_tags.yaml

Смысл: увидеть, где лежат осмысленные значения. Если в колонке float32(ABCD)
внезапно появилось «23.7» — вы нашли температуру.
"""

from __future__ import annotations

import argparse
import asyncio
import struct
import sys
from pathlib import Path

# Русский текст в консоли Windows иначе превращается в кракозябры:
# по умолчанию там cp866/cp1251, а не UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "poller" / "src"))

from modbus_logger import codec  # noqa: E402
from modbus_logger.config import Device  # noqa: E402
from modbus_logger.modbus import ModbusReader  # noqa: E402


def _fmt(value, width: int = 14) -> str:
    if value is None:
        return "—".rjust(width)
    if isinstance(value, float):
        if abs(value) > 1e9 or (value != 0 and abs(value) < 1e-4):
            return f"{value:.3e}".rjust(width)
        return f"{value:.4f}".rjust(width)
    return str(value).rjust(width)


def _try(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return None


def print_table(register: str, start: int, values: list[int]) -> None:
    is_bits = register in {"coil", "discrete"}
    if is_bits:
        print(f"\n{register}: адреса {start}..{start + len(values) - 1}\n")
        print("  адрес  значение")
        for i, v in enumerate(values):
            print(f"  {start + i:>5}  {int(bool(v))}")
        return

    header = (
        f"{'адрес':>6} {'hex':>6} {'uint16':>8} {'int16':>8} "
        f"{'int32 ABCD':>14} {'int32 CDAB':>14} "
        f"{'float ABCD':>14} {'float CDAB':>14}  биты"
    )
    print(f"\n{register}: адреса {start}..{start + len(values) - 1}\n")
    print(header)
    print("-" * len(header))

    for i, raw in enumerate(values):
        addr = start + i
        pair = values[i : i + 2]
        has_pair = len(pair) == 2

        u16 = raw & 0xFFFF
        i16 = struct.unpack(">h", struct.pack(">H", u16))[0]
        i32_be = _try(lambda: codec.decode(pair, "int32", word_order="big")) if has_pair else None
        i32_le = _try(lambda: codec.decode(pair, "int32", word_order="little")) if has_pair else None
        f_be = _try(lambda: codec.decode(pair, "float32", word_order="big")) if has_pair else None
        f_le = _try(lambda: codec.decode(pair, "float32", word_order="little")) if has_pair else None
        bits = format(u16, "016b")

        print(
            f"{addr:>6} {('0x' + format(u16, '04x')):>6} {u16:>8} {i16:>8} "
            f"{_fmt(i32_be)} {_fmt(i32_le)} {_fmt(f_be)} {_fmt(f_le)}  {bits}"
        )


def emit_yaml(register: str, start: int, values: list[int]) -> None:
    """Черновик tags.yaml: по одному uint16-тегу на регистр. Дальше правьте руками."""
    print("tags:")
    for i, raw in enumerate(values):
        addr = start + i
        print(f"  - name: reg_{register}_{addr}")
        print(f"    description: \"снято при сканировании, сырое значение {raw}\"")
        print(f"    address: {addr}")
        print(f"    register: {register}")
        print("    datatype: uint16")
        print("    poll_interval_ms: 1000")
        print("")


async def main() -> int:
    ap = argparse.ArgumentParser(description="Сканирование регистров Modbus")
    ap.add_argument("--host", required=True, help="IP контроллера/шлюза")
    ap.add_argument("--port", type=int, default=502)
    ap.add_argument("--unit", type=int, default=1, help="unit / slave id")
    ap.add_argument("--register", default="holding",
                    choices=["holding", "input", "coil", "discrete"])
    ap.add_argument("--start", type=int, default=0, help="начальный адрес (с нуля!)")
    ap.add_argument("--count", type=int, default=20, help="сколько регистров прочитать")
    ap.add_argument("--timeout", type=float, default=3.0)
    ap.add_argument("--chunk", type=int, default=100, help="размер одного запроса")
    ap.add_argument("--emit-yaml", action="store_true",
                    help="вывести черновик tags.yaml вместо таблицы")
    ap.add_argument("--watch", type=float, default=0.0,
                    help="повторять каждые N секунд (Ctrl+C для выхода)")
    args = ap.parse_args()

    device = Device(name="scan", host=args.host, port=args.port,
                    unit_id=args.unit, timeout_s=args.timeout, retries=1)
    reader = ModbusReader(device)

    if not await reader.connect():
        print(f"не удалось подключиться к {args.host}:{args.port}", file=sys.stderr)
        return 1

    try:
        while True:
            values: list[int] = []
            remaining = args.count
            addr = args.start
            while remaining > 0:
                n = min(remaining, args.chunk)
                try:
                    values.extend(await reader.read_block(args.register, addr, n))
                except Exception as exc:  # noqa: BLE001
                    print(f"ошибка чтения с адреса {addr}: {exc}", file=sys.stderr)
                    if not values:
                        return 2
                    break
                addr += n
                remaining -= n

            if args.emit_yaml:
                emit_yaml(args.register, args.start, values)
                return 0

            print_table(args.register, args.start, values)
            if args.watch <= 0:
                return 0
            await asyncio.sleep(args.watch)
            print("\033[2J\033[H", end="")  # очистить экран
    finally:
        await reader.close()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
