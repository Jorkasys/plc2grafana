"""plc2grafana — локальное веб-приложение: Modbus → PostgreSQL → Grafana."""

from __future__ import annotations

import sys
from pathlib import Path

__version__ = "2.0.0"

# Ядро опроса живёт в poller/src и ставится как отдельный пакет только для
# тестов. Здесь оно нужно как обычная библиотека — добавляем путь один раз.
_CORE = Path(__file__).resolve().parent.parent / "poller" / "src"
if _CORE.is_dir() and str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))
