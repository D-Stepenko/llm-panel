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


def show_unit(unit: str, timeout: float = 5.0) -> UnitInfo | None:
    """Состояние одного юнита; None при отсутствии юнита либо недоступности systemd."""
    if not unit or "/" in unit or "\x00" in unit:
        return None
    argv = ["systemctl", "show", "--no-pager"]
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


def list_loaded_services(timeout: float = 10.0) -> list[dict] | None:
    """Все загруженные systemd-сервисы сервера (read-only, без sudo).

    ТЗ §5.3: чтение состояния выполняется напрямую без helper. Перечисление
    юнитов — тоже read-only операция (list-units --all). Юниты с LOAD=not-found
    не показываются. Возврат None — systemd недоступен (dev на Windows и т.п.).
    """
    import re

    argv = ["systemctl", "list-units", "--type=service", "--all", "--no-pager",
            "--no-legend", "--plain"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return None
    out: list[dict] = []
    for line in proc.stdout.splitlines():
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 5:
            continue
        unit, load, active, sub, desc = parts[0], parts[1], parts[2], parts[3], parts[4]
        if not unit.endswith(".service") or load == "not-found":
            continue
        out.append({"unit": unit, "active": active, "sub": sub, "desc": desc})
    return out


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
