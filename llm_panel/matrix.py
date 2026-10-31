"""Матрица GPU (этап 2): плановая привязка vs фактическая занятость, конфликты.

Только READ-ONLY расчёт для страницы /matrix. Источники:
  * services.yaml + extra_defs (БД) — плановые привязки (UUID) и конвенция
    адресации cuda_addressing (uuid|index) — ТЗ §4.2;
  * снапшот коллектора — фактические владельцы карт (NVML-процессы,
    атрибуция v0.1.12), текущие индексы карт, слушающие порты.

Конвенции станции (docs/STATION-LLM.md):
  * vLLM адресует карты ПО ИНДЕКСАМ → сниппет CUDA_VISIBLE_DEVICES=«0,1»
    (индексы разрешаются из текущего порядка NVML на момент запуска);
  * llama.cpp/TTS — по ПОЛНЫМ UUID;
  * Maya — ключ "gpu": [индексы] в maya-maya-m.json (индексная адресация).
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass
class MatrixRow:
    id: str
    name: str
    unit: str
    scope: str
    state: str
    running: bool
    planned: tuple            # UUID плановой привязки
    planned_idx: tuple        # разрешённые текущие индексы (по порядку NVML)
    addressing: str           # uuid | index
    snippet: str              # готовый фрагмент привязки (CVD / gpu[])
    actual: tuple             # UUID фактически занятых карт
    ram_exclusive: bool
    port: int | None
    listen: tuple
    cells: dict = dataclasses.field(default_factory=dict)  # uuid → plan|actual|both


@dataclasses.dataclass
class Conflict:
    kind: str        # gpu_planned | gpu_actual | ram | port
    level: str       # warn | err
    detail: str


def _snippet(svc, planned_idx: tuple[int, ...]) -> str:
    """Готовая привязка карт по конвенции владельца (для правки конфигов)."""
    if svc.cuda_addressing == "index" and planned_idx:
        idx = ",".join(str(i) for i in sorted(planned_idx))
        if svc.unit.startswith("maya-"):
            return f'maya-maya-m.json → "gpu": [{idx}]'
        return f'CUDA_VISIBLE_DEVICES="{idx}"'
    if svc.cuda_addressing == "uuid" and svc.gpus:
        return 'CUDA_VISIBLE_DEVICES="' + ",".join(svc.gpus) + '"'
    return ""


def build_matrix(defs, svc_rows, gpu_rows) -> dict:
    """def'ы сервисов + строки снапшота → данные матрицы (без побочных эффектов)."""
    gpu_rows = gpu_rows or []
    svc_by_id = {r["id"]: r for r in (svc_rows or [])}

    uuid_index = {g["uuid"]: g.get("index") for g in gpu_rows}
    gpu_names = {g["uuid"]: (g.get("name") or "") for g in gpu_rows}

    rows: list[MatrixRow] = []
    gpu_planned: dict[str, list[str]] = {}
    gpu_actual: dict[str, list[str]] = {}
    ram_svc: list[str] = []

    for svc in defs:
        row = svc_by_id.get(svc.id) or {}
        state = row.get("state", "unknown")
        planned = tuple(svc.gpus)
        planned_idx = tuple(uuid_index[u] for u in planned if u in uuid_index
                            and uuid_index[u] is not None)
        running = state in ("ready", "starting", "unhealthy", "readiness_timeout")
        actual = tuple(a["uuid"] for a in (row.get("gpu_actual") or []))
        rows.append(MatrixRow(
            id=svc.id, name=svc.name, unit=svc.unit,
            scope=getattr(svc, "scope", "system"),
            state=state, running=running,
            planned=planned, planned_idx=planned_idx,
            addressing=svc.cuda_addressing,
            snippet=_snippet(svc, planned_idx),
            actual=actual,
            ram_exclusive=bool(svc.ram_exclusive),
            port=svc.port,
            listen=tuple(row.get("listen") or []),
        ))
        # метки для ячеек матрицы: план / факт / план+факт
        cells: dict[str, str] = {}
        for u in planned:
            cells[u] = "plan"
        for u in actual:
            cells[u] = "both" if u in cells else "actual"
        rows[-1].cells = cells
        for u in planned:
            gpu_planned.setdefault(u, []).append(svc.id)
        for u in actual:
            gpu_actual.setdefault(u, []).append(svc.id)
        if svc.ram_exclusive and (planned or running):
            ram_svc.append(svc.id)

    conflicts: list[Conflict] = []
    # 1) одна карта запланирована двум сервисам — НЕ конфликт: несколько
    #    плановых привязок на карту — штатная схема станции (очередность
    #    запусков), фактические привязки видны в таблице; предупреждение
    #    о дублировании убрано по решению владельца (v0.3.8).
    # 2) одна карта фактически занята процессами нескольких сервисов
    for u, ids in gpu_actual.items():
        uniq = list(dict.fromkeys(ids))
        if len(uniq) > 1:
            conflicts.append(Conflict(
                kind="gpu_actual", level="err",
                detail=f"{_gpu_label(u, gpu_names)} фактически занята: {', '.join(uniq)}",
            ))
    # 3) ram_exclusive: одновременно работают ≥2 таких сервиса
    on_ram = [i for i in ram_svc if next(r for r in rows if r.id == i).running]
    if len(on_ram) > 1:
        conflicts.append(Conflict(
            kind="ram", level="err",
            detail="Одновременно работают сервисы с ram_exclusive: " + ", ".join(on_ram),
        ))
    # 4) один и тот же configured-port у работающих сервисов
    port_owner: dict[int, list[str]] = {}
    for r in rows:
        if r.port and r.running:
            port_owner.setdefault(r.port, []).append(r.id)
    for p, ids in port_owner.items():
        if len(ids) > 1:
            conflicts.append(Conflict(
                kind="port", level="warn",
                detail=f"Порт {p} запланирован у работающих сервисов: {', '.join(ids)}",
            ))

    # строки матрицы: только сервисы с привязкой карт (плановой или фактической);
    # остальные — отдельная приглушённая секция (не шумят в основной таблице)
    rows_gpu = [r for r in rows if r.planned or r.actual]
    rows_other = [r for r in rows if not (r.planned or r.actual)]

    return {
        "gpus": [
            {"uuid": g["uuid"], "index": g.get("index"),
             "name": gpu_names.get(g["uuid"], ""), "mem_total_mb": g.get("mem_total_mb")}
            for g in gpu_rows
        ],
        "rows": rows,
        "rows_gpu": rows_gpu,
        "rows_other": rows_other,
        "conflicts": conflicts,
        "gpu_planned": gpu_planned,
        "gpu_actual": gpu_actual,
    }


def _gpu_label(uuid: str, names: dict) -> str:
    name = names.get(uuid, "")
    return f"{name} ({uuid[-12:]})" if name else uuid
