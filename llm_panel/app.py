"""FastAPI-приложение панели: аутентификация, проверка Host/CSRF, SSE, маршруты (ТЗ §6.1)."""

from __future__ import annotations

import asyncio
import secrets
import time

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import pathlib

from .collector import Collector
from .config import PanelConfig, load_config
from .db import Database
from .metrics import MetricsCollector
from .registry import load_services
from .sse import Hub
from .security import (
    SESSION_COOKIE,
    get_session,
    check_lockout,
    hash_token,
)

BASE_DIR = pathlib.Path(__file__).resolve().parent


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
    metrics = MetricsCollector()
    hub = Hub()
    state = AppState(cfg=config, db=db, registry=registry, keys=None, metrics=metrics, hub=hub)
    state.collector = Collector(config, registry, metrics, hub)

    app = FastAPI(title="llm-panel", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.panel = state

    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    state.templates = templates

    def render(name: str, **ctx):
        return templates.TemplateResponse(name, ctx)

    state.render = render

    # --- middleware: Host + аутентификация (кроме /login и /static) --------
    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path.startswith("/static") or path == "/favicon.ico":
            resp = await call_next(request)
            resp.headers.setdefault("X-Content-Type-Options", "nosniff")
            return resp

        # Host/Origin проверка: доверенные хосты только (ТЗ §6.1)
        raw_host = request.headers.get("host") or ""
        host = raw_host.split(":")[0].lower()   # "host:port" → host; IPv6 без скобок не поддерживаем в hosts
        if host and host not in state.cfg.trusted_hosts:
            return PlainTextResponse("Недопустимый Host", status_code=403)

        # Origin (если передан) не должен чужим источником
        origin = request.headers.get("origin")
        if origin:
            o_host = origin.partition("://")[2].rpartition(":")[0].lower()
            if o_host and o_host not in state.cfg.trusted_hosts:
                return PlainTextResponse("Недопустимый Origin", status_code=403)

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
            return PlainTextResponse("Требуется авторизация", status_code=401)

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

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    @app.on_event("startup")
    async def _startup():
        await state.collector.start()

    @app.on_event("shutdown")
    async def _shutdown():
        await state.collector.stop()
        metrics.shutdown()
        db.close()

    return app


def create_app_from_config_file(config_path: str) -> FastAPI:
    return create_app(load_config(config_path))
