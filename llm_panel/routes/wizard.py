"""Мастер «модель → сервис» (этап 3, v0.3.5): READ-ONLY генератор сниппетов.

Шаг 1 (/wizard) — выбор модели из каталога;
Шаг 2 (/wizard/select) — движок (предвыбор по kind), карты (UUID с метками
занятости из матрицы), свободный порт из cfg.ports_min/max, имя юнита;
Шаг 3 (/wizard, POST) — валидный блок services.yaml + шаблон юнита + шаги
установки. Панель НИЧЕГО не пишет: root-owned файлы правит владелец (ТЗ §5.5).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from .. import __version__
from .. import model_service as ms
from ..i18n import translate
from .models import _scan

router = APIRouter(prefix="/wizard")


async def _form(request: Request):
    """FormData КАК ЕСТЬ (нужен getlist — чекбоксы карт)."""
    try:
        return await request.form()
    except Exception:  # noqa: BLE001
        from starlette.datastructures import FormData
        return FormData()


def _model_by_path(data: dict, path: str):
    for m in data.get("models", []):
        if m.path == path:
            return m
    return None


def _indices_of(state, gpus: list[str]) -> tuple[int, ...]:
    """Текущие индексы выбранных карт (по порядку NVML) — подсказка для CVD."""
    snap = getattr(state.collector, "snapshot", None)
    if not snap or not snap.gpus:
        return ()
    idx = {g["uuid"]: g.get("index") for g in snap.gpus}
    return tuple(idx[u] for u in gpus if idx.get(u) is not None)


@router.get("", response_class=HTMLResponse)
async def wizard_page(request: Request):
    """Шаг 1: выбор модели из каталога."""
    state = request.app.state.panel
    data = await _scan(state)
    err = request.query_params.get("err") or ""
    return state.render(
        "wizard.html", request=request, version=__version__,
        step=1, data=data, err=err if err in ("no_model",) else "",
    )


@router.post("/select", response_class=HTMLResponse)
async def wizard_select(request: Request):
    """Шаг 2: движок, карты, порт, имя юнита (предзаполнены «разумно»)."""
    state = request.app.state.panel
    form = await _form(request)
    if form.get("csrf") != request.state.csrf:
        return PlainTextResponse(translate(state.i18n_lang, "bad_csrf"), status_code=403)
    data = await _scan(state)
    entry = _model_by_path(data, str(form.get("model", "")))
    if entry is None:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/wizard?err=no_model", status_code=303)

    engine = "llamacpp" if entry.kind == "gguf" else "vllm"
    gctx = ms.gpu_context(state)
    port, port_err = ms.free_port(state, str(form.get("port", "")))
    return state.render(
        "wizard.html", request=request, version=__version__,
        step=2, entry=entry, engine=engine, engines=ms.ENGINES,
        gpus=gctx["gpus"], port=port, port_err=port_err,
        unit=ms.suggest_unit(entry.name, engine),
        ports_min=state.cfg.ports_min, ports_max=state.cfg.ports_max,
    )


@router.post("", response_class=HTMLResponse)
async def wizard_generate(request: Request):
    """Шаг 3: сгенерировать сниппет services.yaml + шаблон юнита."""
    state = request.app.state.panel
    form = await _form(request)
    if form.get("csrf") != request.state.csrf:
        return PlainTextResponse(translate(state.i18n_lang, "bad_csrf"), status_code=403)
    data = await _scan(state)
    entry = _model_by_path(data, str(form.get("model", "")))
    if entry is None:
        return state.render("wizard.html", request=request, version=__version__,
                            step=1, data=data, err="no_model")

    engine = str(form.get("engine", "")).strip()
    if engine not in ms.ENGINES:
        return state.render("wizard.html", request=request, version=__version__,
                            step=2, entry=entry, engine="llamacpp",
                            engines=ms.ENGINES, gpus=ms.gpu_context(state)["gpus"],
                            port=None, port_err="wizard_err_engine",
                            unit=ms.suggest_unit(entry.name, "llamacpp"),
                            ports_min=state.cfg.ports_min,
                            ports_max=state.cfg.ports_max)

    unit = str(form.get("unit", "")).strip()
    if not ms.UNIT_NAME_RE.match(unit):
        return state.render("wizard.html", request=request, version=__version__,
                            step=2, entry=entry, engine=engine,
                            engines=ms.ENGINES, gpus=ms.gpu_context(state)["gpus"],
                            port=None, port_err="wizard_err_unit",
                            unit=ms.suggest_unit(entry.name, engine),
                            ports_min=state.cfg.ports_min,
                            ports_max=state.cfg.ports_max)

    gpus = ms.valid_gpus(state, form.getlist("gpus"))
    port, port_err = ms.free_port(state, str(form.get("port", "")))
    if port is None:
        return state.render("wizard.html", request=request, version=__version__,
                            step=2, entry=entry, engine=engine,
                            engines=ms.ENGINES, gpus=ms.gpu_context(state)["gpus"],
                            port=None, port_err=port_err, unit=unit,
                            ports_min=state.cfg.ports_min,
                            ports_max=state.cfg.ports_max)

    addressing = "index" if engine == "vllm" else "uuid"
    indices = _indices_of(state, gpus) if addressing == "index" else ()
    conflicts = ms.gpu_conflicts(state, gpus)
    svc = ms.build_service(entry, engine, gpus, port, unit, addressing)
    yaml_text = ms.render_yaml(svc)
    unit_text = ms.unit_template(unit, entry, engine, port, gpus, indices)

    ip = request.client.host if request.client else ""
    state.db.audit(ip, request.state.session["username"], "wizard_generate",
                   entry.name, "ok", f"engine={engine} port={port} gpus={len(gpus)}")

    return state.render(
        "wizard.html", request=request, version=__version__,
        step=3, entry=entry, engine=engine, unit=unit, port=port,
        gpus=gpus, conflicts=conflicts,
        yaml_text=yaml_text, unit_text=unit_text,
        services_file=state.cfg.services_file,
    )
