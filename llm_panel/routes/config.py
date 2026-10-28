"""Config Panel: панельные настройки (язык, единицы, интервалы, диски).

Хранятся в settings (БД), действуют на всю панель; язык интерфейса — панельный,
не персональный. POST проверяет CSRF (ТЗ §6.1), каждое сохранение — в аудит.
"""

from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from ..i18n import translate
from ..registry import UNIT_RE, make_display_def
from ..systemd_units import list_loaded_services, show_unit

router = APIRouter()

_LANGS = ("en", "ru")
_TEMP_UNITS = ("C", "F")
_THEMES = ("dark", "light")
_INTERVALS = ("2", "3", "5", "10")        # секунд между снимками (SSE)
_AUDIT_LIMITS = ("30", "50", "100", "200")  # записей аудита на странице
_ROOTS_MAX = 8                            # максимум путей дисковой сводки
_ROOT_MAX_LEN = 200                       # длина одного пути
_EXTRAS_MAX = 64                          # максимум найденных юнитов на Статусе
_ADD_ERRORS = ("cfg_scan_bad_name", "cfg_scan_not_found",
               "cfg_scan_exists", "cfg_too_many_units")


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
         "scope": getattr(s, "scope", "system"),
         "visible": s.id not in state.hidden_ids}
        for s in state.registry
    ]
    extra_list_html = _render_scan_fragment(state, request, "initial",
                                            [{"unit": u, "scope": getattr(d, "scope", "system")}
                                             for u, d in state.extra_defs.items()])
    # Ошибка добавления юнита по имени (?add_err=<i18n-key>&unit=<имя>):
    # ключ проверяется по белому списку, чтобы из query не подставить произвольный.
    add_err = request.query_params.get("add_err") or ""
    add_err = add_err if add_err in _ADD_ERRORS else ""
    # Блок «права панели» для только что добавленного юнита (?added=&scope=):
    # имя проверяется UNIT_RE, scope — по белому списку, id — slug из имени.
    added = request.query_params.get("added") or ""
    if added and not UNIT_RE.match(added):
        added = ""
    added_scope = request.query_params.get("scope") or "system"
    added_scope = added_scope if added_scope in ("user", "system") else "system"
    added_id = re.sub(r"[^a-z0-9_-]+", "-", added.lower()).strip("-") or "new-unit"
    return state.render(
        "config.html",
        request=request,
        saved=request.query_params.get("saved") == "1",
        roots_error=request.query_params.get("err") == "roots",
        add_err=add_err,
        add_unit=request.query_params.get("unit", ""),
        added=added,
        added_scope=added_scope,
        added_id=added_id,
        registry_path=str(state.cfg.services_file),
        lang_value=state.i18n_lang,
        temp_value=state.temp_unit,
        theme_value=state.theme,
        interval_value=interval_value,
        audit_value=str(state.audit_limit),
        roots_value="\n".join(state.model_roots),
        svc_display=svc_display,
        extra_list_html=extra_list_html,
    )


def _saved_units(state) -> list[dict]:
    """Сохранённые юниты-«надстройки» для фрагмента: unit + scope."""
    return [{"unit": u, "scope": getattr(d, "scope", "system")}
            for u, d in state.extra_defs.items()]


def _render_scan_fragment(state, request: Request, mode: str,
                          saved: list[dict], units_found: list | None = None) -> str:
    """Фрагмент списка юнитов-«надстроек» для Config Panel.

    mode: initial (поиск ещё не запускался) | unavailable (systemd недоступен)
    | result (units_found — найденные юниты без настроенных и сохранённых).
    saved — список {"unit", "scope"}; user-юниты кодируются значением "user:unit".
    """
    return state.render(
        "_scan.html",
        request=request,
        mode=mode,
        saved=saved,
        units_found=units_found or [],
    ).body.decode("utf-8")


@router.get("/config/services-scan", response_class=HTMLResponse)
async def services_scan(request: Request):
    """«Поиск сервисов»: read-only systemctl list-units (без sudo, ТЗ §5.3).

    Выборка объединяет системный и user-менеджер (scope="user" у user-юнитов,
    в форме они кодируются префиксом "user:").
    """
    state = request.app.state.panel
    configured = {s.unit for s in state.registry}
    found = list_loaded_services()
    saved = _saved_units(state)
    if found is None:
        return HTMLResponse(_render_scan_fragment(state, request, "unavailable", saved))
    saved_names = {s["unit"] for s in saved}
    units_found = [f for f in found if f["unit"] not in configured
                   and f["unit"] not in saved_names]
    return HTMLResponse(_render_scan_fragment(state, request, "result", saved, units_found))


