"""FastAPI-приложение панели: аутентификация, проверка Host/CSRF, SSE, маршруты (ТЗ §6.1)."""

from __future__ import annotations

import asyncio
import json
import secrets
import time

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import pathlib

from .collector import Collector
from .config import PanelConfig, UNIT_NAME_RE, load_config
from .db import Database
from .i18n import translate
from .metrics import MetricsCollector, fmt_mem_mb, fmt_uptime, gpu_vendor
from .registry import ServiceRegistry, load_services, make_display_def
from .sse import Hub
from .security import (
    SESSION_COOKIE,
    get_session,
    check_lockout,
    hash_token,
)

BASE_DIR = pathlib.Path(__file__).resolve().parent


def _hms(v) -> str:
    """Время снимка в ЧЧ:ММ:СС (None/пусто — пустая строка)."""
    if not v:
        return ""
    import datetime
    return datetime.datetime.fromtimestamp(float(v)).strftime("%H:%M:%S")


class AppState:
    """Единая точка доступа к общим объектам (app.state.panel)."""

    def __init__(self, cfg: PanelConfig, db: Database, registry, keys, metrics, hub):
        self.cfg = cfg
        self.db = db
        self.registry = registry
        self.keys = keys          # тёмные секретные кеи (имена сервисов) — зарезервировано этапом 2
        self.metrics = metrics
        self.hub = hub
        self.collector: Collector | None = None
        # Сохранённый пользователем порядок карточек сервисов (settings.services_order)
        self.services_order: list[str] = []
        # Настройка отображения сервисов (Config Panel, settings):
        # hidden_ids — скрытые на Статусе id настроенных сервисов (реестр + панель);
        # extra_defs — найденные «Поиском сервисов» юниты, показанные как карточки.
        self.hidden_ids: list[str] = []
        self.extra_defs: dict[str, object] = {}   # unit -> ServiceDef
        # Панельные настройки (Config Panel, хранятся в settings)
        self.i18n_lang: str = "en"     # язык интерфейса (по умолчанию английский)
        self.temp_unit: str = "C"      # единица температуры: C/F
        self.audit_limit: int = 30     # записей аудита на странице
        self.theme: str = "dark"       # тема оформления: dark | light
        self.model_roots: list[str] = []   # пути для дисковой сводки (Config Panel)
        self.models_scan: dict | None = None  # кэш скана каталога моделей
        self.hf_jobs: dict = {}        # id → HFJob (загрузки HF, память панели)
        self.hf_tool_path: str = ""    # полный путь к hf (conda env без активации)


def _get_state(request: Request) -> AppState:
    return request.app.state.panel


