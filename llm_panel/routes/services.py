"""Управляющие действия: start/stop/restart/enable/disable — только через helper (ТЗ §5.3).

GET-запросы не меняют состояние. POST-запросы проверяют CSRF (ТЗ §6.1).
Результат действия — фактический ответ systemd (через helper), аудит в БД.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from ..systemd_units import show_unit
from ..helper_client import HelperError, call_helper
from ..i18n import translate

router = APIRouter(prefix="/services")

_ACTIONS = ("start", "stop", "restart", "enable", "disable")


def _find_def(state, svc_id: str):
    """Описание сервиса: реестр (services.yaml + карточка панели) или найденный юнит."""
    svc = state.registry.by_id(svc_id)
    if svc is not None:
        return svc
    for d in state.extra_defs.values():
        if d.id == svc_id:
            return d
    return None


async def _render_action_result(request: Request, svc_id: str, ok: bool, message: str, status=""):
    state = request.app.state.panel
    svc = _find_def(state, svc_id)
    return state.render(
        "_action_result.html",
        request=request,
        ok=ok,
        svc_id=svc_id,
        svc_name=svc.name if svc else svc_id,
        message=message,
        status=status,
    ).body.decode("utf-8")


@router.post("/{svc_id}/{action}")
async def run_action(request: Request, svc_id: str, action: str):
    state = request.app.state.panel
    svc = _find_def(state, svc_id)
    if svc is None:
        return PlainTextResponse(translate(state.i18n_lang, "svc_not_found"), status_code=404)
    if action not in _ACTIONS:
        return PlainTextResponse(translate(state.i18n_lang, "bad_action"), status_code=400)

    # CSRF: токен формы должен совпасть с токеном сессии (ТЗ §6.1)
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001
        form = {}
    if form.get("csrf") != request.state.csrf:
        return PlainTextResponse(translate(state.i18n_lang, "bad_csrf"), status_code=403)

    ip = request.client.host if request.client else ""
    user = request.state.session["username"]
    db = state.db

    # Проверки конфликтов — предупреждение (ТЗ §5.2); матрица GPU/RAM добавляется на этапе 2.
    try:
        result = call_helper(state.cfg.helper_path, action, svc.unit, timeout=90)
    except HelperError as e:
        db.audit(ip, user, action, svc.unit, "helper_error", e.message)
        body = await _render_action_result(request, svc_id, False, e.message)
        return HTMLResponse(body, status_code=200)

    db.audit(ip, user, action, svc.unit, "ok" if result.ok else "failed",
             (result.stderr or result.stdout)[:200])

    # фактический результат systemd после действия (не сообщать успех заранее)
    info = await asyncio.to_thread(show_unit, svc.unit)
    lang = state.i18n_lang
    actual = f"{info.active}/{info.sub}" if info else translate(lang, "sys_unavailable")
    body = await _render_action_result(
        request, svc_id, True,
        translate(lang, "action_done", action=action, actual=actual),
        status=actual,
    )
    return HTMLResponse(body, status_code=200)


@router.get("/{svc_id}/settings")
async def settings(request: Request, svc_id: str):
    """Этап 1: только просмотр описания (manage_only). Редактор — этап 2 по ТЗ §11."""
    state = request.app.state.panel
    svc = _find_def(state, svc_id)
    if svc is None:
        return PlainTextResponse(translate(state.i18n_lang, "svc_not_found"), status_code=404)
    # Названия карт (как в nvidia-smi) по UUID — из последнего снапшота NVML
    snap = state.collector.snapshot if state.collector else None
    uuid_name = {}
    if snap is not None and snap.gpus:
        uuid_name = {g["uuid"]: (g.get("name") or "") for g in snap.gpus}
    gpu_info = [{"uuid": g, "name": uuid_name.get(g, "")} for g in svc.gpus]
    return state.render(
        "service_settings.html",
        request=request,
        svc=svc,
        gpu_info=gpu_info,
    )
