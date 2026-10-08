#!/usr/bin/env bash
# ===========================================================================
#  Установка plc2grafana на Linux одной командой (Ubuntu 22.04+, Debian 12+).
#
#  С GitHub:
#    curl -fsSL https://raw.githubusercontent.com/Jorkasys/plc2grafana/main/deploy/install_linux.sh | sudo bash
#
#  Из уже скачанного репозитория:
#    sudo bash deploy/install_linux.sh --source .
#
#  Что делает:
#    1. ставит Python, Git и PostgreSQL из репозиториев дистрибутива;
#    2. создаёт системного пользователя plc2grafana;
#    3. разворачивает приложение в /opt/plc2grafana и его виртуальное окружение;
#    4. задаёт пароль администратора PostgreSQL и кладёт его в /etc/plc2grafana.env —
#       приложение само создаст базу, роли и схему при первом старте;
#    5. скачивает portable-сборку Grafana;
#    6. регистрирует и запускает службу systemd plc2grafana.
#
#  Повторный запуск обновляет код и зависимости, пароли и данные не трогает.
#
#  Параметры (или переменные окружения):
#    --source DIR      взять код из каталога вместо git clone   (PLC_SOURCE)
#    --repo URL        репозиторий                               (PLC_REPO_URL)
#    --branch NAME     ветка, по умолчанию main                  (PLC_BRANCH)
#    --prefix DIR      куда ставить, по умолчанию /opt/plc2grafana (PLC_PREFIX)
#    --port N          порт веб-интерфейса, по умолчанию 8000    (PLC_WEB_PORT)
#    --no-postgres     не ставить PostgreSQL: база уже есть где-то ещё
#    --no-grafana      не скачивать Grafana сейчас (можно позже из интерфейса)
# ===========================================================================
set -euo pipefail

REPO_URL="${PLC_REPO_URL:-https://github.com/Jorkasys/plc2grafana.git}"
BRANCH="${PLC_BRANCH:-main}"
PREFIX="${PLC_PREFIX:-/opt/plc2grafana}"
SOURCE="${PLC_SOURCE:-}"
WEB_PORT="${PLC_WEB_PORT:-8000}"
SERVICE_USER="plc2grafana"
SERVICE_NAME="plc2grafana"
ENV_FILE="/etc/plc2grafana.env"
INSTALL_POSTGRES=1
INSTALL_GRAFANA=1

while [ $# -gt 0 ]; do
  case "$1" in
    --source)      SOURCE="$2"; shift 2 ;;
    --repo)        REPO_URL="$2"; shift 2 ;;
    --branch)      BRANCH="$2"; shift 2 ;;
    --prefix)      PREFIX="$2"; shift 2 ;;
    --port)        WEB_PORT="$2"; shift 2 ;;
    --no-postgres) INSTALL_POSTGRES=0; shift ;;
    --no-grafana)  INSTALL_GRAFANA=0; shift ;;
    -h|--help)     sed -n '2,32p' "$0"; exit 0 ;;
    *) echo "неизвестный параметр: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mОшибка:\033[0m %s\n' "$*" >&2; exit 1; }
as_app() { sudo -u "$SERVICE_USER" -H "$@"; }

[ "$(id -u)" -eq 0 ] || die "запустите через sudo"
command -v apt-get >/dev/null || die "поддерживаются Debian и Ubuntu (apt). На других системах используйте docker compose — см. INSTALL.md"

# --------------------------------------------------------------------------
say "Пакеты системы"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
PACKAGES="git curl ca-certificates sudo python3 python3-venv python3-pip"
[ "$INSTALL_POSTGRES" -eq 1 ] && PACKAGES="$PACKAGES postgresql"
# shellcheck disable=SC2086
apt-get install -y -qq $PACKAGES >/dev/null

# Нужен Python 3.11+. В Ubuntu 22.04 системный — 3.10, но 3.11 есть в universe.
PY=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null &&
     "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    PY="$(command -v "$candidate")"; break
  fi
