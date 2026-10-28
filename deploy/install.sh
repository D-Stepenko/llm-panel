#!/usr/bin/env bash
# install.sh — универсальная установка llm-panel на сервер с systemd (ТЗ §12).
#
# Запуск ОТ ROOT (sudo), с указанием пользователя, от которого будет работать панель:
#   sudo bash deploy/install.sh --user dispot
# Полный список параметров: sudo bash deploy/install.sh --help
#
# Принципы безопасности:
#   * существующие файлы в каталогах конфигурации НИКОГДА не перезаписываются
#     (обновление кода — через git pull, см. docs/DEPLOY.md §7);
#   * sudoers-правило ставится ТОЛЬКО при явном --with-sudoers (ТЗ §12.5);
#   * пароль администратора панели задаётся отдельно интерактивно (не в этом скрипте).
set -euo pipefail

PANEL_USER="${SUDO_USER:-}"
PANEL_DIR=""
CONFIG_DIR="/etc/llm-panel"
DATA_DIR="/var/lib/llm-panel"
WITH_SUDOERS=0
START_NOW=1
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --user)       PANEL_USER="$2"; shift 2 ;;
    --dir)        PANEL_DIR="$2"; shift 2 ;;
    --config-dir) CONFIG_DIR="$2"; shift 2 ;;
    --data-dir)   DATA_DIR="$2"; shift 2 ;;
    --with-sudoers) WITH_SUDOERS=1; shift ;;
    --no-start)   START_NOW=0; shift ;;
    --help|-h)    usage ;;
    *) echo "Неизвестный параметр: $1" >&2; usage ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "ОШИБКА: запускайте от root (sudo bash deploy/install.sh ...)"; exit 2; }
[ -n "$PANEL_USER" ] || { echo "ОШИБКА: укажите --user <пользователь панели>"; exit 2; }
id "$PANEL_USER" >/dev/null 2>&1 || { echo "ОШИБКА: пользователь $PANEL_USER не существует"; exit 2; }
PANEL_GROUP="$(id -gn "$PANEL_USER")"
[ -n "$PANEL_DIR" ] || PANEL_DIR="$(getent passwd "$PANEL_USER" | cut -d: -f6)/llm-panel"
CONFIG_FILE="$CONFIG_DIR/config.yaml"

echo "=== llm-panel install ==="
echo "  пользователь : $PANEL_USER"
echo "  каталог кода : $PANEL_DIR"
echo "  конфигурация : $CONFIG_DIR"
echo "  данные       : $DATA_DIR"
echo

# 1. Код: копируем из репозитория (скрипт лежит в deploy/ репозитория).
if [ "$REPO_DIR" != "$PANEL_DIR" ]; then
  mkdir -p "$PANEL_DIR"
  cp -r "$REPO_DIR"/llm_panel "$REPO_DIR"/requirements.txt "$REPO_DIR"/pyproject.toml "$PANEL_DIR"/
fi
chown -R "$PANEL_USER:$PANEL_GROUP" "$PANEL_DIR"

# 2. Окружение: venv в домашнем каталоге пользователя (не системный Python — ТЗ §7).
if [ ! -x "$PANEL_DIR/.venv/bin/python" ]; then
  echo ">> Создание venv (python3.12 или совместимый)..."
  PYTHON_BIN="$(command -v python3.12 || command -v python3)"
  sudo -u "$PANEL_USER" "$PYTHON_BIN" -m venv "$PANEL_DIR/.venv"
fi
echo ">> Установка зависимостей..."
sudo -u "$PANEL_USER" "$PANEL_DIR/.venv/bin/pip" install -q -r "$PANEL_DIR/requirements.txt"

# 3. Каталоги конфигурации и данных.
mkdir -p "$CONFIG_DIR" "$DATA_DIR"
chown "$PANEL_USER:$PANEL_GROUP" "$DATA_DIR"
chmod 700 "$DATA_DIR"

