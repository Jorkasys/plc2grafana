"""Асинхронный клиент Modbus поверх pymodbus с переподключением.

Совместим с pymodbus 3.6 … 3.11: имя аргумента адреса устройства менялось
(``unit`` → ``slave`` → ``device_id``), поэтому оно определяется через
интроспекцию сигнатуры один раз при старте.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

from pymodbus.exceptions import ConnectionException, ModbusException

from .config import Device

log = logging.getLogger(__name__)

BIT_REGISTERS = {"coil", "discrete"}


class ModbusReadError(Exception):
    """Не удалось прочитать блок регистров."""


class ModbusConnectionLost(ModbusReadError):
    """Соединение с устройством потеряно."""


def _build_client(device: Device):
    """Создать клиент нужного транспорта."""
    if device.transport == "tcp":
        from pymodbus.client import AsyncModbusTcpClient

        return AsyncModbusTcpClient(
            host=device.host, port=device.port, timeout=device.timeout_s
        )
    if device.transport == "udp":
        from pymodbus.client import AsyncModbusUdpClient

        return AsyncModbusUdpClient(
            host=device.host, port=device.port, timeout=device.timeout_s
        )
    if device.transport == "rtu":
        from pymodbus.client import AsyncModbusSerialClient

        return AsyncModbusSerialClient(
            port=device.serial_port,
            baudrate=device.baudrate,
            bytesize=device.bytesize,
            parity=device.parity,
            stopbits=device.stopbits,
            timeout=device.timeout_s,
        )
    raise ValueError(f"неизвестный transport: {device.transport}")


def _detect_unit_kwarg(client: Any) -> str | None:
    """Как в этой версии pymodbus называется аргумент адреса устройства."""
    try:
        params = inspect.signature(client.read_holding_registers).parameters
    except (TypeError, ValueError):
        return "slave"
    for candidate in ("device_id", "slave", "unit"):
        if candidate in params:
            return candidate
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return "slave"
    return None


class ModbusReader:
    """Один экземпляр на устройство. Держит соединение и читает блоки."""

    def __init__(self, device: Device) -> None:
        self.device = device
        self._client: Any = None
        self._unit_kwarg: str | None = None
        self._lock = asyncio.Lock()
        self._backoff = device.reconnect_delay_s
        self.connected = False
        # Статистика для /metrics
        self.reads_ok = 0
        self.reads_failed = 0
        self.reconnects = 0

    # -- соединение --------------------------------------------------------
    async def connect(self) -> bool:
        async with self._lock:
            return await self._connect_unlocked()

    async def _connect_unlocked(self) -> bool:
        if self._client is None:
            self._client = _build_client(self.device)
            self._unit_kwarg = _detect_unit_kwarg(self._client)
            log.debug("pymodbus: аргумент адреса устройства = %r", self._unit_kwarg)
        try:
            await self._client.connect()
        except Exception as exc:  # noqa: BLE001 — транспорт может бросить что угодно
            log.warning("[%s] подключение не удалось: %s", self.device.name, exc)
            self.connected = False
            return False

        self.connected = bool(getattr(self._client, "connected", True))
        if self.connected:
            log.info("[%s] соединение установлено (%s)", self.device.name, self._endpoint())
            self._backoff = self.device.reconnect_delay_s
        return self.connected

    def _endpoint(self) -> str:
        d = self.device
        if d.transport == "rtu":
            return f"{d.serial_port}@{d.baudrate} unit={d.unit_id}"
        return f"{d.host}:{d.port} unit={d.unit_id}"

    async def close(self) -> None:
        async with self._lock:
            self.connected = False
            if self._client is not None:
                try:
                    result = self._client.close()
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:  # noqa: BLE001
                    log.debug("[%s] ошибка при закрытии: %s", self.device.name, exc)
                self._client = None

    async def reconnect_with_backoff(self) -> bool:
        """Пауза с экспоненциальным ростом, затем попытка подключиться."""
        delay = min(self._backoff, self.device.reconnect_delay_max_s)
        log.info("[%s] переподключение через %.1f с", self.device.name, delay)
        await asyncio.sleep(delay)
        self._backoff = min(self._backoff * 2, self.device.reconnect_delay_max_s)
        self.reconnects += 1
        await self.close()
        return await self.connect()

    # -- чтение ------------------------------------------------------------
    async def read_block(self, register: str, address: int, count: int) -> list[int]:
        """Прочитать блок. Возвращает список слов (или булей для бит).

        Бросает ModbusConnectionLost / ModbusReadError.
        """
        last_error: Exception | None = None

        for attempt in range(self.device.retries + 1):
            if not self.connected and not await self.connect():
                raise ModbusConnectionLost(f"нет соединения с {self._endpoint()}")

            try:
                response = await self._call(register, address, count)
            except (ConnectionException, asyncio.TimeoutError, OSError) as exc:
                last_error = exc
                self.connected = False
                log.debug(
                    "[%s] попытка %d/%d: связь потеряна (%s)",
                    self.device.name, attempt + 1, self.device.retries + 1, exc,
                )
                continue
            except ModbusException as exc:
                last_error = exc
                log.debug(
                    "[%s] попытка %d/%d: %s",
                    self.device.name, attempt + 1, self.device.retries + 1, exc,
                )
                continue

            if response is None or response.isError():
                last_error = ModbusReadError(
                    f"устройство вернуло ошибку на {register}[{address}..{address + count - 1}]: {response}"
                )
                continue

            values = self._extract(response, register, count)
            if len(values) < count:
                last_error = ModbusReadError(
                    f"получено {len(values)} значений вместо {count}"
                )
                continue

            self.reads_ok += 1
            if self.device.inter_request_delay_ms:
                await asyncio.sleep(self.device.inter_request_delay_ms / 1000)
            return values

        self.reads_failed += 1
        if isinstance(last_error, (ConnectionException, OSError, asyncio.TimeoutError)):
            raise ModbusConnectionLost(str(last_error)) from last_error
        raise ModbusReadError(str(last_error) if last_error else "неизвестная ошибка")

    async def _call(self, register: str, address: int, count: int):
        kwargs: dict[str, Any] = {"count": count}
        if self._unit_kwarg:
            kwargs[self._unit_kwarg] = self.device.unit_id

        client = self._client
        reader = {
            "holding": client.read_holding_registers,
            "input": client.read_input_registers,
            "coil": client.read_coils,
            "discrete": client.read_discrete_inputs,
        }[register]

        return await asyncio.wait_for(
            reader(address, **kwargs), timeout=self.device.timeout_s + 1
        )

    @staticmethod
    def _extract(response: Any, register: str, count: int) -> list:
        if register in BIT_REGISTERS:
            return list(getattr(response, "bits", [])[:count])
        return list(getattr(response, "registers", []))