done
if [ -z "$PY" ]; then
  say "Системный Python старше 3.11 — ставим python3.11"
  apt-get install -y -qq python3.11 python3.11-venv >/dev/null ||
    die "не удалось поставить python3.11. Подключите PPA deadsnakes или используйте docker"
  PY="$(command -v python3.11)"
fi
echo "Python: $("$PY" --version)"

# --------------------------------------------------------------------------
say "Пользователь службы и каталог $PREFIX"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$PREFIX" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
# Доступ к последовательным портам — для Modbus RTU через USB-RS485
usermod -aG dialout "$SERVICE_USER" 2>/dev/null || true
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$PREFIX"

# --------------------------------------------------------------------------
if [ -n "$SOURCE" ]; then
  SOURCE="$(cd "$SOURCE" && pwd)"
  say "Код из $SOURCE"
  tar -C "$SOURCE" \
      --exclude=./.git --exclude=./.venv --exclude=./runtime \
      --exclude=./config/app.json --exclude='*/__pycache__' \
      -cf - . | tar -C "$PREFIX" -xf -
  chown -R "$SERVICE_USER:$SERVICE_USER" "$PREFIX"
elif [ -d "$PREFIX/.git" ]; then
  say "Обновление кода ($BRANCH)"
  as_app git -C "$PREFIX" fetch --quiet origin "$BRANCH"
  as_app git -C "$PREFIX" checkout --quiet "$BRANCH"
  as_app git -C "$PREFIX" pull --quiet --ff-only origin "$BRANCH"
elif [ -z "$(ls -A "$PREFIX" 2>/dev/null | grep -v '^\.' || true)" ]; then
  say "Клонирование $REPO_URL"
  as_app git clone --quiet --branch "$BRANCH" "$REPO_URL" "$PREFIX/.src"
  # Каталог уже существует (домашний у пользователя) — переносим содержимое
  as_app bash -c "shopt -s dotglob && mv '$PREFIX/.src'/* '$PREFIX/' && rmdir '$PREFIX/.src'"
else
  die "$PREFIX не пуст и не является git-клоном. Укажите --source или другой --prefix"
fi
[ -f "$PREFIX/run.py" ] || die "в $PREFIX нет run.py — код не развернулся"

# --------------------------------------------------------------------------
say "Виртуальное окружение и зависимости"
if [ ! -x "$PREFIX/.venv/bin/python" ]; then
  as_app "$PY" -m venv "$PREFIX/.venv"
fi
as_app "$PREFIX/.venv/bin/python" -m pip install --quiet --no-cache-dir --upgrade pip
as_app "$PREFIX/.venv/bin/python" -m pip install --quiet --no-cache-dir -r "$PREFIX/requirements.txt"

# --------------------------------------------------------------------------
ADMIN_PASSWORD=""
if [ -f "$ENV_FILE" ]; then
  say "Настройки уже есть в $ENV_FILE — оставляем как есть"
elif [ "$INSTALL_POSTGRES" -eq 1 ]; then
  say "PostgreSQL"
  systemctl enable --now postgresql >/dev/null 2>&1 || service postgresql start
  for _ in $(seq 1 30); do
    sudo -u postgres psql -qtAc 'SELECT 1' >/dev/null 2>&1 && break
    sleep 1
  done
  # Пароль из безопасного алфавита [A-Za-z0-9_-]: кавычки в SQL не нужны экранировать
  ADMIN_PASSWORD="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(24))')"
  sudo -u postgres psql -q -v ON_ERROR_STOP=1 \
       -c "ALTER USER postgres WITH PASSWORD '$ADMIN_PASSWORD'" >/dev/null
  echo "Пароль пользователя postgres задан и сохранён в $ENV_FILE"
fi