# 4. Конфиги: только если их ещё нет (существующие НЕ перезаписываем).
install_example() {  # <src> <dst> <mode>
  if [ -f "$2" ]; then
    echo "   $2 уже существует — НЕ перезаписан"
  else
    install -m "$3" "$1" "$2"
    echo "   создан $2 (шаблон) — ОТРЕДАКТИРУЙТЕ под свой сервер"
  fi
}
echo ">> Конфигурационные файлы:"
install_example "$REPO_DIR/deploy/config.yaml.example"    "$CONFIG_DIR/config.yaml"    644
install_example "$REPO_DIR/deploy/services.yaml.example"  "$CONFIG_DIR/services.yaml"  644
install_example "$REPO_DIR/deploy/allowlist.json.example" "$CONFIG_DIR/allowlist.json" 644
chown root:root "$CONFIG_DIR"/config.yaml "$CONFIG_DIR"/services.yaml "$CONFIG_DIR"/allowlist.json 2>/dev/null || true

# 5. Helper: root-owned, путь недоступен пользователю на запись.
echo ">> Helper:"
install -o root -g root -m 755 "$REPO_DIR/deploy/helper/llm-panel-systemd" /usr/local/sbin/llm-panel-systemd
echo "   /usr/local/sbin/llm-panel-systemd установлен (root:root 0755)"

# 6. sudoers — ТОЛЬКО при явном подтверждении (ТЗ §12.5).
if [ "$WITH_SUDOERS" -eq 1 ]; then
  sed -e "s/^dispot /${PANEL_USER} /" "$REPO_DIR/deploy/sudoers.llm-panel" \
    > "/etc/sudoers.d/llm-panel"
  chmod 0440 /etc/sudoers.d/llm-panel
  visudo -c >/dev/null || { echo "ОШИБКА: sudoers невалиден, удаляю"; rm -f /etc/sudoers.d/llm-panel; exit 2; }
  echo ">> sudoers-правило установлено: $PANEL_USER может запускать только helper (NOPASSWD)"
else
  echo ">> sudoers НЕ установлен (ТЗ §12.5). Без него управление сервисами из панели"
  echo "   работать не будет. Установка вручную после подтверждения — docs/DEPLOY.md §4.6."
fi

# 7. БД.
echo ">> Инициализация БД..."
sudo -u "$PANEL_USER" "$PANEL_DIR/.venv/bin/python" -m llm_panel --config "$CONFIG_FILE" init-db

# 8. systemd-юнит панели из шаблона.
sed -e "s|@@PANEL_USER@@|$PANEL_USER|g" \
    -e "s|@@PANEL_GROUP@@|$PANEL_GROUP|g" \
    -e "s|@@PANEL_DIR@@|$PANEL_DIR|g" \
    -e "s|@@CONFIG_FILE@@|$CONFIG_FILE|g" \
    "$REPO_DIR/deploy/llm-panel.service.template" > /etc/systemd/system/llm-panel.service
chmod 644 /etc/systemd/system/llm-panel.service
systemctl daemon-reload

echo
echo "=== Осталось вручную ==="
echo "1. Отредактируйте $CONFIG_DIR/config.yaml (trusted_hosts, models.roots)."
echo "2. Отредактируйте $CONFIG_DIR/services.yaml и allowlist.json под свои юниты"
echo "   (GPU-UUID: nvidia-smi -L; обследование: bash scripts/survey.sh)."
echo "3. Задайте пароль администратора:"
echo "   sudo -u $PANEL_USER $PANEL_DIR/.venv/bin/python -m llm_panel --config $CONFIG_FILE set-password"
if [ "$WITH_SUDOERS" -ne 1 ]; then
  echo "4. sudoers (после подтверждения): docs/DEPLOY.md §4.6."
fi
if [ "$START_NOW" -eq 1 ]; then
  systemctl enable --now llm-panel
  echo
  echo ">> Панель запущена: systemctl status llm-panel --no-pager"
  echo "   Наружу публикуется через Caddy (docs/DEPLOY.md §5), слушает 127.0.0.1."
else
  echo "Запуск: sudo systemctl enable --now llm-panel"
fi
