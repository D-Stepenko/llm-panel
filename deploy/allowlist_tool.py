#!/usr/bin/env python3
"""allowlist_tool — безопасный редактор /etc/llm-panel/allowlist.json (v0.3.20).

Зачем: править JSON по каждому юниту вручную неудобно и опасно (опечатка
ломает ВСЕ права разом — helper при невалидном JSON отказывает всем юнитам).
Инструмент даёт простые команды с валидацией, резервной копией перед каждой
записью и атомарной заменой файла (tmp + os.replace, root:root 0644).

Команды:
  list                              показать юниты и глобальные ключи
  check                             проверить файл (валидация без правки)
  add-unit <unit> [--actions ...]   добавить юнит в allowlist
  remove-unit <unit>                удалить юнит из allowlist
  grant <unit> <action> [...]       добавить действия юниту
  revoke <unit> <action> [...]      убрать действия у юнита
  set-global <key> <true|false>     глобальный ключ: reset_failed | daemon_reload
  sync-services [--services PATH]   добавить юниты из services.yaml, которых
                                    ещё нет в allowlist (существующие НЕ трогает)

Безопасность:
  * запись только после валидации (имена юнитов, действия, JSON-структура);
  * перед каждой записью — backup allowlist.json.bak-<timestamp> (последние 5);
  * атомарная запись: tmp в том же каталоге + os.replace, права root:root 0644;
  * целевой файл по умолчанию /etc/llm-panel/allowlist.json — правится только
    от root (sudo); для тестов есть --file (не-root работает только с --file);
  * ключи-комментарии («_note», «_power», ...) и порядок ключей сохраняются.

Запуск на станции (из корня ~/llm-panel):
  sudo .venv/bin/python deploy/allowlist_tool.py sync-services --dry-run
  sudo .venv/bin/python deploy/allowlist_tool.py grant vllm.service unit-cat unit-write
"""

import argparse
import json
import os
import re
import sys
import time

DEFAULT_PATH = "/etc/llm-panel/allowlist.json"
# Тот же шаблон, что у helper (UNIT_RE): только *.service, безопасные символы.
UNIT_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}\.service$")
# Полный набор per-unit действий helper (deploy/helper/llm-panel-systemd ACTIONS).
KNOWN_ACTIONS = ("start", "stop", "restart", "enable", "disable", "reset-failed",
                 "unit-cat", "unit-write", "status", "journal", "journal-follow")
# Глобальные ключи (один на менеджер, покрывают все юниты allowlist).
GLOBAL_KEYS = ("reset_failed", "daemon_reload")
# Дефолт для sync-services: управление юнитом БЕЗ правки юнит-файла —
# unit-cat/unit-write остаются строго per-unit (решение владельца, v0.3.19).
DEFAULT_ACTIONS = ("start", "stop", "restart", "enable", "disable", "reset-failed",
                   "status", "journal", "journal-follow")
BACKUPS_KEEP = 5


def die(msg: str, code: int = 2):
    print(f"ОШИБКА: {msg}", file=sys.stderr)
    sys.exit(code)


# ----------------------------------------------------------------- загрузка --

