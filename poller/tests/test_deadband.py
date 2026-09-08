import time

from modbus_logger.config import Tag
from modbus_logger.poller import DevicePoller, TagState
from modbus_logger.storage import QUALITY_BAD_COMM, QUALITY_GOOD

should_write = DevicePoller._should_write


def make_tag(**kw):
    base = dict(device="d", name="t", address=0, register="holding", datatype="float32")
    base.update(kw)
    return Tag(**base)


def fresh_state(value, quality=QUALITY_GOOD):
    return TagState(last_value=value, last_quality=quality, last_written=time.monotonic())


def test_first_value_always_written():
    assert should_write(make_tag(), TagState(), 1.0, QUALITY_GOOD)


def test_no_deadband_writes_every_sample():
    tag = make_tag(deadband=0)
    assert should_write(tag, fresh_state(10.0), 10.0, QUALITY_GOOD)


def test_absolute_deadband_suppresses_small_change():
    tag = make_tag(deadband=0.5, heartbeat_s=0)
    state = fresh_state(10.0)
    assert not should_write(tag, state, 10.2, QUALITY_GOOD)
    assert should_write(tag, state, 10.6, QUALITY_GOOD)


def test_percent_deadband():
    tag = make_tag(deadband=5, deadband_mode="percent", heartbeat_s=0)
    state = fresh_state(100.0)
    assert not should_write(tag, state, 103.0, QUALITY_GOOD)
    assert should_write(tag, state, 106.0, QUALITY_GOOD)


def test_quality_change_always_written():
    tag = make_tag(deadband=100, heartbeat_s=0)
    state = fresh_state(10.0)
    assert should_write(tag, state, None, QUALITY_BAD_COMM)


def test_heartbeat_forces_write():
    tag = make_tag(deadband=100, heartbeat_s=1)
    state = TagState(last_value=10.0, last_quality=QUALITY_GOOD,
                     last_written=time.monotonic() - 5)
    assert should_write(tag, state, 10.0, QUALITY_GOOD)


def test_repeated_bad_quality_not_spammed():
    tag = make_tag(heartbeat_s=0)
    state = fresh_state(None, QUALITY_BAD_COMM)
    assert not should_write(tag, state, None, QUALITY_BAD_COMM)


def test_string_change_detected():
    tag = make_tag(datatype="string", length=4, heartbeat_s=0)
    state = fresh_state("A")
    assert not should_write(tag, state, "A", QUALITY_GOOD)
    assert should_write(tag, state, "B", QUALITY_GOOD)
