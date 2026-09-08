#!/usr/bin/env python3
"""Мини-эмулятор Modbus TCP «как будто ПЛК».

Нужен, чтобы поднять и проверить весь стек до того, как появится доступ
к реальному контроллеру. Значения меняются во времени: синус, счётчик, биты.

Реализован на голом asyncio, без зависимости от pymodbus, — чтобы работать
одинаково на любой версии библиотеки и не ломаться при её обновлении.

    python tools/simulator.py --port 5020

Затем в poller/config/config.yaml укажите host/port эмулятора
(из контейнера poller — host.docker.internal или IP хоста).

Карта регистров (совпадает с примерами в poller/config/tags.yaml):
    holding 0..1     float32  температура,  20…80 °C
    holding 2..3     float32  давление,     1…9 bar
    holding 4        int16    расход, сырое 0…27648
    holding 10..11   uint32   счётчик выпуска
    holding 20       uint16   слово состояния (бит 0 — насос, бит 3 — авария)
    holding 200..207 string   имя рецепта
    input   100      uint16   ток двигателя, ×0.1 A
    coil    0..15             меняются по кругу
    discrete 0..15            инверсия coil
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import struct
import sys
import time

log = logging.getLogger("simulator")

# Русский текст в консоли Windows иначе превращается в кракозябры:
# по умолчанию там cp866/cp1251, а не UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

SIZE = 1000  # регистров каждого типа

# Коды функций
FC_READ_COILS = 1
FC_READ_DISCRETE = 2
FC_READ_HOLDING = 3
FC_READ_INPUT = 4
FC_WRITE_SINGLE_COIL = 5
FC_WRITE_SINGLE_REGISTER = 6
FC_WRITE_MULTIPLE_REGISTERS = 16

# Коды исключений Modbus
EX_ILLEGAL_FUNCTION = 0x01
EX_ILLEGAL_ADDRESS = 0x02
EX_ILLEGAL_VALUE = 0x03


class DataStore:
    def __init__(self) -> None:
        self.holding = [0] * SIZE
        self.input = [0] * SIZE
        self.coils = [False] * SIZE
        self.discrete = [False] * SIZE

    # -- помощники записи --------------------------------------------------
    def put_f32(self, addr: int, value: float) -> None:
        raw = struct.pack(">f", value)
        self.holding[addr] = struct.unpack(">H", raw[0:2])[0]
        self.holding[addr + 1] = struct.unpack(">H", raw[2:4])[0]

    def put_u32(self, addr: int, value: int) -> None:
        value &= 0xFFFFFFFF
        self.holding[addr] = (value >> 16) & 0xFFFF
        self.holding[addr + 1] = value & 0xFFFF

    def put_string(self, addr: int, text: str, length: int) -> None:
        raw = text.encode("latin-1")[: length * 2].ljust(length * 2, b"\x00")
        for i in range(length):
            self.holding[addr + i] = struct.unpack(">H", raw[i * 2 : i * 2 + 2])[0]


RECIPES = ["RECIPE-A", "RECIPE-B", "NIGHT-01"]

# --- Карта «производственной линии» ---------------------------------------
# Машина №i занимает 20 holding-регистров начиная с LINE_BASE + i * 20.
# Такая раскладка удобна для демонстрации цифрового двойника: сканер находит
# у каждой машины один и тот же набор тегов, а вы раскладываете их по узлам.
#
#   +0..1   float32  температура, °C
#   +2..3   float32  давление, bar
#   +4..5   float32  скорость, м/мин
#   +6..7   uint32   счётчик выпуска, шт
#   +8      uint16   слово состояния (бит0 работа, бит1 подача,
#                     бит3 авария, бит5 наладка)
#   +10..11 float32  ток двигателя, A
#   +12..13 float32  вибрация, мм/с
LINE_BASE = 100
LINE_STRIDE = 20
MACHINE_NAMES = ["Розлив", "Укупор", "Этикетировка", "Упаковка", "Паллетайзер"]


async def updater(store: DataStore, machines: int) -> None:
    """Крутит значения раз в 200 мс, чтобы графики были живыми."""
    started = time.monotonic()
    counter = 0
    while True:
        t = time.monotonic() - started
        counter += 1

        # --- одиночное устройство (совместимо с poller/config/tags.yaml) ---
        temperature = 50 + 30 * math.sin(t / 20)
        pressure = 5 + 4 * math.sin(t / 7 + 1)

        store.put_f32(0, temperature)
        store.put_f32(2, pressure)
        store.holding[4] = int(13824 + 13820 * math.sin(t / 11)) & 0xFFFF
        store.put_u32(10, counter)

        status = 0
        if int(t) % 10 < 6:
            status |= 1 << 0            # насос работает
        if temperature > 72:
            status |= 1 << 3            # авария по перегреву
        store.holding[20] = status

        store.put_string(200, RECIPES[int(t / 30) % len(RECIPES)], 8)
        store.input[100] = int(abs(120 + 40 * math.sin(t / 3)))

        pattern = [(counter + i) % 7 < 3 for i in range(16)]
        store.coils[0:16] = pattern
        store.discrete[0:16] = [not p for p in pattern]

        # --- производственная линия ---------------------------------------
        for i in range(machines):
            base = LINE_BASE + i * LINE_STRIDE
            phase = i * 1.7
            running = (t + i * 9) % 120 > 12        # раз в 2 минуты короткий простой
            temp = 42 + 18 * math.sin(t / 25 + phase) + (6 if not running else 0)
            press = 3.2 + 1.4 * math.sin(t / 9 + phase)
            speed = (85 + 12 * math.sin(t / 17 + phase)) if running else 0.0
            current = (14 + 3.5 * math.sin(t / 5 + phase)) if running else 0.4
            vibration = 1.2 + 0.6 * math.sin(t / 3 + phase) + (1.8 if temp > 57 else 0)

            store.put_f32(base + 0, temp)
            store.put_f32(base + 2, press)
            store.put_f32(base + 4, speed)
            store.put_u32(base + 6, int(counter * (0.6 + 0.15 * i)) if running else 0)
            store.put_f32(base + 10, current)
            store.put_f32(base + 12, vibration)

            word = 0
            if running:
                word |= 1 << 0                      # работа
                word |= 1 << 1                      # подача материала
            if temp > 57:
                word |= 1 << 3                      # авария по перегреву
            if not running and int(t / 7) % 3 == 0:
                word |= 1 << 5                      # режим наладки
            store.holding[base + 8] = word

            store.input[110 + i] = int(max(speed, 0) * 10)

        await asyncio.sleep(0.2)


def _bits_to_bytes(bits: list[bool]) -> bytes:
    out = bytearray((len(bits) + 7) // 8)
    for i, bit in enumerate(bits):
        if bit:
            out[i // 8] |= 1 << (i % 8)
    return bytes(out)


def handle_pdu(store: DataStore, pdu: bytes) -> bytes:
    """Обработать PDU и вернуть ответный PDU."""
    if not pdu:
        return bytes([0x80, EX_ILLEGAL_FUNCTION])
    fc = pdu[0]

    def error(code: int) -> bytes:
        return bytes([fc | 0x80, code])

    if fc in (FC_READ_COILS, FC_READ_DISCRETE, FC_READ_HOLDING, FC_READ_INPUT):
        if len(pdu) < 5:
            return error(EX_ILLEGAL_VALUE)
        address, count = struct.unpack(">HH", pdu[1:5])

        if fc in (FC_READ_HOLDING, FC_READ_INPUT):
            if not 1 <= count <= 125:
                return error(EX_ILLEGAL_VALUE)
            source = store.holding if fc == FC_READ_HOLDING else store.input
            if address + count > SIZE:
                return error(EX_ILLEGAL_ADDRESS)
            data = b"".join(struct.pack(">H", v & 0xFFFF)
                            for v in source[address : address + count])
            return bytes([fc, len(data)]) + data

        if not 1 <= count <= 2000:
            return error(EX_ILLEGAL_VALUE)
        source = store.coils if fc == FC_READ_COILS else store.discrete
        if address + count > SIZE:
            return error(EX_ILLEGAL_ADDRESS)
        data = _bits_to_bytes(source[address : address + count])
        return bytes([fc, len(data)]) + data

    if fc == FC_WRITE_SINGLE_REGISTER:
        address, value = struct.unpack(">HH", pdu[1:5])
        if address >= SIZE:
            return error(EX_ILLEGAL_ADDRESS)
        store.holding[address] = value
        return pdu[:5]

    if fc == FC_WRITE_SINGLE_COIL:
        address, value = struct.unpack(">HH", pdu[1:5])
        if address >= SIZE:
            return error(EX_ILLEGAL_ADDRESS)
        store.coils[address] = value == 0xFF00
        return pdu[:5]

    if fc == FC_WRITE_MULTIPLE_REGISTERS:
        address, count, _bytes = struct.unpack(">HHB", pdu[1:6])
        if address + count > SIZE:
            return error(EX_ILLEGAL_ADDRESS)
        for i in range(count):
            (store.holding[address + i],) = struct.unpack(">H", pdu[6 + i * 2 : 8 + i * 2])
        return struct.pack(">BHH", fc, address, count)

    return error(EX_ILLEGAL_FUNCTION)


def make_handler(store: DataStore, unit_id: int):
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        log.info("подключился %s", peer)
        try:
            while True:
                header = await reader.readexactly(7)
                tid, pid, length, uid = struct.unpack(">HHHB", header)
                pdu = await reader.readexactly(max(length - 1, 0))

                if pid != 0:
                    continue
                if unit_id not in (0, uid) and uid != 0:
                    log.debug("запрос к unit=%d проигнорирован", uid)
                    continue

                response = handle_pdu(store, pdu)
                writer.write(struct.pack(">HHHB", tid, 0, len(response) + 1, uid) + response)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            log.info("отключился %s", peer)
            writer.close()

    return handler


async def main() -> int:
    ap = argparse.ArgumentParser(description="Эмулятор Modbus TCP для тестов")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5020)
    ap.add_argument("--unit", type=int, default=1)
    ap.add_argument("--machines", type=int, default=3,
                    help="сколько машин линии эмулировать (0 — только базовая карта)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    args.machines = max(0, min(args.machines, len(MACHINE_NAMES)))

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    store = DataStore()
    asyncio.create_task(updater(store, args.machines))

    server = await asyncio.start_server(make_handler(store, args.unit), args.host, args.port)
    log.info("эмулятор Modbus TCP слушает %s:%d, unit=%d", args.host, args.port, args.unit)
    for i in range(args.machines):
        log.info("  машина «%s»: holding %d…%d",
                 MACHINE_NAMES[i], LINE_BASE + i * LINE_STRIDE,
                 LINE_BASE + i * LINE_STRIDE + LINE_STRIDE - 1)
    async with server:
        await server.serve_forever()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
