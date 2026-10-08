"""Локальная Grafana под управлением приложения.

Скачивает portable-сборку в ``runtime/grafana``, готовит provisioning
(датасорс + папка с дашбордами) и запускает сервер как дочерний процесс.
Ничего не устанавливается в систему — всё лежит внутри проекта.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import shutil
import subprocess
import tarfile
import time
import zipfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ..settings import RUNTIME_DIR, Settings

log = logging.getLogger("app.grafana")

DOWNLOAD_BASE = "https://dl.grafana.com/oss/release"

GRAFANA_HOME = RUNTIME_DIR / "grafana"
GRAFANA_DATA = RUNTIME_DIR / "grafana-data"
GRAFANA_LOGS = RUNTIME_DIR / "grafana-logs"
GRAFANA_PLUGINS = RUNTIME_DIR / "grafana-plugins"
PROVISIONING_DIR = RUNTIME_DIR / "grafana-provisioning"
DASHBOARD_DIR = RUNTIME_DIR / "grafana-dashboards"

DATASOURCE_UID = "plc-postgres"
DASHBOARD_FOLDER = "PLC"


@dataclass
class DownloadState:
    active: bool = False
    percent: float = 0.0
    downloaded: int = 0
    total: int = 0
    stage: str = ""
    error: str | None = None


@dataclass
class ProcessState:
    running: bool = False
    pid: int | None = None
    started_at: float | None = None
    exit_code: int | None = None
    log_tail: deque = field(default_factory=lambda: deque(maxlen=200))


class GrafanaError(RuntimeError):
    """Понятная человеку ошибка установки или запуска."""


class GrafanaManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.download = DownloadState()
        self.process = ProcessState()
        self._proc: subprocess.Popen | None = None
        self._reader: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._adopted_pid: int | None = None
        self._adopt()

    # ------------------------------------------------------- прошлый запуск
    def _adopt(self) -> None:
        """Подхватить Grafana, оставшуюся от прошлого запуска приложения.

        Если приложение убили жёстко (Ctrl+Break, taskkill), дочерний процесс
        переживает его и держит порт. Без этого следующий запуск молча
        поднимал бы вторую Grafana, которая падала бы на bind.
        """
        pid = _read_pid_file()
        if pid is None or not _alive(pid):
            PID_FILE.unlink(missing_ok=True)
            return
        self._adopted_pid = pid
        self.process = ProcessState(running=True, pid=pid)
        log.info("подхвачена Grafana из прошлого запуска: pid=%s", pid)

    # ------------------------------------------------------------------ пути
    @property
    def cfg(self):
        return self.settings.grafana

    @property
    def bind_host(self) -> str:
        """Где слушает встроенная Grafana: по умолчанию там же, где интерфейс."""
        return self.cfg.bind_host or self.settings.server.host or "127.0.0.1"

    @property
    def exposed(self) -> bool:
        """Доступна ли Grafana с других компьютеров."""
        return self.bind_host not in {"127.0.0.1", "localhost", "::1"}

    @property
    def api_url(self) -> str:
        """Адрес для запросов самого приложения к Grafana."""
        if not self.cfg.managed:
            return self.cfg.url
        host = self.bind_host
        if host in {"0.0.0.0", "::", ""}:
            host = "127.0.0.1"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.cfg.port}"

    @property
    def install_dir(self) -> Path:
        return GRAFANA_HOME / f"grafana-v{self.cfg.version}"

    @property
    def installed(self) -> bool:
        return self._binary() is not None

    def _binary(self) -> Path | None:
        if not self.install_dir.is_dir():
            return None
        suffix = ".exe" if os.name == "nt" else ""
        # Сначала настоящий сервер: grafana-server — устаревшая обёртка, которая
        # запускает «grafana server» дочерним процессом. Остановив обёртку,
        # мы оставили бы сам сервер висеть на порту (так и было в Windows).
        for name in (f"grafana{suffix}", f"grafana-server{suffix}"):
            candidate = self.install_dir / "bin" / name
            if candidate.is_file():
                return candidate
        return None

    def archive_name(self) -> str:
        system = platform.system().lower()
        machine = platform.machine().lower()
        arch = {"amd64": "amd64", "x86_64": "amd64",
                "arm64": "arm64", "aarch64": "arm64"}.get(machine)
        if arch is None:
            raise GrafanaError(f"неподдерживаемая архитектура: {machine}")
        version = self.cfg.version
        if system == "windows":
            return f"grafana-{version}.windows-{arch}.zip"
        if system == "darwin":
            return f"grafana-{version}.darwin-{arch}.tar.gz"
        return f"grafana-{version}.linux-{arch}.tar.gz"

    # ------------------------------------------------------------- установка
    async def install(self, force: bool = False) -> dict[str, Any]:
        """Скачать и распаковать portable Grafana. Идемпотентно."""
        if self.installed and not force:
            return {"installed": True, "path": str(self.install_dir), "skipped": True}
        if self.download.active:
            raise GrafanaError("установка уже идёт")

        self.download = DownloadState(active=True, stage="скачивание")
        archive = self.archive_name()
        url = f"{DOWNLOAD_BASE}/{archive}"
        GRAFANA_HOME.mkdir(parents=True, exist_ok=True)
        target = GRAFANA_HOME / archive

        try:
            await self._download(url, target)
            self.download.stage = "распаковка"
            await asyncio.to_thread(self._extract, target, GRAFANA_HOME)
            if self._binary() is None:
                raise GrafanaError(
                    f"архив распакован, но исполняемый файл не найден в {self.install_dir}")
            target.unlink(missing_ok=True)
            self.download.stage = "очистка"
            freed = await asyncio.to_thread(self.prune)
            log.info("удалено лишнего из дистрибутива Grafana: %.0f МБ", freed / 1024 / 1024)
            self.download.stage = "готово"
            self.download.percent = 100.0
            log.info("Grafana %s установлена в %s", self.cfg.version, self.install_dir)
            return {"installed": True, "path": str(self.install_dir), "skipped": False}
        except Exception as exc:  # noqa: BLE001
            self.download.error = str(exc)
            self.download.stage = "ошибка"
            log.error("установка Grafana не удалась: %s", exc)
            raise GrafanaError(str(exc)) from exc
        finally:
            self.download.active = False

    async def _download(self, url: str, target: Path) -> None:
        tmp = target.with_suffix(target.suffix + ".part")
        timeout = httpx.Timeout(30.0, read=120.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise GrafanaError(
                        f"сервер загрузок ответил {response.status_code} на {url}")
                total = int(response.headers.get("content-length") or 0)
                self.download.total = total
                done = 0
                with tmp.open("wb") as fh:
                    async for chunk in response.aiter_bytes(256 * 1024):
                        fh.write(chunk)
                        done += len(chunk)
                        self.download.downloaded = done
                        if total:
                            self.download.percent = round(done / total * 100, 1)
        tmp.replace(target)

    @staticmethod
    def _extract(archive: Path, dest: Path) -> None:
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(dest)
        else:
            with tarfile.open(archive, "r:gz") as tf:
                _safe_extract(tf, dest)

    def prune(self) -> int:
        """Выбросить из дистрибутива то, что серверу не нужно.

        Распакованная Grafana занимает ~2 ГБ, из которых больше половины —
        карты исходников для отладки в браузере, storybook и документация.
        Без них сервер работает точно так же. Возвращает освобождённые байты.
        """
        freed = 0
        for name in ("storybook", "docs", "npm-artifacts", "packaging"):
            directory = self.install_dir / name
            if directory.is_dir():
                freed += _dir_size(directory)
                shutil.rmtree(directory, ignore_errors=True)
        for source_map in self.install_dir.rglob("*.map"):
            try:
                size = source_map.stat().st_size
                source_map.unlink()
                freed += size
            except OSError:
                continue
        return freed

    def uninstall(self) -> None:
        shutil.rmtree(GRAFANA_HOME, ignore_errors=True)

    # ------------------------------------------------------------- provisioning
    def write_provisioning(self) -> None:
        """Датасорс и провайдер дашбордов. Читается Grafana при старте."""
        # Пустые каталоги нужны все: без них Grafana при старте пишет в лог
        # ошибки «can't read provisioning files from directory».
        for name in ("datasources", "dashboards", "plugins", "notifiers",
                     "alerting", "access-control"):
            (PROVISIONING_DIR / name).mkdir(parents=True, exist_ok=True)
        for path in (DASHBOARD_DIR, GRAFANA_DATA, GRAFANA_LOGS, GRAFANA_PLUGINS):
            path.mkdir(parents=True, exist_ok=True)

        db = self.settings.database
        (PROVISIONING_DIR / "datasources" / "plc.yaml").write_text(
            "apiVersion: 1\n"
            "\n"
            "deleteDatasources:\n"
            f"  - name: {_yaml(_datasource_name())}\n"
            "    orgId: 1\n"
            "\n"
            "datasources:\n"
            f"  - name: {_yaml(_datasource_name())}\n"
            f"    uid: {DATASOURCE_UID}\n"
            "    type: postgres\n"
            "    access: proxy\n"
            f"    url: {_yaml(f'{db.host}:{db.port}')}\n"
            f"    user: {_yaml(db.readonly_user)}\n"
            f"    database: {_yaml(db.name)}\n"
            "    isDefault: true\n"
            "    editable: false\n"
            "    secureJsonData:\n"
            f"      password: {_yaml(db.readonly_password)}\n"
            "    jsonData:\n"
            "      sslmode: disable\n"
            "      postgresVersion: 1600\n"
            "      maxOpenConns: 10\n"
            "      maxIdleConns: 4\n"
            "      connMaxLifetime: 14400\n"
            "      timescaledb: false\n",
            encoding="utf-8",
        )

        (PROVISIONING_DIR / "dashboards" / "plc.yaml").write_text(
            "apiVersion: 1\n"
            "\n"
            "providers:\n"
            "  - name: plc2grafana\n"
            "    orgId: 1\n"
            f"    folder: {DASHBOARD_FOLDER}\n"
            "    type: file\n"
            "    disableDeletion: false\n"
            "    updateIntervalSeconds: 10\n"
            "    allowUiUpdates: true\n"
            "    options:\n"
            f"      path: {_yaml(str(DASHBOARD_DIR))}\n"
            "      foldersFromFilesStructure: false\n",
            encoding="utf-8",
        )

    def set_timescale(self, enabled: bool) -> None:
        """Отметить в датасорсе, что база — TimescaleDB (макросы $__timeGroup)."""
        path = PROVISIONING_DIR / "datasources" / "plc.yaml"
        if not path.is_file():
            return
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace("timescaledb: false", f"timescaledb: {str(enabled).lower()}"),
            encoding="utf-8")

    # ----------------------------------------------------------------- запуск
    async def start(self) -> dict[str, Any]:
        if not self.cfg.managed:
            raise GrafanaError("используется внешняя Grafana — запускать нечего")
        if self.process.running and await self.health():
            return self.status()
        # Порт может держать чужая Grafana или прошлый запуск, который мы уже
        # не контролируем. Молча «запускать» вторую бессмысленно: она упадёт
        # на bind, а health-проверка увидит чужую и решит, что всё хорошо.
        if not self.process.running and await self.health():
            raise GrafanaError(
                f"порт {self.cfg.port} уже занят другой Grafana. Остановите её "
                f"или укажите другой порт на странице «Система»")

        async with self._lock:
            # Автозапуск и кнопка в интерфейсе могут прийти одновременно:
            # второй вызов не должен поднимать ещё один процесс на тот же порт
            if self._proc is not None and self._proc.poll() is None:
                return self.status()
            binary = self._binary()
            if binary is None:
                raise GrafanaError("Grafana не установлена — нажмите «Установить»")

            self.write_provisioning()
            env = os.environ.copy()
            env.update(self._env())
            args = [str(binary), "--homepath", str(self.install_dir)]
            if binary.stem in {"grafana", "grafana.exe"}:
                args.insert(1, "server")

            try:
                self._proc = subprocess.Popen(  # noqa: S603
                    args,
                    cwd=str(self.install_dir),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
            except OSError as exc:
                raise GrafanaError(f"не удалось запустить {binary}: {exc}") from exc

            self.process = ProcessState(running=True, pid=self._proc.pid,
                                        started_at=time.time())
            self._adopted_pid = None
            _write_pid_file(self._proc.pid, self.cfg.port)
            self._reader = asyncio.create_task(self._pump_output(), name="grafana-log")
            log.info("Grafana запускается: pid=%s, порт %d", self._proc.pid, self.cfg.port)

        ok = await self.wait_healthy(timeout_s=60)
        if not ok:
            tail = "\n".join(list(self.process.log_tail)[-8:])
            raise GrafanaError(
                f"Grafana не поднялась за 60 секунд. Последние строки лога:\n{tail}")
        return self.status()

    def _env(self) -> dict[str, str]:
        cfg = self.cfg
        return {
            "GF_PATHS_DATA": str(GRAFANA_DATA),
            "GF_PATHS_LOGS": str(GRAFANA_LOGS),
            "GF_PATHS_PLUGINS": str(GRAFANA_PLUGINS),
            "GF_PATHS_PROVISIONING": str(PROVISIONING_DIR),
            "GF_SERVER_HTTP_ADDR": self.bind_host,
            "GF_SERVER_HTTP_PORT": str(cfg.port),
            "GF_SERVER_ROOT_URL": (cfg.public_url.rstrip("/") + "/") if cfg.public_url
                                  else f"http://localhost:{cfg.port}/",
            "GF_SECURITY_ADMIN_USER": cfg.admin_user,
            "GF_SECURITY_ADMIN_PASSWORD": cfg.admin_password,
            # Нужно, чтобы дашборды открывались в iframe внутри нашего интерфейса
            "GF_SECURITY_ALLOW_EMBEDDING": "true",
            "GF_SECURITY_COOKIE_SAMESITE": "lax",
            "GF_AUTH_ANONYMOUS_ENABLED": "true" if cfg.anonymous_view else "false",
            "GF_AUTH_ANONYMOUS_ORG_ROLE": "Viewer",
            "GF_USERS_ALLOW_SIGN_UP": "false",
            "GF_USERS_DEFAULT_THEME": "dark",
            "GF_ANALYTICS_REPORTING_ENABLED": "false",
            "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
            "GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES": "false",
            # Grafana 11.6 при старте пытается доустановить приложения-плагины
            # с grafana.com. Без интернета это ломает отрисовку дашбордов
            # (SystemJS не может загрузить модуль плагина), поэтому выключаем.
            "GF_PLUGINS_PREINSTALL": "",
            "GF_PLUGINS_PREINSTALL_DISABLED": "true",
            "GF_PLUGIN_ADMIN_ENABLED": "false",
            "GF_LIVE_ALLOWED_ORIGINS": "*" if self.exposed and not cfg.public_url else "",
            "GF_LOG_MODE": "console file",
            "GF_LOG_LEVEL": "info",
        }

    async def _pump_output(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        loop = asyncio.get_running_loop()
        try:
            while True:
                line = await loop.run_in_executor(None, proc.stdout.readline)
                if not line:
                    break
                self.process.log_tail.append(line.rstrip())
        except asyncio.CancelledError:
            raise
        finally:
            code = proc.poll()
            if code is not None:
                self.process.running = False
                self.process.exit_code = code
                log.warning("процесс Grafana завершился с кодом %s", code)

    async def autostart(self) -> None:
        """Поднять Grafana вместе с приложением, если это уместно.

        Ошибки только логируем: недоступная Grafana не должна мешать опросу.
        """
        cfg = self.cfg
        if not (cfg.managed and cfg.enabled and cfg.autostart and self.installed):
            return
        if self.process.running and await self.health():
            return
        try:
            await self.start()
            log.info("Grafana запущена автоматически: %s", self.api_url)
        except GrafanaError as exc:
            log.warning("автозапуск Grafana не удался: %s", exc)

    async def stop(self) -> dict[str, Any]:
        async with self._lock:
            proc, self._proc = self._proc, None
            if self._reader is not None:
                self._reader.cancel()
                await asyncio.gather(self._reader, return_exceptions=True)
                self._reader = None
            if proc is not None and proc.poll() is None:
                if os.name == "nt":
                    # Гасим всё дерево: у старых сборок под сервером может
                    # оказаться дочерний процесс
                    await asyncio.to_thread(_terminate_pid, proc.pid)
                else:
                    proc.terminate()
                try:
                    await asyncio.to_thread(proc.wait, 15)
                except Exception:  # noqa: BLE001
                    proc.kill()
            elif self._adopted_pid is not None:
                await asyncio.to_thread(_terminate_pid, self._adopted_pid)
            self._adopted_pid = None
            PID_FILE.unlink(missing_ok=True)
            self.process.running = False
            self.process.pid = None
            self.process.exit_code = proc.poll() if proc else None
        return self.status()

    # ----------------------------------------------------------------- статус
    async def wait_healthy(self, timeout_s: float = 60) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                return False
            if await self.health():
                return True
            await asyncio.sleep(1.0)
        return False

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(f"{self.api_url}/api/health")
                return response.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def status(self) -> dict[str, Any]:
        if self._proc is not None and self._proc.poll() is not None:
            self.process.running = False
            self.process.exit_code = self._proc.poll()
            PID_FILE.unlink(missing_ok=True)
        elif self._adopted_pid is not None and not _alive(self._adopted_pid):
            self.process.running = False
            self._adopted_pid = None
            PID_FILE.unlink(missing_ok=True)
        return {
            "managed": self.cfg.managed,
            "enabled": self.cfg.enabled,
            "installed": self.installed,
            "version": self.cfg.version,
            "install_dir": str(self.install_dir),
            "url": self.api_url,
            "browser_url": self.cfg.browser_url,
            "bind_host": self.bind_host if self.cfg.managed else None,
            "exposed": self.exposed if self.cfg.managed else None,
            "autostart": self.cfg.autostart,
            "port": self.cfg.port,
            "admin_user": self.cfg.admin_user,
            "running": self.process.running,
            "pid": self.process.pid,
            "started_at": self.process.started_at,
            "exit_code": self.process.exit_code,
            "download": {
                "active": self.download.active,
                "percent": self.download.percent,
                "downloaded": self.download.downloaded,
                "total": self.download.total,
                "stage": self.download.stage,
                "error": self.download.error,
            },
            "log_tail": list(self.process.log_tail)[-40:],
            "dashboard_dir": str(DASHBOARD_DIR),
        }


PID_FILE = RUNTIME_DIR / "grafana.pid"


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _write_pid_file(pid: int, port: int) -> None:
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(f"{pid} {port}", encoding="utf-8")
    except OSError as exc:  # noqa: BLE001
        log.debug("не удалось записать %s: %s", PID_FILE, exc)


def _read_pid_file() -> int | None:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(  # noqa: S603,S607
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        return str(pid) in (out.stdout or "")
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _terminate_pid(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],  # noqa: S603,S607
                       capture_output=True)
        return
    import signal

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def _yaml(value: str) -> str:
    """Строка в YAML-совместимом виде (пароли содержат что угодно)."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _datasource_name() -> str:
    return "PLC PostgreSQL"


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Распаковка с защитой от путей вида ../.."""
    dest = dest.resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(dest)):
            raise GrafanaError(f"архив содержит небезопасный путь: {member.name}")
    tar.extractall(dest)
