from modbus_logger.blocks import build_blocks
from modbus_logger.config import BlockConfig, Tag


def tag(name, address, datatype="uint16", register="holding", interval=1000, **kw):
    return Tag(device="d", name=name, address=address, register=register,
               datatype=datatype, poll_interval_ms=interval, **kw)


def test_adjacent_tags_merge():
    tags = [tag("a", 0), tag("b", 1), tag("c", 2)]
    blocks = build_blocks(tags, BlockConfig(max_registers=100, max_gap=8))
    assert len(blocks) == 1
    assert (blocks[0].address, blocks[0].count) == (0, 3)


def test_large_gap_splits():
    tags = [tag("a", 0), tag("b", 500)]
    blocks = build_blocks(tags, BlockConfig(max_registers=100, max_gap=8))
    assert len(blocks) == 2


def test_small_gap_merges():
    tags = [tag("a", 0), tag("b", 5)]
    blocks = build_blocks(tags, BlockConfig(max_registers=100, max_gap=8))
    assert len(blocks) == 1
    assert blocks[0].count == 6


def test_max_registers_respected():
    tags = [tag(f"t{i}", i * 4) for i in range(60)]
    blocks = build_blocks(tags, BlockConfig(max_registers=50, max_gap=8))
    assert blocks
    assert all(b.count <= 50 for b in blocks)


def test_different_intervals_not_merged():
    tags = [tag("fast", 0, interval=100), tag("slow", 1, interval=5000)]
    blocks = build_blocks(tags, BlockConfig())
    assert len(blocks) == 2


def test_different_register_types_not_merged():
    tags = [tag("h", 0), tag("i", 0, register="input")]
    blocks = build_blocks(tags, BlockConfig())
    assert len(blocks) == 2


def test_float32_spans_two_registers():
    tags = [tag("f", 0, datatype="float32"), tag("g", 2, datatype="float32")]
    blocks = build_blocks(tags, BlockConfig())
    assert len(blocks) == 1
    assert blocks[0].count == 4


def test_disabled_tag_skipped():
    tags = [tag("a", 0), tag("b", 1, enabled=False)]
    blocks = build_blocks(tags, BlockConfig())
    assert sum(len(b.tags) for b in blocks) == 1


def test_slice_for_extracts_right_window():
    tags = [tag("a", 10, datatype="float32"), tag("b", 12, datatype="uint16")]
    block = build_blocks(tags, BlockConfig())[0]
    values = [0xAAAA, 0xBBBB, 0xCCCC]
    assert block.slice_for(tags[0], values) == [0xAAAA, 0xBBBB]
    assert block.slice_for(tags[1], values) == [0xCCCC]
