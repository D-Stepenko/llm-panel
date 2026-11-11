"""Чтение состояния systemd-юнитов (read-only, direct systemctl show).

ТЗ §5.3: изменяющие операции (start/stop/restart/enable/disable) — только через
привилегированный helper. Чтение статуса выполняется напрямую без sudo, чтобы
не дёргать sudo каждые 2 секунды; если чтение недоступно — «нет данных».

Все вызовы subprocess без shell=True (ТЗ §5.3).
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import pathlib
import subprocess


@dataclasses.dataclass(frozen=True)
class UnitInfo:
    active: str          # ActiveState
    sub: str             # SubState
    result: str          # Result
    pid: int
    since: str           # ExecMainStartTimestamp (raw) / ISO
    nrestarts: int
    unit_file_state: str
    error: str = ""
    load_state: str = "" # LoadState ("loaded"/"found"/...; "not-found" = юнита нет)


_PROPERTIES = [
    "ActiveState",
    "SubState",
    "Result",
    "ExecMainPID",
    "ExecMainStartTimestamp",
    "ExecMainExitTimestamp",
    "NRestarts",
    "UnitFileState",
    "LoadState",
]

_TIME_FORMATS = ("%a %Y-%m-%d %H:%M:%S %Z", "%a %Y-%m-%d %H:%M:%S")


def _parse_since(raw: str):
    for fmt in _TIME_FORMATS:
        try:
            return _dt.datetime.strptime(raw, fmt).isoformat()
        except ValueError:
            continue
    return raw


def _user_env() -> dict | None:
    """Окружение для user-вызовов (systemctl --user / journalctl --user).

    llm-panel — СИСТЕМНЫЙ сервис, работающий от пользователя-владельца юнитов
    (dispot): у процесса нет XDG_RUNTIME_DIR, и без него systemctl --user
    отвечает «Failed to connect to bus: No medium found» (наблюдение станции
    2026-10-28: карточка user-юнита unknown, скан user-юнитов пуст). Явно
    задаём runtime-каталог сессии этого пользователя /run/user/<uid>.
    None — uid определить нельзя (Windows dev, вызовы и так не сработают).
    """
    import os
    try:
        uid = os.getuid()
    except AttributeError:
        return None
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    return env


def show_unit(unit: str, timeout: float = 5.0, scope: str = "system") -> UnitInfo | None:
    """Состояние одного юнита; None при отсутствии юнита либо недоступности systemd.

    scope="system" — системный менеджер (systemctl show); scope="user" —
    user-менеджер пользователя панели (systemctl --user show).
    """
    if not unit or "/" in unit or "\x00" in unit:
        return None
    argv = ["systemctl"]
    if scope == "user":
        argv.append("--user")
    argv.append("show")
    argv.append("--no-pager")
    for p in _PROPERTIES:
        argv.append(f"--property={p}")
    argv.append("--")
    argv.append(unit)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              env=_user_env() if scope == "user" else None)
    except FileNotFoundError:
        return None  # systemd недоступен (dev-режим на Windows и т.п.)
    except subprocess.TimeoutExpired:
        return None

    if proc.returncode != 0:
        return None
    return _info_from_show(proc.stdout)


def _info_from_show(stdout: str) -> UnitInfo | None:
    """Разбор вывода `systemctl show --property=...`; None — юнита нет.

    ВАЖНО: `systemctl show` (включая --user) отвечает кодом 0 и НЕПУСТЫМ
    ActiveState (inactive) даже для несуществующего юнита — наличие юнита
    проверяется по LoadState ("not-found" = юнита нет). Наблюдение станции
    2026-10-28: через user-менеджер «находились» любые имена *.service.
    """
    vals: dict[str, str] = {}
    for line in stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            vals[key.strip()] = value.strip()
    if vals.get("LoadState", "") in ("", "not-found"):
        return None
    if not vals.get("ActiveState"):
        return None
    return UnitInfo(
        active=vals.get("ActiveState", "unknown"),
        sub=vals.get("SubState", ""),
        result=vals.get("Result", ""),
        pid=int(vals.get("ExecMainPID", "0") or 0),
        since=_parse_since(vals.get("ExecMainStartTimestamp", "")),
        nrestarts=int(vals.get("NRestarts", "0") or 0),
        unit_file_state=vals.get("UnitFileState", ""),
        error="",
        load_state=vals.get("LoadState", ""),
    )


def _run_list(argv: list[str], timeout: float, env: dict | None = None):
    """subprocess.run для list-units (отделено для удобного monkeypatch в тестах).

    Возврат None — команда недоступна/таймаут (dev на Windows и т.п.).
    """
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              env=env)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return None
    return proc


def _parse_list_output(stdout: str) -> list[dict]:
    """Разбор вывода `systemctl list-units --type=service --all --no-legend --plain`."""
    import re

    out: list[dict] = []
    for line in stdout.splitlines():
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 5:
            continue
        unit, load, active, sub, desc = parts[0], parts[1], parts[2], parts[3], parts[4]
        if not unit.endswith(".service") or load == "not-found":
            continue
        out.append({"unit": unit, "active": active, "sub": sub, "desc": desc})
    return out


def list_loaded_services(timeout: float = 10.0) -> list[dict] | None:
    """Все загруженные systemd-сервисы сервера (read-only, без sudo).

    ТЗ §5.3: чтение состояния выполняется напрямую без helper. Перечисление
    юнитов — тоже read-only операция (list-units --all). Выборка объединяет
    СИСТЕМНЫЙ менеджер (systemctl list-units) и user-менеджер пользователя
    панели (systemctl --user list-units): у user-юнитов поле scope="user"
    (см. §registry — юниты ~/.config/systemd/user не видны системному
    менеджеру, поэтому openclaw и подобные им не попадают в системный скан).
    Юниты с LOAD=not-found не показываются. Возврат None — systemctl
    недоступен вовсе (dev на Windows и т.п.); если user-менеджер недоступен —
    возвращаются только системные юниты.
    """
    argv = ["systemctl", "list-units", "--type=service", "--all", "--no-pager",
            "--no-legend", "--plain"]
    proc = _run_list(argv, timeout)
    if proc is None:
        return None
    out = _parse_list_output(proc.stdout)
    user_argv = ["systemctl", "--user", "list-units", "--type=service", "--all",
                 "--no-pager", "--no-legend", "--plain"]
    uproc = _run_list(user_argv, timeout, _user_env())
    if uproc is not None and uproc.returncode == 0:
        seen = {e["unit"] for e in out}
        for e in _parse_list_output(uproc.stdout):
            if e["unit"] not in seen:
                e["scope"] = "user"
                out.append(e)
    return out


def run_user_action(action: str, unit: str, timeout: float = 90.0) -> tuple[bool, str]:
    """Изменяющая операция над user-юнитом — НАПРЯМУЮ systemctl --user.

    Обоснование (отличие от system-юнитов, ТЗ §5.3): панель работает от того же
    пользователя, чей user-менеджер владеет юнитом (~/.config/systemd/user),
    поэтому helper и root-allowlist не нужны — панель и так ограничена правами
    своего пользователя и не может тронуть чужие user-менеджеры. Действие
    валидируется вызывающей стороной (action из _ACTIONS, unit по UNIT_RE),
    вызов — argv-список без shell.
    """
    if action not in ("start", "stop", "restart", "enable", "disable", "reset-failed"):
        return False, "unsupported action"
    if not unit or "/" in unit or "\x00" in unit:
        return False, "bad unit"
    argv = ["systemctl", "--user", action, "--no-ask-password", "--", unit]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              env=_user_env())
    except FileNotFoundError:
        return False, "systemctl --user недоступен (dev-среда?)"
    except subprocess.TimeoutExpired:
        return False, "timeout"
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out.strip()[:2000]


def daemon_reload_user(timeout: float = 60.0) -> tuple[bool, str]:
    """systemctl --user daemon-reload (v0.3.18): перечитать юниты user-менеджера.

    user-менеджер принадлежит тому же пользователю, от которого работает
    панель, — helper/allowlist не нужны (как run_user_action). Возврат
    (успех, текст вывода).
    """
    argv = ["systemctl", "--user", "daemon-reload", "--no-ask-password"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              env=_user_env())
    except FileNotFoundError:
        return False, "systemctl --user недоступен (dev-среда?)"
    except subprocess.TimeoutExpired:
        return False, "timeout"
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out.strip()[:2000]


def _user_unit_base() -> pathlib.Path:
    """Каталог user-юнитов владельца панели (~/.config/systemd/user).
    Отдельная функция — для точечного monkeypatch в тестах."""
    return pathlib.Path.home() / ".config" / "systemd" / "user"


def _user_unit_path(unit: str) -> pathlib.Path | None:
    """Безопасный путь юнит-файла: строго внутри каталога user-юнитов."""
    if not unit or "/" in unit or "\\" in unit or ".." in unit or "\x00" in unit:
        return None
    base = _user_unit_base()
    p = base / unit
    if p.parent != base:
        return None
    return p


USER_UNIT_MAX_BYTES = 262144


def unit_cat_user(unit: str) -> tuple[bool, str]:
    """unit-cat для user-юнита (v0.3.19): чтение ~/.config/systemd/user/<unit>.

    Панель работает от владельца юнита — helper не нужен (как run_user_action).
    Возврат (успех, содержимое или текст ошибки).
    """
    p = _user_unit_path(unit)
    if p is None:
        return False, "bad unit"
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False, f"юнит-файл не найден: {p} (создание юнитов через панель запрещено)"
    except OSError as e:
        return False, f"юнит-файл нечитаем: {type(e).__name__}"
    return True, text


def unit_write_user(unit: str, content: str) -> tuple[bool, str]:
    """unit-write для user-юнита (v0.3.19): атомарная правка существующего файла.

    Только СУЩЕСТВУЮЩИЕ файлы (создание — вне панели); резервная копия —
    ~/.config/systemd/user/.panel-backups/ перед каждой записью.
    """
    import re as _re

    p = _user_unit_path(unit)
    if p is None:
        return False, "bad unit"
    if not content or not content.strip():
        return False, "пустое содержимое юнит-файла"
    raw = content.encode("utf-8")
    if len(raw) > USER_UNIT_MAX_BYTES:
        return False, f"юнит-файл слишком большой (> {USER_UNIT_MAX_BYTES} байт)"
    if "\x00" in content:
        return False, "ввод содержит NUL — отказ"
    sections = _re.findall(r"^\s*\[([A-Za-z][A-Za-z0-9._-]*)\]", content, _re.M)
    if not any(s in ("Unit", "Service", "Install") for s in sections):
        return False, "ввод не похож на systemd-юнит (нет секции [Unit]/[Service]/[Install])"
    if not p.is_file():
        return False, f"юнит-файл не найден: {p} (создание юнитов через панель запрещено)"
    backup_dir = p.parent / ".panel-backups"
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"{unit}.{stamp}.bak"
    tmp = p.parent / (unit + ".panel-tmp")
    try:
        backup_dir.mkdir(mode=0o700, exist_ok=True)
        backup.write_bytes(p.read_bytes())
        tmp.write_bytes(raw)
        tmp.replace(p)
    except OSError as e:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        return False, f"запись не выполнена: {type(e).__name__} ({e})"
    return True, f"ok ({len(raw)} байт, backup {backup})"


def journal_user_text(unit: str, lines: int, timeout: float = 10.0) -> tuple[int, str]:
    """Последние строки журнала user-юнита: journalctl --user (read-only, как панель).

    Возврат (код, текст): код 0 — успех; 1 — сбой (текст содержит stderr).
    """
    if not unit or "/" in unit or "\x00" in unit:
        return 1, "bad unit"
    argv = ["journalctl", "--user", f"--unit={unit}", f"--lines={int(lines)}",
            "--no-pager", "--no-color"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              env=_user_env())
    except FileNotFoundError:
        return 1, "journalctl недоступен (dev-среда)"
    except subprocess.TimeoutExpired:
        return 1, "timeout"
    if proc.returncode != 0:
        return 1, (proc.stderr or "journalctl error").strip()[:2000]
    return 0, proc.stdout


def spawn_user_follow(unit: str, lines: int):
    """Поток журнала user-юнита: Popen `journalctl --user -f` (для SSE follow)."""
    argv = ["journalctl", "--user", f"--unit={unit}", f"--lines={int(lines)}",
            "--no-pager", "--no-color", "-f"]
    return subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=_user_env())


def list_unit_files(patterns: tuple[str, ...], timeout: float = 5.0) -> dict[str, str]:
    """unit -> UnitFileState. Для отображения факта autostart (ТЗ §4.1)."""
    argv = ["systemctl", "list-unit-files", "--no-pager", "--quiet"]
    for p in patterns:
        argv.append(p)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].endswith(".service"):
            out[parts[0]] = parts[1]
    return out
