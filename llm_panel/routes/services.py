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

router = APIRouter(prefix="/services")

_ACTIONS = ("start", "stop", "restart", "enable", "disable")


async def _render_action_result(request: Request, svc_id: str, ok: bool, message: str, status=""):
    state = request.app.state.panel
    svc = state.registry.by_id(svc_id)
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
    svc = state.registry.by_id(svc_id)
    if svc is None:
        return PlainTextResponse("Сервис не найден", status_code=404)
    if action not in _ACTIONS:
        return PlainTextResponse("Недопустимое действие", status_code=400)

    # CSRF: токен формы должен совпасть с токеном сессии (ТЗ §6.1)
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001
        form = {}
    if form.get("csrf") != request.state.csrf:
        return PlainTextResponse("Недопустимый CSRF-токен", status_code=403)

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
    actual = f"{info.active}/{info.sub}" if info else "состояние недоступно"
    body = await _render_action_result(
        request, svc_id, True,
        f"systemd: действие «{action}» выполнено (код 0). Фактическое состояние юнита: {actual}. " \
        f"Отображение статуса обновится в течение пары секунд (SSE).",
        status=actual,
    )
    return HTMLResponse(body, status_code=200)


@router.get("/{svc_id}/settings")
async def settings(request: Request, svc_id: str):
    """Этап 1: только просмотр описания (manage_only). Редактор — этап 2 по ТЗ §11."""
    state = request.app.state.panel
    svc = state.registry.by_id(svc_id)
    if svc is None:
        return PlainTextResponse("Сервис не найден", status_code=404)
    return state.render(
        "service_settings.html",
        request=request,
        svc=svc,
        gpu_short=[g[-12:] if g else "" for g in svc.gpus],
    )