def create_app(config: PanelConfig) -> FastAPI:
    import llm_panel.routes as routes

    db = Database(config.db_path)
    registry = load_services(
        config.services_file,
        startup_timeout_default=config.startup_timeout_default,
        ports_min=config.ports_min,
        ports_max=config.ports_max,
    )
    # Встроенная карточка самой панели (systemd.panel_unit, пусто — отключить):
    # статус читается напрямую, действия — только при внесении юнита в allowlist.
    if config.panel_unit and not registry.by_unit(config.panel_unit):
        panel_def = make_display_def(
            config.panel_unit, name="llm-panel",
            notes="Карточка самой панели (systemd.panel_unit из config.yaml).",
        )
        registry = ServiceRegistry(registry.services + (panel_def,))
    metrics = MetricsCollector()
    hub = Hub()
    state = AppState(cfg=config, db=db, registry=registry, keys=None, metrics=metrics, hub=hub)
    state.collector = Collector(config, registry, metrics, hub)

    from contextlib import asynccontextmanager

    # Аудит №4 (плюс миграция c устаревшего on_event): запуск/остановка
    # через lifespan. При старте: коллектор, затем чистка БД (executor) и
    # фоновая задача периодической чистки. При остановке: отмена задачи,
    # коллектор, закрытие БД.
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await state.collector.start()
        # Аудит №4: раньше cleanup() не вызывался вовсе (мёртвый код).
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, db.cleanup)
        task = asyncio.create_task(_cleanup_loop(db), name="db-cleanup")
        app.state.cleanup_task = task
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await state.collector.stop()
            metrics.shutdown()
            db.close()

    app = FastAPI(title="llm-panel", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.panel = state

    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    state.templates = templates

    def _load_settings() -> None:
        """Панельные настройки из settings (Config Panel persist)."""
        state.i18n_lang = db.get_setting("lang") if db.get_setting("lang") in ("en", "ru") else "en"
        temp_unit = (db.get_setting("temp_unit") or "C").strip().upper()[:1]
        state.temp_unit = temp_unit if temp_unit in ("C", "F") else "C"
        theme = (db.get_setting("theme") or "dark").strip().lower()
        state.theme = theme if theme in ("dark", "light") else "dark"
        try:
            state.audit_limit = max(1, min(200, int(db.get_setting("audit_limit") or 30)))
        except (TypeError, ValueError):
            state.audit_limit = 30
        try:   # интервал SSE-коллектора можно менять на ходу
            state.collector.interval = float(db.get_setting("refresh_interval") or config.metrics_interval)
        except (TypeError, ValueError):
            state.collector.interval = config.metrics_interval
        # пути дисковой сводки: settings.model_roots, иначе models.roots из config.yaml
        raw_roots = db.get_setting("model_roots")
        roots: list[str] | None = None
        if raw_roots:
            try:
                data = json.loads(raw_roots)
                if isinstance(data, list):
                    roots = [str(x) for x in data if str(x).strip()]
            except ValueError:
                roots = None
        if roots is None:
            roots = list(config.model_roots)
        state.model_roots = roots
        state.collector.model_roots = list(roots)
        # hf вне PATH юнита (conda-окружение): полный путь задаётся в Config Panel
        state.hf_tool_path = (db.get_setting("hf_tool_path") or "").strip()
        # отображение сервисов: скрытые id + найденные («Поиск сервисов») юниты;
        # extra_units хранятся с префиксом scope ("user:<unit>" для user-менеджера)
        state.hidden_ids = _load_str_list("hidden_ids")
        extra_defs: dict = {}
        for raw in _load_str_list("extra_units"):
            if raw.startswith("user:"):
                scope, unit = "user", raw[len("user:"):].strip()
            else:
                scope, unit = "system", raw
            if (UNIT_NAME_RE.match(unit) and unit not in extra_defs
                    and not registry.by_unit(unit)):
                extra_defs[unit] = make_display_def(unit, scope=scope)
        state.extra_defs = extra_defs
        state.collector.extra_defs = tuple(extra_defs.values())

    def _load_str_list(key: str) -> list[str]:
        raw = db.get_setting(key)
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(data, list):
            return []
        seen: list[str] = []
        for x in data:
            s = str(x).strip()
            if s and s not in seen:
                seen.append(s)
        return seen

    _load_settings()

    # фильтры зависят от текущего языка панели (меняется через Config Panel)
    templates.env.filters["mem"] = lambda v: fmt_mem_mb(v, state.i18n_lang)   # человекочитаемые ГБ/МБ/КБ
    templates.env.filters["dur"] = lambda v: fmt_uptime(v, state.i18n_lang)   # время работы: 5ч 12м / 5h 12m
    templates.env.filters["hms"] = _hms                                       # время снимка: HH:MM:SS
    from .model_catalog import human_bytes as _hb
    templates.env.filters["hb"] = _hb                                         # размеры моделей: 16.1 ГБ

    def render(name: str, **ctx):
        # CSRF доступен во всех шаблонах (base.html: кнопка «Выйти») — устраняет
        # сломанный logout вне страницы логина: раньше токен не передавался.
        req = ctx.get("request")
        if req is not None:
            ctx.setdefault("csrf", getattr(req.state, "csrf", "") or "")
        lang = state.i18n_lang
        ctx.setdefault("lang", lang)
        ctx.setdefault("theme", state.theme)

        def t(key: str, **kw):
            return translate(lang, key, **kw)

        ctx.setdefault("t", t)

        def temp(c):
            """Температура GPU с учётом единицы из Config Panel (°C/°F)."""
            if c is None:
                return translate(lang, "no_data")
            if state.temp_unit == "F":
                return f"{c * 9 / 5 + 32:g} °F"
            return f"{c:g} °C"

        ctx.setdefault("temp", temp)
        ctx.setdefault("gpu_vendor", gpu_vendor)   # вендор-класс карточки GPU (v0.3.14)
        # starlette 1.x (аудит №1: CVE-2025-54121 и иные advisories закрыты
        # только в 1.x/0.49+): сигнатура — (request, name, context).
        if req is None:
            raise LookupError(f"render({name!r}) вызван без 'request' в ctx")
        return templates.TemplateResponse(req, name, ctx)

    state.render = render

    def _load_services_order() -> list[str]:
        raw = db.get_setting("services_order")
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except ValueError:
            return []
        return [str(x) for x in data] if isinstance(data, list) else []

    state.services_order = _load_services_order()

    # --- middleware: Host + аутентификация (кроме /login и /static) --------
    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path.startswith("/static") or path == "/favicon.ico":
            resp = await call_next(request)
            resp.headers.setdefault("X-Content-Type-Options", "nosniff")
            return resp

        # Host/Origin проверка: доверенные хосты только (ТЗ §6.1).
        # Аудит №6: пустой Host-заголовок (HTTP/1.0-запрос без Host) раньше
        # ПРОХОДИЛ проверку («if host and …») — теперь отклоняется 400.
        # В dev_mode пустой Host допустим (локальные HTTP/1.0 клиенты).
        raw_host = request.headers.get("host") or ""
        host = raw_host.split(":")[0].lower()   # "host:port" → host; IPv6 без скобок не поддерживаем в hosts
        if not host:
            if not state.cfg.dev_mode:
                return PlainTextResponse(translate(state.i18n_lang, "bad_host"), status_code=400)
        elif host not in state.cfg.trusted_hosts:
            return PlainTextResponse(translate(state.i18n_lang, "bad_host"), status_code=403)

        # Origin (если передан) не должен чужим источником
        origin = request.headers.get("origin")
        if origin:
            o_host = origin.partition("://")[2].rpartition(":")[0].lower()
            if o_host and o_host not in state.cfg.trusted_hosts:
                return PlainTextResponse(translate(state.i18n_lang, "bad_origin"), status_code=403)

        session = None
        token = request.cookies.get(SESSION_COOKIE)
        session = get_session(db, token)
        request.state.session = session
        request.state.csrf = session["csrf"] if session else ""

        # GET не меняет состояние; любые страницы кроме /login требуют входа (ТЗ §6.1).
        # SSE-эндпоинты не перенаправляем (EventSource не следует за redirect) — отдаём 401.
        if session is None and path != "/login":
            if request.method == "GET" and path != "/events":
                nxt = request.url.path or "/"
                return RedirectResponse(f"/login?next={nxt}", status_code=302)
            return PlainTextResponse(translate(state.i18n_lang, "auth_required"), status_code=401)

        resp = await call_next(request)
        resp.headers.setdefault("Cache-Control", "no-store")
        resp.headers.setdefault("Content-Security-Policy",
                                "default-src 'self'; script-src 'self'; style-src 'self'; "
                                "img-src 'self' data:; frame-ancestors 'none'; form-action 'self'")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp

    app.include_router(routes.auth.router)
    app.include_router(routes.dashboard.router)
    app.include_router(routes.services.router)
    app.include_router(routes.logs.router)
    app.include_router(routes.config.router)
    app.include_router(routes.matrix.router)
    app.include_router(routes.config_files.router)
    app.include_router(routes.models.router)
    app.include_router(routes.wizard.router)

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    @app.get("/favicon.ico", include_in_schema=False)
    async def _favicon():
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
            '<rect width="64" height="64" rx="14" fill="#141a24"/>'
            '<rect x="11" y="13" width="42" height="16" rx="4" fill="#223047"/>'
            '<rect x="11" y="35" width="42" height="16" rx="4" fill="#223047"/>'
            '<circle cx="19" cy="21" r="3.2" fill="#73e0c0"/>'
            '<circle cx="19" cy="43" r="3.2" fill="#73e0c0"/>'
            '<rect x="27" y="19.5" width="20" height="3" rx="1.5" fill="#3d5373"/>'
            '<rect x="27" y="25" width="14" height="3" rx="1.5" fill="#3d5373"/>'
            '<rect x="27" y="41.5" width="20" height="3" rx="1.5" fill="#3d5373"/>'
            '<rect x="27" y="47" width="14" height="3" rx="1.5" fill="#3d5373"/>'
            "</svg>"
        )
        return Response(svg, media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=86400"})

    return app


def create_app_from_config_file(config_path: str) -> FastAPI:
    return create_app(load_config(config_path))


# Интервал периодической чистки БД (аудит №4): истёкшие сессии, старые
# login_attempts и audit (старше RETENTION_DAYS из db.Database).
CLEANUP_INTERVAL_S = 6 * 3600


async def _cleanup_loop(db: Database) -> None:
    loop = asyncio.get_running_loop()
    while True:
        try:
            await loop.run_in_executor(None, db.cleanup)
        except Exception:  # noqa: BLE001 — цикл чистки не должен падать
            pass
        await asyncio.sleep(CLEANUP_INTERVAL_S)
