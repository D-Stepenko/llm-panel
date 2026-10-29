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
from .. import hf_downloads as hf
from ..i18n import translate
from ..model_catalog import MAX_DEPTH_DEFAULT, MAX_ENTRIES_DEFAULT, scan_models

router = APIRouter(prefix="/models")

SCAN_TTL_S = 60.0          # повторный скан не чаще (кэш на состоянии панели)
SCAN_TIMEOUT_S = 60.0      # страховка: большие корни не должны вешать поток


def _jobs(state) -> dict:
    if getattr(state, "hf_jobs", None) is None:
        state.hf_jobs = {}
    return state.hf_jobs


def _ctx(state, request: Request, **extra) -> dict:
    jobs = sorted(_jobs(state).values(),
                  key=lambda j: (0 if j.status == "running" else 1, j.started),
                  reverse=False)
    ctx = {
        "hf_tool": hf.find_tool(getattr(state, "hf_tool_path", "") or None),
        "hf_install_variants": hf.HF_INSTALL_VARIANTS,
        "hf_find_path_cmd": hf.HF_FIND_PATH_CMD,
        "jobs": jobs,
    }
    ctx.update(extra)
    return ctx


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
        **_ctx(state, request),
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
        **_ctx(state, request),
    )


# --------------------------------------------------- HF-загрузки (v0.3.1) ----

async def _form(request: Request) -> dict:
    try:
        return dict(await request.form())
    except Exception:  # noqa: BLE001
        return {}


@router.post("/download")
async def download(request: Request):
    state = request.app.state.panel
    form = await _form(request)
    if form.get("csrf") != request.state.csrf:
        return PlainTextResponse(translate(state.i18n_lang, "bad_csrf"), status_code=403)

    data = await _scan(state)
    ctx = _ctx(state, request)

    tool = ctx["hf_tool"]
    if tool is None:
        return state.render("models.html", request=request, version=__version__,
                            data=data, roots=state.model_roots,
                            err="hf_err_no_tool", **ctx)

    repo = str(form.get("repo", "")).strip()
    if not hf.validate_repo(repo):
        return state.render("models.html", request=request, version=__version__,
                            data=data, roots=state.model_roots,
                            err="hf_err_repo", **ctx)

    root = str(form.get("root", "")).strip()
    if root not in (state.model_roots or []):
        return state.render("models.html", request=request, version=__version__,
                            data=data, roots=state.model_roots,
                            err="hf_err_root", **ctx)
    dest, err_key = hf.resolve_dest(root, str(form.get("subpath", "")))
    if dest is None:
        return state.render("models.html", request=request, version=__version__,
                            data=data, roots=state.model_roots,
                            err=err_key, **ctx)

    # hf пишет метаданные (.cache/huggingface) ВНУТРИ dest: read-only ФС или
    # чужие права ловим заранее — иначе загрузчик падает Errno 30 / 500.
    werr = hf.check_writable(dest)
    if werr:
        return state.render("models.html", request=request, version=__version__,
                            data=data, roots=state.model_roots,
                            err=werr, **ctx)

    if hf.active_count(_jobs(state)) >= hf.MAX_CONCURRENT:
        return state.render("models.html", request=request, version=__version__,
                            data=data, roots=state.model_roots,
                            err="hf_err_busy", **ctx)

    await hf.start_job(state, tool, repo, dest)
    ip = request.client.host if request.client else ""
    state.db.audit(ip, request.state.session["username"], "hf_download",
                   repo, "ok", dest)
    return state.render("models.html", request=request, version=__version__,
                        data=data, roots=state.model_roots,
                        ok="hf_started", **_ctx(state, request))


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_fragment(request: Request):
    """HTMX-фрагмент: список задач, опрашивается каждые 2 с."""
    state = request.app.state.panel
    return state.render("_downloads.html", request=request, jobs=_jobs(state).values())


async def _job_action(request: Request, jid: str):
    """Общий хвост stop/clear: CSRF + поиск задачи. Возвращает (state, job, form)."""
    state = request.app.state.panel
    form = await _form(request)
    if form.get("csrf") != request.state.csrf:
        return state, None, form
    job = _jobs(state).get(jid)
    return state, job, form


@router.post("/jobs/{jid}/stop")
async def job_stop(request: Request, jid: str):
    state, job, _ = await _job_action(request, jid)
    if job is None:
        return PlainTextResponse(translate(state.i18n_lang, "hf_err_no_job"),
                                 status_code=404)
    hf.request_stop(job)
    ip = request.client.host if request.client else ""
    state.db.audit(ip, request.state.session["username"], "hf_download_stop",
                   job.repo, "ok", job.dest)
    return state.render("_downloads.html", request=request, jobs=_jobs(state).values())


@router.post("/jobs/{jid}/clear")
async def job_clear(request: Request, jid: str):
    state, job, _ = await _job_action(request, jid)
    if job is None:
        return PlainTextResponse(translate(state.i18n_lang, "hf_err_no_job"),
                                 status_code=404)
    if job.status == "running" or job.status == "stopping":
        return PlainTextResponse(translate(state.i18n_lang, "hf_err_busy"), status_code=409)
    _jobs(state).pop(jid, None)
    return state.render("_downloads.html", request=request, jobs=_jobs(state).values())
