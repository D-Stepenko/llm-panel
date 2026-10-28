"""Config Panel: панельные настройки (язык, единицы, интервалы).

Хранятся в settings (БД), действуют на всю панель; язык интерфейса — панельный,
не персональный. POST проверяет CSRF (ТЗ §6.1), каждое сохранение — в аудит.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from ..i18n import translate

router = APIRouter()

_LANGS = ("en", "ru")
_TEMP_UNITS = ("C", "F")
_INTERVALS = ("2", "3", "5", "10")        # секунд между снимками (SSE)
_AUDIT_LIMITS = ("30", "50", "100", "200")  # записей аудита на странице


@router.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    state = request.app.state.panel
    interval = state.collector.interval
    interval_value = str(int(interval)) if float(interval).is_integer() else str(interval)
    return state.render(
        "config.html",
        request=request,
        saved=request.query_params.get("saved") == "1",
        lang_value=state.i18n_lang,
        temp_value=state.temp_unit,
        interval_value=interval_value,
        audit_value=str(state.audit_limit),
    )


@router.post("/config")
async def config_save(request: Request):
    state = request.app.state.panel
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001
        form = {}
    if form.get("csrf") != request.state.csrf:
        return PlainTextResponse(translate(state.i18n_lang, "bad_csrf"), status_code=403)

    lang = str(form.get("lang", ""))
    temp_unit = str(form.get("temp_unit", "")).strip().upper()[:1]
    interval = str(form.get("refresh_interval", "")).strip()
    audit_limit = str(form.get("audit_limit", "")).strip()
    if (lang not in _LANGS or temp_unit not in _TEMP_UNITS
            or interval not in _INTERVALS or audit_limit not in _AUDIT_LIMITS):
        # невалидные значения — просто вернуть форму без сохранения
        return RedirectResponse("/config", status_code=303)

    state.i18n_lang = lang
    state.db.set_setting("lang", lang)
    state.temp_unit = temp_unit
    state.db.set_setting("temp_unit", temp_unit)
    state.collector.interval = float(interval)
    state.db.set_setting("refresh_interval", interval)
    state.audit_limit = int(audit_limit)
    state.db.set_setting("audit_limit", audit_limit)

    ip = request.client.host if request.client else ""
    state.db.audit(ip, request.state.session["username"], "config", "panel", "ok",
                   f"lang={lang} temp={temp_unit} interval={interval} audit_limit={audit_limit}")
    return RedirectResponse("/config?saved=1", status_code=303)