def _stored_units(state) -> list[tuple[str, str]]:
    """Сохранённые юниты-«надстройки» из settings: список (scope, unit).

    Формат в БД — JSON-список строк: "unit" (system) или "user:unit".
    Дубликаты и невалидные имена отбрасываются (как в app._load_settings).
    """
    raw = state.db.get_setting("extra_units") or ""
    try:
        data = json.loads(raw)
    except ValueError:
        data = []
    out: list[tuple[str, str]] = []
    for x in (data if isinstance(data, list) else []):
        s = str(x).strip()
        if not s:
            continue
        if s.startswith("user:"):
            scope, unit = "user", s[len("user:"):].strip()
        else:
            scope, unit = "system", s
        if UNIT_RE.match(unit) and (scope, unit) not in out:
            out.append((scope, unit))
    return out


@router.post("/config/services-add")
async def services_add(request: Request):
    """Добавление юнита по имени — БЕЗ правки services.yaml вручную.

    Дополняет «Поиск сервисов»: скан показывает загруженные юниты (list-units),
    а юнит, который ещё ни разу не стартовал (или остановлен и выгружен
    systemd'ом), в этот список не попадает. Здесь имя проверяется напрямую
    `systemctl show` (read-only): юнит должен существовать хотя бы в одном из
    менеджеров. Сначала проверяется user-менеджер пользователя панели (его
    собственные юниты), затем системный. Обнаруженный scope сохраняется в
    settings («extra_units») вместе с уже накопленными юнитами; карточка
    появляется на Статусе с действиями (system — по allowlist, user — напрямую).
    """
    state = request.app.state.panel
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001
        form = {}
    if form.get("csrf") != request.state.csrf:
        return PlainTextResponse(translate(state.i18n_lang, "bad_csrf"), status_code=403)
    unit = str(form.get("unit", "")).strip()

    def reject(key: str) -> RedirectResponse:
        """Ошибка добавления: баннер на /config (строка не теряется при вводе)."""
        return RedirectResponse(
            f"/config?add_err={key}&unit={quote(unit, safe='')}", status_code=303)

    if not UNIT_RE.match(unit):
        return reject("cfg_scan_bad_name")
    if state.registry.by_unit(unit) or any(u == unit for _, u in _stored_units(state)):
        return reject("cfg_scan_exists")

    scope = None
    for candidate in ("user", "system"):
        info = await asyncio.to_thread(show_unit, unit, 5.0, candidate)
        if info is not None:
            scope = candidate
            break
    if scope is None:
        return reject("cfg_scan_not_found")

    stored = _stored_units(state)
    stored.append((scope, unit))
    if len(stored) > _EXTRAS_MAX:
        return reject("cfg_too_many_units")
    serialized = json.dumps([f"{s}:{u}" if s == "user" else u for s, u in stored])
    extra_defs: dict = {u: make_display_def(u, scope=s) for s, u in stored}
    state.extra_defs = extra_defs
    state.collector.extra_defs = tuple(extra_defs.values())
    state.db.set_setting("extra_units", serialized)

    ip = request.client.host if request.client else ""
    state.db.audit(ip, request.state.session["username"], "config", "services_add",
                   "ok", f"{scope}:{unit}")
    return RedirectResponse(
        f"/config?saved=1&added={quote(unit, safe='')}&scope={scope}", status_code=303)


@router.post("/config/services-display")
async def services_display_save(request: Request):
    """Сохранение видимости сервисов и списка найденных юнитов.

    Настроенные сервисы: чекбокс «виден» — непомеченные id уходят в hidden.
    Найденные юниты: помеченные показываются на Статусе простыми карточками
    (состояние/uptime; system-юниты — действия по allowlist, user-юниты —
    напрямую systemctl --user), сохраняются в settings с префиксом scope
    ("user:<unit>" для user-менеджера).
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

    extra_units: list[str] = []       # как хранится в settings (с префиксом scope)
    extra_defs: dict = {}
    for v in form.getlist("extra_units"):
        raw = str(v).strip()
        if raw.startswith("user:"):
            scope, unit = "user", raw[len("user:"):].strip()
        else:
            scope, unit = "system", raw
        if (UNIT_RE.match(unit) and unit not in extra_defs
                and not state.registry.by_unit(unit)):
            extra_units.append(f"{scope}:{unit}" if scope == "user" else unit)
            extra_defs[unit] = make_display_def(unit, scope=scope)
    if len(extra_units) > _EXTRAS_MAX:
        return PlainTextResponse(
            translate(state.i18n_lang, "cfg_too_many_units", max=_EXTRAS_MAX), status_code=400)

    state.hidden_ids = hidden
    state.extra_defs = extra_defs
    state.collector.extra_defs = tuple(extra_defs.values())
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