if [ ! -f "$ENV_FILE" ]; then
  umask 027
  {
    echo "# Настройки службы plc2grafana. Читаются при каждом старте."
    echo "# После правки: sudo systemctl restart $SERVICE_NAME"
    echo "PLC_WEB_HOST=0.0.0.0"
    echo "PLC_WEB_PORT=$WEB_PORT"
    echo "PLC_DB_HOST=127.0.0.1"
    echo "PLC_DB_PORT=5432"
    echo "PLC_DB_NAME=plc"
    echo "PLC_DB_ADMIN_USER=postgres"
    echo "PLC_DB_ADMIN_PASSWORD=$ADMIN_PASSWORD"
    if [ -n "$ADMIN_PASSWORD" ]; then
      echo "# Создать базу, роли и схему при первом старте без мастера в браузере"
      echo "PLC_DB_AUTO_SETUP=1"
    else
      echo "# Укажите адрес и учётку своего PostgreSQL выше и включите автонастройку,"
      echo "# либо оставьте 0 и пройдите мастер настройки в браузере"
      echo "PLC_DB_AUTO_SETUP=0"
    fi
  } > "$ENV_FILE"
  chown "root:$SERVICE_USER" "$ENV_FILE"
  chmod 640 "$ENV_FILE"
fi

# --------------------------------------------------------------------------
if [ "$INSTALL_GRAFANA" -eq 1 ]; then
  say "Grafana (portable-сборка, ~250 МБ — один раз)"
  as_app bash -c "cd '$PREFIX' && .venv/bin/python run.py --install-grafana" ||
    warn "Grafana не скачалась — это можно сделать позже кнопкой в интерфейсе"
fi

# --------------------------------------------------------------------------
say "Служба systemd"
cat > "/etc/systemd/system/$SERVICE_NAME.service" <<UNIT
[Unit]
Description=plc2grafana: опрос Modbus, история в PostgreSQL, дашборды Grafana
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$PREFIX
EnvironmentFile=$ENV_FILE
ExecStart=$PREFIX/.venv/bin/python run.py --no-browser
Restart=on-failure
RestartSec=5
# Grafana — дочерний процесс приложения; при остановке гасим всю группу
KillMode=control-group
TimeoutStopSec=30
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
systemctl restart "$SERVICE_NAME"

# --------------------------------------------------------------------------
say "Проверка"
for _ in $(seq 1 60); do
  curl -fsS "http://127.0.0.1:$WEB_PORT/healthz" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "http://127.0.0.1:$WEB_PORT/healthz" >/dev/null 2>&1 ||
  die "приложение не отвечает. Журнал: journalctl -u $SERVICE_NAME -n 100"

if grep -q '^PLC_DB_AUTO_SETUP=1' "$ENV_FILE"; then
  for _ in $(seq 1 90); do
    curl -fsS "http://127.0.0.1:$WEB_PORT/api/status" 2>/dev/null |
      grep -q '"connected":true' && break
    sleep 1
  done
  if curl -fsS "http://127.0.0.1:$WEB_PORT/api/status" | grep -q '"connected":true'; then
    echo "База данных создана и подключена"
  else
    warn "база пока не подключена — смотрите journalctl -u $SERVICE_NAME"
  fi
fi

ADDRESSES="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -v '^$' | grep -v ':' || true)"
echo
echo "Готово. Интерфейс:"
echo "  http://localhost:$WEB_PORT"
for ip in $ADDRESSES; do echo "  http://$ip:$WEB_PORT"; done
echo
echo "Управление службой:"
echo "  sudo systemctl status $SERVICE_NAME"
echo "  sudo journalctl -u $SERVICE_NAME -f"
echo "  sudo systemctl restart $SERVICE_NAME"
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q 'Status: active'; then
  echo
  echo "Включён брандмауэр ufw. Чтобы открыть интерфейс и Grafana для своей сети:"
  echo "  sudo ufw allow from 192.168.0.0/16 to any port $WEB_PORT proto tcp"
  echo "  sudo ufw allow from 192.168.0.0/16 to any port 3000 proto tcp"
fi
