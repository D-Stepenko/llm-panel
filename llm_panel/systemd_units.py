"""Чтение состояния systemd-юнитов (read-only, direct systemctl show).

ТЗ §5.3: изменяющие операции (start/stop/restart/enable/disable) — только через
привилегированный helper. Чтение статуса выполняется напрямую без sudo, чтобы
не дёргать sudo каждые 2 секунды; если чтение недоступно — «нет данных».

Все вызовы subprocess без shell=True (ТЗ §5.3).
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
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


_PROPERTIES = [
    "ActiveState",
    "SubState",
    "Result",
    "ExecMainPID",
    "ExecMainStartTimestamp",
    "ExecMainExitTimestamp",
    "NRestarts",
    "UnitFileState",
]

_TIME_FORMATS = ("%a %Y-%m-%d %H:%M:%S %Z", "%a %Y-%m-%d %H:%M:%S")


def _parse_since(raw: str):
    for fmt in _TIME_FORMATS:
        try:
            return _dt.datetime.strptime(raw, fmt).isoformat()
        except ValueError:
            continue
    return raw


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
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None  # systemd недоступен (dev-режим на Windows и т.п.)
    except subprocess.TimeoutExpired:
        return None

    if proc.returncode != 0:
        return None
    vals: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            vals[key.strip()] = value.strip()
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
    )


def _run_list(argv: list[str], timeout: float):
    """subprocess.run для list-units (отделено для удобного monkeypatch в тестах).

    Возврат None — команда недоступна/таймаут (dev на Windows и т.п.).
    """
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
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
    uproc = _run_list(user_argv, timeout)
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
    if action not in ("start", "stop", "restart", "enable", "disable"):
        return False, "unsupported action"
    if not unit or "/" in unit or "\x00" in unit:
        return False, "bad unit"
    argv = ["systemctl", "--user", action, "--no-ask-password", "--", unit]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return False, "systemctl --user недоступен (dev-среда?)"
    except subprocess.TimeoutExpired:
        return False, "timeout"
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out.strip()[:2000]


def journal_user_text(unit: str, lines: int, timeout: float = 10.0) -> tuple[int, str]:
    """Последние строки журнала user-юнита: journalctl --user (read-only, как панель).

    Возврат (код, текст): код 0 — успех; 1 — сбой (текст содержит stderr).
    """
    if not unit or "/" in unit or "\x00" in unit:
        return 1, "bad unit"
    argv = ["journalctl", "--user", f"--unit={unit}", f"--lines={int(lines)}",
            "--no-pager", "--no-color"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
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
                            stderr=subprocess.PIPE, text=True)


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
