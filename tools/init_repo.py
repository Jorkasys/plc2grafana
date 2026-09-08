#!/usr/bin/env python3
"""Подготовить репозиторий к первой отправке на GitHub.

Подставляет в README ссылки на ваш репозиторий (значки CI и адрес демо),
инициализирует git, делает первый коммит и прописывает remote.

    python tools/init_repo.py https://github.com/ВАШ-ЛОГИН/plc2grafana

После этого остаётся один шаг:

    git push -u origin main
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"

# Русский текст в консоли Windows иначе превращается в кракозябры:
# по умолчанию там cp866/cp1251, а не UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

COMMIT_MESSAGE = """plc2grafana: локальное приложение Modbus → PostgreSQL → Grafana

Веб-интерфейс с мастером подключения устройства, автоопределением типов
тегов по карте регистров, цифровым двойником производства и автоматической
установкой и настройкой Grafana.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
"""

URL_RE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:)"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)/"
    r"(?P<repo>[A-Za-z0-9._-]+?)(?:\.git)?/?$"
)


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    if check and result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)}:\n{result.stderr.strip()}")
    return (result.stdout or "").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", help="адрес репозитория, например "
                                    "https://github.com/user/plc2grafana")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--no-commit", action="store_true",
                        help="только подставить ссылки, ничего не коммитить")
    args = parser.parse_args()

    match = URL_RE.match(args.url.strip())
    if not match:
        raise SystemExit(
            "не похоже на адрес репозитория GitHub.\n"
            "Ожидается https://github.com/ЛОГИН/ИМЯ или git@github.com:ЛОГИН/ИМЯ.git")
    owner, repo = match["owner"], match["repo"]

    # 1. Ссылки в README
    text = README.read_text(encoding="utf-8")
    updated = text.replace("OWNER/REPO", f"{owner}/{repo}") \
                  .replace("OWNER.github.io/REPO", f"{owner}.github.io/{repo}")
    if updated != text:
        README.write_text(updated, encoding="utf-8")
        print(f"README: ссылки заменены на {owner}/{repo}")
    else:
        print("README: заменять нечего — ссылки уже подставлены")

    if args.no_commit:
        return 0

    # 2. Репозиторий
    if not (ROOT / ".git").is_dir():
        git("init", "-b", args.branch)
        print(f"git: создан репозиторий, ветка {args.branch}")
    elif git("rev-parse", "--abbrev-ref", "HEAD", check=False) in {"", "HEAD"}:
        git("checkout", "-b", args.branch, check=False)

    git("add", "-A")
    if git("status", "--porcelain"):
        git("commit", "-m", COMMIT_MESSAGE)
        print("git: коммит создан")
    else:
        print("git: изменений нет, коммит не нужен")

    # 3. remote
    existing = git("remote", "get-url", "origin", check=False)
    if not existing:
        git("remote", "add", "origin", args.url)
        print(f"git: origin → {args.url}")
    elif existing != args.url:
        git("remote", "set-url", "origin", args.url)
        print(f"git: origin изменён на {args.url}")

    print()
    print("Осталось два шага:")
    print(f"  1. Создайте пустой репозиторий {owner}/{repo} на GitHub "
          "(без README и .gitignore)")
    print(f"  2. git push -u origin {args.branch}")
    print()
    print("Затем в Settings → Pages выберите источник «GitHub Actions» — "
          "демо появится по адресу")
    print(f"  https://{owner}.github.io/{repo}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
