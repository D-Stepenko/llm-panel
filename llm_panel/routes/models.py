"""Каталог моделей (этап 3, v0.3.0): /models — READ-ONLY скан model_roots.

Скан выполняется в отдельном потоке (каталоги бывают большие), результат
кэшируется на панели с TTL; кнопка «Обновить» — принудительный перескан.
"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from .. import __version__
from ..i18n import translate
from ..model_catalog import MAX_DEPTH_DEFAULT, MAX_ENTRIES_DEFAULT, scan_models

router = APIRouter(prefix="/models")

SCAN_TTL_S = 60.0          # повторный скан не чаще (кэш на состоянии панели)
SCAN_TIMEOUT_S = 60.0      # страховка: большие корни не должны вешать поток


async def _scan(state, force: bool = False) -> dict:
    """Скан model_roots в отдельном потоке с кэшем (TTL) на состоянии панели."""
    if not force:
        cache = getattr(state, "models_scan", None)
        if cache is not None and time.time() - cache["ts"] < SCAN_TTL_S:
            return cache
    try:
        data = await asyncio.wait_for(
            asyncio.to_thread(
                scan_models, state.model_roots,
                MAX_DEPTH_DEFAULT, MAX_ENTRIES_DEFAULT),
            timeout=SCAN_TIMEOUT_S)
    except asyncio.TimeoutError:
        data = {"models": [], "roots": [], "errors": ["Скан превысил тайм-аут"],
                "ts": time.time()}
    state.models_scan = data
    return data


@router.get("", response_class=HTMLResponse)
async def models_page(request: Request):
    state = request.app.state.panel
    data = await _scan(state)
    return state.render(
        "models.html", request=request, version=__version__,
        data=data, roots=state.model_roots,
    )


@router.post("/refresh")
async def models_refresh(request: Request):
    state = request.app.state.panel
    form = {}
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001
        pass
    if form.get("csrf") != request.state.csrf:
        return PlainTextResponse(translate(state.i18n_lang, "bad_csrf"), status_code=403)
    data = await _scan(state, force=True)
    return state.render(
        "models.html", request=request, version=__version__,
        data=data, roots=state.model_roots,
    )
