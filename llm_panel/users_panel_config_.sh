#!/usr/bin/env bash
# users_panel_config_.sh — управление учётными записями панели llm-panel (v0.3.16).
#
# Запуск на СТАНЦИИ из каталога проекта:  llm_panel/users_panel_config_.sh
# sudo НЕ нужен: учётки — обычные строки SQLite-базы панели (владелец dispot).
# Пароли вводятся интерактивно (getpass, getpass — БЕЗ отображения, не в argv,
# как в самом llm-panel CLI); минимум 10 символов, Argon2-хеш — тем же кодом,
# что и для admin (llm_panel.security.set_password). Смена пароля/создание
# сбрасывают сессии этого пользователя (защита самой панели).
#
# Меню:
#   1 — создать пользователя
#   2 — удалить пользователя
#   3 — изменить пароль пользователю
#   4 — показать всех пользователей
#   0 — выход

set -euo pipefail

# Каталог проекта = каталог НАД llm_panel (здесь лежит этот файл)
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${LLM_PANEL_CONFIG:-/etc/llm-panel/config.yaml}"
PY="$ROOT/.venv/bin/python"

if [ ! -x "$PY" ]; then
    echo "ОШИБКА: не найден интерпретатор панели: $PY" >&2
    echo "       запустите скрипт из каталога проекта (~/llm-panel/llm_panel/)" >&2
    exit 1
fi
if [ ! -f "$CONFIG" ]; then
    echo "ОШИБКА: конфиг панели не найден: $CONFIG" >&2
    echo "       (можно переопределить: LLM_PANEL_CONFIG=/path/config.yaml $0)" >&2
    exit 1
fi

# python_action <действие> — общая обёртка (PYTHONPATH — чтобы импорт
# llm_panel работал и при запуске из каталога llm_panel/, и из корня)
export PYTHONPATH="${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
python_action() {
    "$PY" - "$@" <<'PYEOF'
import getpass
import sys

from llm_panel.config import load_config
from llm_panel.db import Database
from llm_panel.security import get_user, set_password

cfg = load_config(sys.argv[1])
db = Database(cfg.db_path)
cmd = sys.argv[2]

name_re = __import__("re").compile(r"^[a-zA-Z0-9_.-]{1,64}$")

def ask_new_password() -> str | None:
    pw = getpass.getpass("Новый пароль: ")
    if len(pw) < 10:
        print("Пароль должен быть не короче 10 символов.", file=sys.stderr)
        return None
    if pw != getpass.getpass("Повторите пароль: "):
        print("Пароли не совпадают.", file=sys.stderr)
        return None
    return pw

def valid_name(username: str) -> bool:
    if not name_re.match(username or ""):
        print("Имя: только латиница/цифры/_.- , до 64 символов.", file=sys.stderr)
        return False
    return True

if cmd == "create":
    username = input("Имя нового пользователя: ").strip()
    if not valid_name(username):
        sys.exit(2)
    pw = ask_new_password()
    if pw is None:
        sys.exit(2)
    set_password(db, username, pw)
    print(f"Пользователь {username!r} создан (сессии сброшены).")

elif cmd == "delete":
    username = input("Имя пользователя для удаления: ").strip()
    if username == "admin":
        print("Отказ: admin удалить нельзя (через него — восстановление доступа).", file=sys.stderr)
        sys.exit(3)
    if get_user(db, username) is None:
        print(f"Отказ: пользователь {username!r} не найден.", file=sys.stderr)
        sys.exit(3)
    if input(f"Точно удалить {username!r}? (введите yes): ").strip() != "yes":
        print("Отменено.")
        sys.exit(0)
    db.execute("DELETE FROM users WHERE username = ?", (username,))
    db.execute("DELETE FROM sessions WHERE username = ?", (username,))
    print(f"Пользователь {username!r} удалён (сессии также сброшены).")

elif cmd == "passwd":
    username = input("Имя пользователя: ").strip()
    if get_user(db, username) is None:
        print(f"Отказ: пользователь {username!r} не найден.", file=sys.stderr)
        sys.exit(3)
    pw = ask_new_password()
    if pw is None:
        sys.exit(2)
    set_password(db, username, pw)
    print(f"Пароль для {username!r} обновлён (сессии сброшены).")

elif cmd == "list":
    rows = db.query_all(
        "SELECT username, updated_at, password_hash != '!' AS has_password "
        "FROM users ORDER BY username"
    )
    if not rows:
        print("Учётных записей нет.")
        sys.exit(0)
    import datetime as _dt
    print(f"{'ПОЛЬЗОВАТЕЛЬ':<16} {'ОБНОВЛЁН':<20} ПАРОЛЬ")
    for r in rows:
        ts = r["updated_at"] or 0
        dt = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        print(f"{r['username']:<16} {dt:<20} {'есть' if r['has_password'] else 'НЕТ (без пароля)'}")
    print(f"итого: {len(rows)}")

else:
    print(f"неизвестное действие: {cmd}", file=sys.stderr)
    sys.exit(2)
PYEOF
}

while true; do
    echo
    echo "llm-panel: учётные записи  (конфиг: $CONFIG)"
    echo "  1) создать пользователя"
    echo "  2) удалить пользователя"
    echo "  3) изменить пароль пользователю"
    echo "  4) показать всех пользователей"
    echo "  0) выход"
    read -r -p "Выбор [1-4, 0]: " choice || { echo; exit 0; }
    case "$choice" in
        1) python_action "$CONFIG" create ;;
        2) python_action "$CONFIG" delete ;;
        3) python_action "$CONFIG" passwd ;;
        4) python_action "$CONFIG" list ;;
        0|"") echo "Выход."; exit 0 ;;
        *) echo "Нет такого пункта: $choice" ;;
    esac
done
