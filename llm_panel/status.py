"""Расчёт отображаемого состояния сервиса (ТЗ §4.2).

Разделяем состояние systemd и готовность API.missing_gpu — запуск запрещён.
readiness_timeout не убивает процесс (ТЗ §9).
"""

from __future__ import annotations

import dataclasses
from enum import Enum


class DisplayState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    UNHEALTHY = "unhealthy"
    READINESS_TIMEOUT = "readiness_timeout"
    FAILED = "failed"
    MISSING_GPU = "missing_gpu"
    UNKNOWN = "unknown"


STATE_RU = {
    "stopped": "остановлен",
    "starting": "запускается",
    "ready": "запущен",
    "unhealthy": "неисправен (был готов)",
    "readiness_timeout": "не стал готов (тайм-аут)",
    "failed": "сбой",
    "missing_gpu": "отсутствует GPU",
    "unknown": "неизвестно",
}

# Результаты завершения systemd, означающие сбой (кроме успешного выхода)
_BAD_RESULTS = {"exit-code", "signal", "core-dump", "watchdog", "start-limit-hit", "oom-kill"}


@dataclasses.dataclass
class Display:
    state: DisplayState
    note: str = ""
    system_active: str = ""
    system_sub: str = ""
    system_result: str = ""
    health_ok: bool | None = None
    health_detail: str = ""
    uptime_start: str = ""


def _gpus_missing(saved: tuple, present) -> list:
    """present: список UUID из NVML или None (нет данных — не выдумывать, ТЗ §5.1)."""
    if present is None or not saved:
        return []
    return [g for g in saved if g not in set(present)]


def compute_display(
    unit,                          # systemd_units.UnitInfo | None
    health,                        # health.HealthResult | None
    saved_gpus: tuple,
    present_gpu_uuids,             # list | None
    *,
    uptime_s: float | None,
    startup_timeout: int,
    last_ready_ts: float | None,
    now_ts: float,
) -> Display:
    if _gpus_missing(saved_gpus, present_gpu_uuids):
        return Display(
            state=DisplayState.MISSING_GPU,
            note="Часть сохранённых GPU отсутствует в текущем списке устройств; запуск запрещён до исправления конфигурации",
            uptime_start=(unit.since if unit else "") or "",
        )

    if unit is None or not unit.active or unit.active == "unknown":
        return Display(
            state=DisplayState.UNKNOWN,
            note="Состояние systemd недоступно (соответствующий блок показывает «нет данных»)",
        )

    sys_note = f"ActiveState={unit.active} SubState={unit.sub} Result={unit.result or '-'}"

    failed = unit.active == "failed" or (
        unit.active == "inactive" and unit.result != "" and unit.result != "success" and (
            unit.result in _BAD_RESULTS
        )
    )
    if failed:
        return Display(
            state=DisplayState.FAILED,
            note=f"systemd: сбой ({unit.result or 'подробности в логах'})",
            system_active=unit.active, system_sub=unit.sub, system_result=unit.result,
            uptime_start=unit.since,
        )

    if unit.active in ("inactive", "deactivating"):
        return Display(
            state=DisplayState.STOPPED,
            system_active=unit.active, system_sub=unit.sub, system_result=unit.result,
            uptime_start="",
        )

    if unit.active in ("activating", "reloading"):
        state = DisplayState.STARTING if (uptime_s or 0) <= startup_timeout else DisplayState.READINESS_TIMEOUT
        return Display(
            state=state,
            note=(
                "запуск в процессе" if state is DisplayState.STARTING
                else "не дождались готовности; процесс не убивается автоматически (ТЗ §9)"
            ),
            system_active=unit.active, system_sub=unit.sub, system_result=unit.result,
            uptime_start=unit.since,
        )

    if unit.active == "active":
        if health is not None:
            if health.ok:
                return Display(
                    state=DisplayState.READY, note="проверка готовности успешна",
                    system_active=unit.active, system_sub=unit.sub, health_ok=True,
                    health_detail=health.detail, uptime_start=unit.since,
                )
            if last_ready_ts is not None:
                return Display(
                    state=DisplayState.UNHEALTHY,
                    note=f"ранее был готов ({last_ready_ts:.0f}); сейчас проверка не проходит: {health.detail}",
                    system_active=unit.active, system_sub=unit.sub, health_ok=False,
                    health_detail=health.detail, uptime_start=unit.since,
                )
            # никогда не был готов (в рамках текущей жизни панели)
            if uptime_s is not None and uptime_s > startup_timeout:
                return Display(
                    state=DisplayState.READINESS_TIMEOUT,
                    note=f"ActiveState={unit.active}; процесс не убивается автоматически",
                    system_active=unit.active, system_sub=unit.sub, health_ok=False,
                    health_detail=health.detail, uptime_start=unit.since,
                )
            return Display(
                state=DisplayState.STARTING, note=f"проверка готовности: {health.detail}",
                system_active=unit.active, system_sub=unit.sub, health_ok=False,
                health_detail=health.detail, uptime_start=unit.since,
            )

        # health-проверка не настроена: не подменять подтверждением (ТЗ §4.2)
        return Display(
            state=DisplayState.UNKNOWN,
            note="Юнит активен; health-проверка не настроена — готовность API не проверяется",
            system_active=unit.active, system_sub=unit.sub, uptime_start=unit.since,
        )

    return Display(
        state=DisplayState.UNKNOWN,
        note=f"Неожиданное состояние systemd: {unit.active}",
        system_active=unit.active, system_sub=unit.sub, uptime_start=unit.since,
    )
