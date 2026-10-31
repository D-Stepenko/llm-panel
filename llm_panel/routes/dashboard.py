"""Главная страница + SSE-поток статусов/метрик (ТЗ §5.1).

Один общий коллектор питает всех подключённых клиентов (нет опроса на клиента).
SSE-фрагменты: event «services», «gpus», «host» с отрендеренными HTML-частями.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse

from ..helper_client import HelperError, call_power
from ..i18n import translate

router = APIRouter()


def _version() -> str:
    from .. import __version__
    return __version__


def _snap_dict(snap) -> dict:
    return {
        "ts": snap.ts,
        "services": snap.services,
        "gpus": snap.gpus,
        "host": snap.host,
        "warnings": snap.warnings,
    }


def _ordered_services(state, rows: list) -> list:
    """Фильтр скрытых + сортировка карточек по сохранённому порядку.

    1) скрытые (Config Panel → «Отображение сервисов», settings.hidden_ids)
       не показываются;
    2) сортировка по сохранённому порядку («Порядок» в шапке);
    3) несохранённые/новые сервисы остаются в хвосте в исходном порядке.
    """
    rows = [r for r in rows if r.get("id") not in set(state.hidden_ids)]
    order = state.services_order or []
    if not order:
        return rows
    pos = {sid: i for i, sid in enumerate(order)}
    return [r for _, r in sorted(
        enumerate(rows),
        key=lambda t: (pos.get(t[1].get("id"), len(order)), t[0]),
    )]


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    state = request.app.state.panel
    snap = state.collector.snapshot
    initial = {"services": "", "gpus": "", "host": ""}
    if snap is not None:
        snap_d = _snap_dict(snap)
        snap_d["services"] = _ordered_services(state, snap_d["services"])
        for name in initial:
            initial[name] = state.render(
                f"_{name}.html", request=request, snapshot=snap_d
            ).body.decode("utf-8")
    return state.render(
        "dashboard.html",
        request=request,
        username=request.state.session["username"],
        version=_version(),
        sse_available=initial["services"] != "",
        snapshot_ts=snap.ts if snap is not None else None,
        initial_services=initial["services"],
        initial_gpus=initial["gpus"],
        initial_host=initial["host"],
    )


@router.get("/audit")
async def audit_tail(request: Request, limit: int | None = None):
    """Последние события аудита (для обзора). Только GET, только для вошедшего администратора."""
    state = request.app.state.panel
    if limit is None:
        limit = state.audit_limit            # по умолчанию — из Config Panel
    limit = max(1, min(int(limit), 200))
    rows = []
    for r in state.db.audit_tail(limit):
        d = dict(r)
        d["ts_str"] = _dt.datetime.fromtimestamp(
            d["ts"], _dt.timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S UTC")
        rows.append(d)
    return state.render("audit.html", request=request, rows=rows)


@router.post("/ui/order")
async def save_services_order(request: Request):
    """Сохранение порядка карточек сервисов (кнопка «Порядок» → «Запомнить»).

    Порядокосуществляется по id сервисов; неизвестные id отбрасываются.
    """
    state = request.app.state.panel
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001
        form = {}
    if form.get("csrf") != request.state.csrf:
        return PlainTextResponse(translate(state.i18n_lang, "bad_csrf"), status_code=403)

    ids: list[str] = []
    extra_ids = {d.id for d in state.extra_defs.values()}
    for piece in str(form.get("order", "")).split(","):
        piece = piece.strip()
        if piece and piece not in ids and (
                state.registry.by_id(piece) is not None or piece in extra_ids):
            ids.append(piece)

    state.services_order = ids
    state.db.set_setting("services_order", json.dumps(ids))
    ip = request.client.host if request.client else ""
    state.db.audit(ip, request.state.session["username"],
                   "ui_order", "services", "ok", "порядок: " + ",".join(ids))
    return PlainTextResponse(translate(state.i18n_lang, "order_saved"))


@router.post("/ui/models-order")
async def save_models_order(request: Request):
    """Сохранение ручного порядка строк моделей (/models, кнопка «sort»).

    Порядок задаётся ПОЛНЫМИ путями моделей; посторонние значения
    отбрасываются, максимум 200 строк.
    """
    state = request.app.state.panel
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001
        form = {}
    if form.get("csrf") != request.state.csrf:
        return PlainTextResponse(translate(state.i18n_lang, "bad_csrf"), status_code=403)

    paths: list[str] = []
    for piece in str(form.get("order", "")).split(","):
        piece = piece.strip()
        if piece.startswith("/") and len(piece) <= 400 and piece not in paths:
            paths.append(piece)
    paths = paths[:200]

    state.db.set_setting("models_order", json.dumps(paths))
    ip = request.client.host if request.client else ""
    state.db.audit(ip, request.state.session["username"],
                   "ui_order", "models", "ok", f"порядок: {len(paths)} моделей")
    return PlainTextResponse(translate(state.i18n_lang, "order_saved"))


@router.post("/ui/power")
async def power_server(request: Request):
    """Выключение/перезагрузка СТАНЦИИ с панели (v0.3.12, Config panel низ).

    Выполняется через root-owned helper: `sudo -n <helper> power <op>`;
    сам helper разрешает операцию ТОЛЬКО при явной секции "power" в
    allowlist.json (безопасный дефолт — отказ). На клиенте кнопка
    требует подтверждения и отсчёта 5 с с отменой (app.js).
    """
    state = request.app.state.panel
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001
        form = {}
    if form.get("csrf") != request.state.csrf:
        return PlainTextResponse(translate(state.i18n_lang, "bad_csrf"), status_code=403)

    op = str(form.get("action", "")).strip()
    if op not in ("poweroff", "reboot"):
        return PlainTextResponse(translate(state.i18n_lang, "bad_request"), status_code=400)

    ip = request.client.host if request.client else ""
    user = request.state.session["username"]
    state.db.audit(ip, user, f"power:{op}", "server", "started",
                   "команда с панели (подтверждена клиентом, отсчёт 5 с)")
    try:
        call_power(state.cfg.helper_path, op)
    except HelperError as e:
        state.db.audit(ip, user, f"power:{op}", "server", "helper_error", e.message)
        return PlainTextResponse(e.message)
    state.db.audit(ip, user, f"power:{op}", "server", "ok",
                   "задача принята systemd; хост уйдёт вниз через несколько секунд")
    return PlainTextResponse(translate(state.i18n_lang, "power_sent"))


@router.get("/events")
async def events(request: Request):
    state = request.app.state.panel
    hub = state.hub

    async def gen():
        queue = hub.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    snap = await asyncio.wait_for(queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                snap_d = _snap_dict(snap)
                snap_d["services"] = _ordered_services(state, snap_d["services"])
                for name in ("services", "gpus", "host"):
                    body = state.render(
                        f"_{name}.html", request=request, snapshot=snap_d
                    ).body.decode("utf-8")
                    data_lines = "".join(f"data: {ln}\n" for ln in body.splitlines()) or "data: \n"
                    yield f"event: {name}\n{data_lines}\n"
        finally:
            hub.unsubscribe(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",   # запрет буферизации через Caddy (readily ignored otherwise)
        },
    )
