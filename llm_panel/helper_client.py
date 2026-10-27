"""Вызов привилегированного helper (ТЗ §6.3).

Backend запускает sudo -n <helper> с argv-списком, БЕЗ shell=True.
Реальные проверки action/unit/allowlist выполняет сам helper
(root-owned файл /usr/local/sbin/llm-panel-systemd). Здесь — только
структурированный вызов и понятная обработка кодов возврата.

Коды возврата helper:
  0 — успех; 2 — ошибка аргументов; 3 — отказ (allowlist/валидация); 4 — сбой systemd.
"""

from __future__ import annotations

import subprocess
import dataclasses
import pathlib


class HelperError(Exception):
    def __init__(self, kind: str, message: str, stdout: str = "", stderr: str = "", code: int = -1):
        super().__init__(message)
        self.kind = kind          # denied | usage | systemd_error | launch_error
        self.message = message
        self.stdout = stdout
        self.stderr = stderr
        self.code = code


HELPER_EXIT_DENY = 3
HELPER_EXIT_USAGE = 2
HELPER_EXIT_SYSTEMD = 4


@dataclasses.dataclass(frozen=True)
class HelperResult:
    ok: bool
    stdout: str
    stderr: str
    code: int


def _map_exit(code: int, stderr: str) -> str:
    if code == HELPER_EXIT_DENY:
        return "отказано: юнит или операция вне allowlist (проверено самим helper)"
    if code == HELPER_EXIT_USAGE:
        return "ошибка аргументов helper"
    if code == HELPER_EXIT_SYSTEMD:
        return f"systemd вернул ошибку: {stderr.strip()[:300] or '--'}"
    return f"helper завершился с кодом {code}"


def call_helper(helper_path: str, action: str, unit: str, extra: tuple = (), timeout: float = 60.0) -> HelperResult:
    p = pathlib.Path(helper_path)
    if not p.is_file():
        raise HelperError(
            kind="launch_error",
            message=f"Helper не установлен: {helper_path} (см. README «Привилегированный helper»)",
        )
    if not unit or "/" in unit or "\x00" in unit or "\n" in unit:
        raise HelperError(kind="usage", message="Недопустимое имя юнита")

    argv = ["sudo", "-n", helper_path, action, unit, *extra]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise HelperError(kind="launch_error", message=f"sudo недоступен: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise HelperError(kind="systemd_error", message=f"Тайм-аут операции helper ({timeout}с)") from e

    ok = proc.returncode == 0
    result = HelperResult(
        ok=ok,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        code=proc.returncode,
    )
    if not ok:
        raise HelperError(
            kind={2: "usage", 3: "denied", 4: "systemd_error"}.get(proc.returncode, "systemd_error"),
            message=_map_exit(proc.returncode, result.stderr),
            stdout=result.stdout,
            stderr=result.stderr,
            code=proc.returncode,
        )
    return result


def spawn_helper_follow(helper_path: str, unit: str, lines: int) -> subprocess.Popen:
    """Потоковое чтение журнала (journal-follow) для SSE. Ответственность вызывающего — kill."""
    argv = ["sudo", "-n", helper_path, "journal-follow", unit, str(int(lines))]
    return subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,  # отдельная группа процессов для гарантированной остановки
    )
