"""Группировка тегов в блоки чтения.

Читать 40 тегов сорока запросами — верный способ получить таймауты и
перегрузить слабый шлюз. Здесь соседние адреса объединяются в один запрос
(FC3/FC4 — до 125 регистров, FC1/FC2 — до 2000 бит за раз).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import BlockConfig, Tag

# Жёсткие лимиты протокола Modbus
MAX_REGISTERS_PER_READ = 125
MAX_BITS_PER_READ = 2000

BIT_REGISTERS = {"coil", "discrete"}


@dataclass(slots=True)
class ReadBlock:
    """Один Modbus-запрос и теги, которые из него декодируются."""

    register: str
    address: int
    count: int
    poll_interval_ms: int
    tags: list[Tag] = field(default_factory=list)
    # служебное: время следующего опроса (monotonic), заполняет поллер
    next_due: float = 0.0

    @property
    def end(self) -> int:
        return self.address + self.count

    def slice_for(self, tag: Tag, values: list) -> list:
        """Вырезать из ответа блока значения, относящиеся к тегу."""
        start = tag.address - self.address
        return values[start : start + tag.count]

    def __str__(self) -> str:
        return (
            f"{self.register}[{self.address}..{self.end - 1}] "
            f"x{self.count} @{self.poll_interval_ms}ms ({len(self.tags)} тегов)"
        )


def build_blocks(tags: list[Tag], cfg: BlockConfig) -> list[ReadBlock]:
    """Разложить теги устройства на блоки чтения.

    Теги группируются по (тип регистра, период опроса) — смешивать периоды
    в одном запросе нельзя, иначе быстрый тег будет читаться редко.
    """
    groups: dict[tuple[str, int], list[Tag]] = {}
    for tag in tags:
        if not tag.enabled:
            continue
        groups.setdefault((tag.register, tag.poll_interval_ms), []).append(tag)

    blocks: list[ReadBlock] = []
    for (register, interval), group in sorted(groups.items()):
        is_bits = register in BIT_REGISTERS
        hard_limit = MAX_BITS_PER_READ if is_bits else MAX_REGISTERS_PER_READ
        max_span = min(cfg.max_registers, hard_limit)
        if is_bits:
            # для битов лимит на порядок выше — не режем без нужды
            max_span = min(max(cfg.max_registers, 1) * 8, hard_limit)

        group.sort(key=lambda t: (t.address, t.count))
        current: ReadBlock | None = None

        for tag in group:
            tag_end = tag.address + tag.count
            if current is not None:
                gap = tag.address - current.end
                span = tag_end - current.address
                if gap <= cfg.max_gap and span <= max_span:
                    current.count = max(current.count, tag_end - current.address)
                    current.tags.append(tag)
                    continue
            current = ReadBlock(
                register=register,
                address=tag.address,
                count=tag.count,
                poll_interval_ms=interval,
                tags=[tag],
            )
            blocks.append(current)

    return blocks


def summarize(blocks: list[ReadBlock]) -> str:
    """Короткая сводка для лога при старте."""
    total_tags = sum(len(b.tags) for b in blocks)
    lines = [f"{len(blocks)} блок(ов) чтения, {total_tags} тегов:"]
    lines += [f"  - {b}" for b in blocks]
    return "\n".join(lines)