def load_allowlist(path: str) -> dict:
    """Читает и валидирует allowlist; при ошибке — die (ничего не меняем)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        die(f"файл не найден: {path}")
    except json.JSONDecodeError as e:
        die(f"невалидный JSON ({path}): {e} — файл НЕ изменён; "
            f"восстановите из backup (allowlist.json.bak-*) или исправьте вручную")
    except OSError as e:
        die(f"файл нечитаем ({path}): {type(e).__name__}")
    if not isinstance(data, dict):
        die(f"{path}: корень JSON должен быть объектом")
    units = data.get("units", {})
    if not isinstance(units, dict):
        die(f"{path}: секция \"units\" должна быть объектом")
    for unit, entry in units.items():
        validate_unit(unit)
        if entry is not None and not isinstance(entry, dict):
            die(f'{path}: запись юнита {unit} должна быть объектом')
        actions = (entry or {}).get("actions", [])
        if not isinstance(actions, list):
            die(f'{path}: {unit}: "actions" должен быть массивом')
        for a in actions:
            if a not in KNOWN_ACTIONS:
                die(f'{path}: {unit}: неизвестное действие "{a}" '
                    f"(известные: {', '.join(KNOWN_ACTIONS)})")
    return data


def validate_unit(unit: str) -> str:
    if not unit or not UNIT_RE.match(unit):
        die(f"имя юнита не соответствует шаблону {UNIT_RE.pattern!r}: {unit!r} "
            "(имя пишется точно как в systemctl, с суффиксом .service)")
    return unit


def validate_actions(actions) -> list:
    cleaned = [a.strip() for a in actions if isinstance(a, str) and a.strip()]
    if not cleaned:
        die("набор действий пуст")
    for a in cleaned:
        if a not in KNOWN_ACTIONS:
            die(f'неизвестное действие "{a}" (известные: {", ".join(KNOWN_ACTIONS)})')
    if len(set(cleaned)) != len(cleaned):
        die("дубликаты в наборе действий")
    return cleaned


# ------------------------------------------------------------------ запись --

def save_allowlist(path: str, data: dict, dry_run: bool) -> None:
    """Backup + атомарная запись (tmp + os.replace, root:root 0644)."""
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if dry_run:
        print("DRY-RUN: файл НЕ записан.")
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = f"{path}.bak-{stamp}"
    tmp = f"{path}.tmp-{os.getpid()}"
    try:
        with open(path, "rb") as src:
            old = src.read()
        with open(backup, "wb") as dst:
            dst.write(old)
        _prune_backups(path)
        with open(tmp, "w", encoding="utf-8", newline="\n") as dst:
            dst.write(text)
        os.chmod(tmp, 0o644)
        if hasattr(os, "chown") and os.geteuid() == 0:
            os.chown(tmp, 0, 0)
        os.replace(tmp, path)
    except OSError as e:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        die(f"запись не выполнена: {type(e).__name__} ({e})", 4)
    print(f"OK: записано {path} (backup: {backup})")


def _prune_backups(path: str) -> None:
    """Оставляет последние BACKUPS_KEEP backup-файлов allowlist.json.bak-*."""
    directory = os.path.dirname(path) or "."
    prefix = os.path.basename(path) + ".bak-"
    try:
        baks = sorted(f for f in os.listdir(directory) if f.startswith(prefix))
    except OSError:
        return
    for name in baks[:-BACKUPS_KEEP]:
        try:
            os.unlink(os.path.join(directory, name))
        except OSError:
            pass


# ----------------------------------------------------------------- команды --

def cmd_list(args) -> int:
    data = load_allowlist(args.file)
    units = data.get("units", {})
    print(f"Файл: {args.file}")
    for key in GLOBAL_KEYS:
        print(f'  {key}: {data.get(key, "(не задан — отказ)")}')
    print(f"Юнитов в allowlist: {len(units)}")
    for unit, entry in sorted(units.items()):
        actions = ", ".join((entry or {}).get("actions", [])) or "(пусто)"
        print(f"  {unit}: {actions}")
    return 0


def cmd_check(args) -> int:
    load_allowlist(args.file)  # die при любой проблеме
    print(f"OK: {args.file} валиден.")
    return 0


def cmd_add_unit(args) -> int:
    data = load_allowlist(args.file)
    validate_unit(args.unit)
    units = data.setdefault("units", {})
    if args.unit in units:
        die(f'юнит {args.unit} уже в allowlist (используйте grant/revoke)')
    actions = validate_actions(args.actions.split(",") if args.actions
                               else list(DEFAULT_ACTIONS))
    entry: dict = {"actions": actions}
    if args.note:
        entry["note"] = args.note
    units[args.unit] = entry
    print(f'Юнит {args.unit}: добавлен, actions: {", ".join(actions)}')
    save_allowlist(args.file, data, args.dry_run)
    return 0


def cmd_remove_unit(args) -> int:
    data = load_allowlist(args.file)
    validate_unit(args.unit)
    units = data.setdefault("units", {})
    if args.unit not in units:
        die(f'юнита {args.unit} нет в allowlist')
    del units[args.unit]
    print(f"Юнит {args.unit}: удалён из allowlist.")
    save_allowlist(args.file, data, args.dry_run)
    return 0


def _get_entry(data: dict, unit: str) -> dict:
    units = data.setdefault("units", {})
    if unit not in units:
        die(f'юнита {unit} нет в allowlist (сначала add-unit или sync-services)')
    if not isinstance(units[unit], dict):
        units[unit] = {"actions": []}
    if not isinstance(units[unit].get("actions", []), list):
        die(f'у юнита {unit} "actions" не массив — исправьте файл вручную')
    return units[unit]


def cmd_grant(args) -> int:
    data = load_allowlist(args.file)
    validate_unit(args.unit)
    actions = validate_actions(args.actions)
    entry = _get_entry(data, args.unit)
    added = [a for a in actions if a not in entry["actions"]]
    if not added:
        print("Изменений нет: все действия уже разрешены.")
        return 0
    entry["actions"] = list(entry["actions"]) + added
    print(f'Юнит {args.unit}: + {", ".join(added)}')
    save_allowlist(args.file, data, args.dry_run)
    return 0


def cmd_revoke(args) -> int:
    data = load_allowlist(args.file)
    validate_unit(args.unit)
    actions = validate_actions(args.actions)
    entry = _get_entry(data, args.unit)
    removed = [a for a in actions if a in entry["actions"]]
    if not removed:
        print("Изменений нет: этих действий у юнита и так нет.")
        return 0
    entry["actions"] = [a for a in entry["actions"] if a not in removed]
    print(f'Юнит {args.unit}: − {", ".join(removed)}')
    save_allowlist(args.file, data, args.dry_run)
    return 0


def cmd_set_global(args) -> int:
    if args.key not in GLOBAL_KEYS:
        die(f'неизвестный глобальный ключ "{args.key}" '
            f"(известные: {', '.join(GLOBAL_KEYS)})")
    if args.value not in ("true", "false"):
        die("значение должно быть true или false")
    data = load_allowlist(args.file)
    new = args.value == "true"
    if data.get(args.key) is new:
        print(f"Изменений нет: {args.key} уже {args.value}.")
        return 0
    data[args.key] = new
    print(f"Глобальный ключ {args.key} = {new} (для ВСЕХ юнитов allowlist).")
    save_allowlist(args.file, data, args.dry_run)
    return 0


def cmd_sync_services(args) -> int:
    """Добавляет в allowlist юниты из services.yaml, которых там ещё нет.

    User-юниты (scope: user) пропускаются — им allowlist не нужен (панель
    работает systemctl --user напрямую). Существующие записи НЕ меняются.
    """
    try:
        import yaml  # PyYAML есть в venv панели (панель читает services.yaml)
    except ImportError:
        die("нужен PyYAML — запустите через venv панели: "
            ".venv/bin/python deploy/allowlist_tool.py ...")
    try:
        with open(args.services, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except FileNotFoundError:
        die(f"файл не найден: {args.services}")
    except OSError as e:
        die(f"файл нечитаем ({args.services}): {type(e).__name__}")
    if not isinstance(doc, dict) or not isinstance(doc.get("services"), list):
        die(f"{args.services}: ожидается структура \"services\": [...]")

    data = load_allowlist(args.file)
    units = data.setdefault("units", {})
    actions = (args.actions.split(",") if args.actions else None)
    added, skipped, user_skipped = [], [], []
    for svc in doc["services"]:
        if not isinstance(svc, dict):
            continue
        unit = svc.get("unit")
        if not unit:
            continue
        if svc.get("scope") == "user":
            user_skipped.append(unit)
            continue
        validate_unit(unit)
        if unit in units:
            skipped.append(unit)
            continue
        entry: dict = {"actions": validate_actions(
            actions if actions is not None else list(DEFAULT_ACTIONS))}
        if svc.get("name"):
            entry["note"] = f"из services.yaml: {svc['name']}"
        units[unit] = entry
        added.append(unit)

    if added:
        print("Добавлены юниты:")
        for u in added:
            print(f'  + {u}: {", ".join(units[u]["actions"])}')
    else:
        print("Новых юнитов нет (allowlist уже актуален).")
    if skipped:
        print(f"Уже были в allowlist (не тронуты): {', '.join(skipped)}")
    if user_skipped:
        print(f"User-юниты пропущены (allowlist не нужен): {', '.join(user_skipped)}")
    save_allowlist(args.file, data, args.dry_run)
    return 0


def _add_file(sp) -> None:
    """--file доступен и до, и после подкоманды. SUPPRESS: если после команды
    не задан — значение верхнего уровня (или DEFAULT_PATH) не затирается."""
    sp.add_argument("--file", default=argparse.SUPPRESS, metavar="PATH",
                    help=f"путь к allowlist (default: {DEFAULT_PATH})")


def _add_dry(sp) -> None:
    sp.add_argument("--dry-run", action="store_true",
                    help="только показать изменения, файл НЕ записывать")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="allowlist_tool",
        description="Безопасный редактор /etc/llm-panel/allowlist.json (backup + валидация + атомарная запись).",
    )
    p.add_argument("--file", default=None, metavar="PATH",
                   help=f"путь к allowlist (по умолчанию {DEFAULT_PATH}; не-root — только с --file)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list", help="показать юниты и глобальные ключи")
    _add_file(sp)
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("check", help="валидация файла без правки")
    _add_file(sp)
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("add-unit", help="добавить юнит в allowlist")
    sp.add_argument("unit")
    sp.add_argument("--actions", help="действия через запятую (по умолчанию: управление без unit-cat/unit-write)")
    sp.add_argument("--note", help="комментарий к записи")
    _add_file(sp)
    _add_dry(sp)
    sp.set_defaults(func=cmd_add_unit)

    sp = sub.add_parser("remove-unit", help="удалить юнит из allowlist")
    sp.add_argument("unit")
    _add_file(sp)
    _add_dry(sp)
    sp.set_defaults(func=cmd_remove_unit)

    sp = sub.add_parser("grant", help="добавить действия юниту")
    sp.add_argument("unit")
    sp.add_argument("actions", nargs="+")
    _add_file(sp)
    _add_dry(sp)
    sp.set_defaults(func=cmd_grant)

    sp = sub.add_parser("revoke", help="убрать действия у юнита")
    sp.add_argument("unit")
    sp.add_argument("actions", nargs="+")
    _add_file(sp)
    _add_dry(sp)
    sp.set_defaults(func=cmd_revoke)

    sp = sub.add_parser("set-global", help=f"глобальный ключ: {' | '.join(GLOBAL_KEYS)}")
    sp.add_argument("key")
    sp.add_argument("value", help="true | false")
    _add_file(sp)
    _add_dry(sp)
    sp.set_defaults(func=cmd_set_global)

    sp = sub.add_parser("sync-services",
                        help="добавить юниты из services.yaml, отсутствующие в allowlist")
    sp.add_argument("--services", default="/etc/llm-panel/services.yaml",
                    help="файл описаний сервисов панели (default: /etc/llm-panel/services.yaml)")
    sp.add_argument("--actions", help="действия для НОВЫХ юнитов через запятую (default: DEFAULT_ACTIONS)")
    _add_file(sp)
    _add_dry(sp)
    sp.set_defaults(func=cmd_sync_services)

    args = p.parse_args(argv)
    args.file = getattr(args, "file", None) or DEFAULT_PATH

    # Правка системного allowlist — только от root; --file снимает ограничение.
    if args.file == DEFAULT_PATH and hasattr(os, "geteuid") and os.geteuid() != 0:
        die("файл /etc/llm-panel/allowlist.json правится через sudo: "
            "sudo .venv/bin/python deploy/allowlist_tool.py ... "
            "(для тестов используйте --file)")

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
