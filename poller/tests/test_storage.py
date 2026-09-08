"""Тесты буферизации и восстановления соединения с БД.

Регрессия: раньше после сбоя пул обнулялся и больше не поднимался —
буфер рос до переполнения, данные терялись.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from modbus_logger.config import DatabaseConfig
from modbus_logger.storage import QUALITY_GOOD, Sample, Storage


def sample(tag_id=1, value=1.0):
    return Sample(ts=datetime.now(timezone.utc), tag_id=tag_id, value=value,
                  value_text=None, quality=QUALITY_GOOD)


class FakePool:
    """Минимальная замена asyncpg.Pool: считает записанные строки."""

    def __init__(self, fail: bool = False):
        self.rows: list[tuple] = []
        self.fail = fail
        self.closed = False

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *_):
                return False

        return _Ctx()

    async def copy_records_to_table(self, _table, *, records, columns):
        if self.fail:
            raise ConnectionError("БД недоступна")
        self.rows.extend(records)

    async def close(self):
        self.closed = True

    def terminate(self):
        self.closed = True


def make_storage(**kw):
    cfg = DatabaseConfig(batch_max_rows=10, batch_max_delay_ms=10, **kw)
    return Storage(cfg)


async def test_flush_writes_rows():
    st = make_storage()
    st._pool = FakePool()
    for i in range(5):
        st.enqueue(sample(value=float(i)))
    await st._flush_once()
    assert len(st._pool.rows) == 5
    assert st.rows_written == 5
    assert st.pending == 0


async def test_failed_flush_returns_rows_to_buffer():
    st = make_storage()
    st._pool = FakePool(fail=True)
    for i in range(5):
        st.enqueue(sample(value=float(i)))
    with pytest.raises(ConnectionError):
        await st._flush_once()
    # данные не потеряны и лежат в исходном порядке
    assert st.pending == 5
    assert [s.value for s in st._buffer] == [0.0, 1.0, 2.0, 3.0, 4.0]
    assert st.rows_written == 0


async def test_flush_without_pool_raises():
    """Без этого цикл записи «засыпал» навсегда и не переподключался."""
    st = make_storage()
    st._pool = None
    st.enqueue(sample())
    with pytest.raises(ConnectionError):
        await st._flush_once()


async def test_flush_loop_reconnects_after_outage(monkeypatch):
    st = make_storage()
    st._pool = None
    attempts = {"n": 0}

    async def fake_connect(retry_forever=True):
        attempts["n"] += 1
        if attempts["n"] < 2:          # первая попытка — БД ещё лежит
            raise ConnectionError("нет связи")
        st._pool = FakePool()
        st.db_connected = True

    monkeypatch.setattr(st, "_connect", fake_connect)

    for i in range(4):
        st.enqueue(sample(value=float(i)))

    task = asyncio.create_task(st._flush_loop())
    await asyncio.sleep(2.0)
    st._stopping.set()
    st._wake.set()
    await asyncio.wait_for(task, timeout=5)

    assert attempts["n"] >= 2, "цикл записи должен был повторять попытки подключения"
    assert st.pending == 0, "после восстановления буфер обязан быть дописан"
    assert st.rows_written == 4


async def test_buffer_overflow_drops_oldest():
    st = make_storage(buffer_max_rows=3)
    for i in range(5):
        st.enqueue(sample(value=float(i)))
    assert st.pending == 3
    assert st.rows_dropped == 2
    # остались самые свежие
    assert [s.value for s in st._buffer] == [2.0, 3.0, 4.0]
