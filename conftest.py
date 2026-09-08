"""Общая настройка тестов: ядро опроса лежит в poller/src, а не в site-packages."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for path in (ROOT, ROOT / "poller" / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
