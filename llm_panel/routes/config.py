"""Config Panel: панельные настройки (язык, единицы, интервалы, диски).

Хранятся в settings (БД), действуют на всю панель; язык интерфейса — панельный,
не персональный. POST проверяет CSRF (ТЗ §6.1), каждое сохранение — в аудит.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from ..i18n import translate
from ..registry import UNIT_RE, make_display_def
from ..systemd_units import list_loaded_services

router = APIRouter()

_LANGS = ("en", "ru")
_TEMP_UNITS = ("C", "F")
_THEMES = ("dark", "light")
_INTERVALS = ("2", "3", "5", "10")        # секунд между снимками (SSE)
_AUDIT_LIMITS = ("30", "50", "100", "200")  # записей аудита на странице
_ROOTS_MAX = 8                            # максимум путей дисковой сводки
_ROOT_MAX_LEN = 200                       # длина одного пути
_EXTRAS_MAX = 64                          # максимум найденных юнитов на Статусе


def _parse_model_roots(raw: str) -> list[str] | None:
    """Список путей из textarea (по одному в строке). None — невалидный ввод.

    Правила: абсолютные пути (начинаются с /), без дубликатов, не более
    _ROOTS_MAX строк. Несуществующие пути допустимы — сводка покажет
    «нет данных» (метрики не выдумывают значений).
    """
    paths: list[str] = []
    for line in str(raw).splitlines():
        p = line.strip()
        if not p:
            continue
        if not p.startswith("/") or len(p) > _ROOT_MAX_LEN or p in paths:
            return None
        paths.append(p)
    if len(paths) > _ROOTS_MAX:
        return None
    return paths


@router.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    state = request.app.state.panel
    interval = state.collector.interval
    interval_value = str(int(interval)) if float(interval).is_integer() else str(interval)
    # Секция «Отображение сервисов»: настроенные сервисы (реестр + карточка
    # панели) + фрагмент со списком найденных юнитов (сначала — без поиска).
    svc_display = [
        {"id": s.id, "name": s.name, "unit": s.unit,
         "visible": s.id not in state.hidden_ids}
        for s in state.registry
    ]
    extra_list_html = _render_scan_fragment(state, request, "initial",
                                            list(state.extra_defs.keys()))
    return state.render(
        "config.html",
        request=request,
        saved=request.query_params.get("saved") == "1",
        roots_error=request.query_params.get("err") == "roots",
        lang_value=state.i18n_lang,
        temp_value=state.temp_unit,
        theme_value=state.theme,
        interval_value=interval_value,
        audit_value=str(state.audit_limit),
        roots_value="\n".join(state.model_roots),
        svc_display=svc_display,
        extra_list_html=extra_list_html,
    )


def _render_scan_fragment(state, request: Request, mode: str,
                          saved_units: list[str], units_found: list | None = None) -> str:
    """Фрагмент списка юнитов-«надстроек» для Config Panel.

    mode: initial (поиск ещё не запускался) | unavailable (systemd недоступен)
    | result (units_found — найденные юниты без настроенных и сохранённых).
    """
    return state.render(
        "_scan.html",
        request=request,
        mode=mode,
        saved_units=saved_units,
        units_found=units_found or [],
    ).body.decode("utf-8")


@router.get("/config/services-scan", response_class=HTMLResponse)
async def services_scan(request: Request):
    """«Поиск сервисов»: read-only systemctl list-units (без sudo, ТЗ §5.3)."""
    state = request.app.state.panel
    configured = {s.unit for s in state.registry}
    found = list_loaded_services()
    saved_units = list(state.extra_defs.keys())
    if found is None:
        return HTMLResponse(_render_scan_fragment(state, request, "unavailable", saved_units))
    units_found = [f for f in found if f["unit"] not in configured
                   and f["unit"] not in saved_units]
    return HTMLResponse(_render_scan_fragment(state, request, "result", saved_units, units_found))


@router.post("/config/services-display")
async def services_display_save(request: Request):
    """Сохранение видимости сервисов и списка найденных юнитов.

    Настроенные сервисы: чекбокс «виден» — непомеченные id уходят в hidden.
    Найденные юниты: помеченные показываются на Статусе простыми карточками
    (состояние/uptime/действия по allowlist), сохраняются в settings.
    """
    state = request.app.state.panel
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001
        form = {}
    if form.get("csrf") != request.state.csrf:
        return PlainTextResponse(translate(state.i18n_lang, "bad_csrf"), status_code=403)

    configured_ids = {s.id for s in state.registry}
    visible = {str(v).strip() for v in form.getlist("visible")}
    hidden = sorted(configured_ids - visible)

    extra_units: list[str] = []
    for v in form.getlist("extra_units"):
        unit = str(v).strip()
        if (UNIT_RE.match(unit) and unit not in extra_units
                and not state.registry.by_unit(unit)):
            extra_units.append(unit)
    if len(extra_units) > _EXTRAS_MAX:
        return PlainTextResponse(
            translate(state.i18n_lang, "cfg_too_many_units", max=_EXTRAS_MAX), status_code=400)

    state.hidden_ids = hidden
    state.extra_defs = {u: make_display_def(u) for u in extra_units}
    state.collector.extra_defs = tuple(state.extra_defs.values())
    state.db.set_setting("hidden_ids", json.dumps(hidden))
    state.db.set_setting("extra_units", json.dumps(extra_units))

    ip = request.client.host if request.client else ""
    state.db.audit(ip, request.state.session["username"], "config", "services_display",
                   "ok", f"hidden={','.join(hidden) or '-'} extras={','.join(extra_units) or '-'}")
    return RedirectResponse("/config?saved=1", status_code=303)


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

    # Тема: поле может отсутствовать (старая форма/тесты) — тогда не менять.
    theme_detail = ""
    if "theme" in form:
        theme = str(form.get("theme", "")).strip().lower()
        if theme not in _THEMES:
            return RedirectResponse("/config", status_code=303)
        state.theme = theme
        state.db.set_setting("theme", theme)
        theme_detail = f" theme={theme}"

    # Диски сводки: поле может отсутствовать (старая форма/тесты) — тогда не менять.
    roots_detail = ""
    if "model_roots" in form:
        paths = _parse_model_roots(str(form.get("model_roots", "")))
        if paths is None:
            return RedirectResponse("/config?err=roots", status_code=303)
        state.model_roots = paths
        state.collector.model_roots = list(paths)   # применяется на ходу, без рестарта
        state.db.set_setting("model_roots", json.dumps(paths))
        roots_detail = f" roots={len(paths)}"

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
                   f"lang={lang} temp={temp_unit} interval={interval} "
                   f"audit_limit={audit_limit}{theme_detail}{roots_detail}")
    return RedirectResponse("/config?saved=1", status_code=303)