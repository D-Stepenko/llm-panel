"""Матрица GPU (этап 2): плановые привязки vs фактическая занятость + конфликты.

READ-ONLY страница: никаких изменений конфигурации. Снippets привязок
(CUDA_VISIBLE_DEVICES / gpu[]) — копируемые подсказки для правки конфигов
владельцем; редактор конфигураций — отдельная страница (/config/files).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .. import __version__
from ..matrix import build_matrix

router = APIRouter()


@router.get("/matrix", response_class=HTMLResponse)
async def matrix_page(request: Request):
    state = request.app.state.panel
    snap = state.collector.snapshot
    defs = tuple(state.registry.services) + tuple(state.extra_defs.values())
    data = build_matrix(defs, snap.services if snap else None,
                        snap.gpus if snap else None)
    warnings = list(snap.warnings) if snap else ["Снапшот ещё не собран"]
    return state.render(
        "matrix.html",
        request=request,
        username=request.state.session["username"],
        version=__version__,
        data=data,
        warnings=warnings,
    )
