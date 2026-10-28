"""Редактор конфигураций сервисов (этап 2): версии/diff/rollback в SQLite.

Границы безопасности:
  * редактируются ТОЛЬКО файлы, зарегистрированные администратором панели и
    доступные ПО ЧТЕНИЮ И ЗАПИСИ самому пользователю панели (dispot);
  * root-owned файлы (/etc/llm-panel/*, системные юниты) — откат запроса:
    ТЗ §5.5/§9, панель не пишет чужое;
  * .json проверяется парсером ДО записи; запись атомарная (tmp + os.replace);
  * каждая правка/rollback остаётся версией в SQLite (последние 50 на файл);
    в аудит пишется событие, но НЕ содержимое.

Файлы регистрируются в Config Panel → «Файлы конфигураций» (settings.config_files).
"""

from __future__ import annotations

import difflib
import json
import os
import pathlib

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from ..i18n import translate

router = APIRouter(prefix="/config/files")

MAX_FILE_BYTES = 512 * 1024
FORBIDDEN_PREFIXES = ("/etc/llm-panel",)   # root-owned конфиги панели (ТЗ §5.5)


def _files_list(state) -> list[dict]:
    raw = state.db.get_setting("config_files")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def _save_files_list(state, items: list[dict]) -> None:
    state.db.set_setting("config_files", json.dumps(items, ensure_ascii=False))


def _entry(state, fid: int) -> dict | None:
    return next((e for e in _files_list(state) if e.get("id") == fid), None)


def _validate_new_path(raw: str) -> tuple[str | None, str]:
    """Возвращает (путь | None, ключ ошибки)."""
    p = pathlib.Path(raw.strip())
    # root-owned конфиги панели запрещены независимо от платформы/абсолютности
    if p.as_posix().startswith(FORBIDDEN_PREFIXES):
        return None, "cfgfile_err_forbidden"
    if not p.is_absolute():
        return None, "cfgfile_err_relative"
    if not p.is_file():
        return None, "cfgfile_err_missing"
    if p.stat().st_size > MAX_FILE_BYTES:
        return None, "cfgfile_err_size"
    if hasattr(os, "geteuid") and p.stat().st_uid != os.geteuid():
        return None, "cfgfile_err_owner"
    if not os.access(p, os.R_OK | os.W_OK):
        return None, "cfgfile_err_access"
    return str(p), ""


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def _write_atomic(p: pathlib.Path, content: str) -> None:
    tmp = p.with_name(p.name + ".tmp-panel")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    os.replace(tmp, p)


def _ctx(state, request: Request, **extra):
    ctx = {
        "username": request.state.session["username"],
        "files": _files_list(state),
    }
    ctx.update(extra)
    return ctx


@router.get("", response_class=HTMLResponse)
async def files_page(request: Request):
    state = request.app.state.panel
    return state.render("files_list.html", request=request,
                        version=_version(), **_ctx(state, request))


@router.post("/add")
async def files_add(request: Request):
    state = request.app.state.panel
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001
        form = {}
    if form.get("csrf") != request.state.csrf:
        return PlainTextResponse(translate(state.i18n_lang, "bad_csrf"), status_code=403)

    path, err = _validate_new_path(str(form.get("path", "")))
    if err:
        return state.render("files_list.html", request=request, version=_version(),
                            err=err, **_ctx(state, request))
    items = _files_list(state)
    if any(e.get("path") == path for e in items):
        return state.render("files_list.html", request=request, version=_version(),
                            err="cfgfile_err_dup", **_ctx(state, request))

    fid = (max((e.get("id", 0) for e in items), default=0) + 1)
    items.append({"id": fid, "path": path,
                  "label": str(form.get("label", "")).strip() or pathlib.Path(path).name})
    _save_files_list(state, items)

    # базовый снапшот текущего содержимого — точка для будущих diff/rollback
    content = _read(pathlib.Path(path))
    state.db.add_config_version(path, content,
                                request.state.session["username"], note="baseline")
    ip = request.client.host if request.client else ""
    state.db.audit(ip, request.state.session["username"],
                   "config_file_add", path, "ok")
    return state.render("files_list.html", request=request, version=_version(),
                        ok="cfgfile_added", **_ctx(state, request))


@router.post("/{fid}/remove")
async def files_remove(request: Request, fid: int):
    state = request.app.state.panel
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001
        form = {}
    if form.get("csrf") != request.state.csrf:
        return PlainTextResponse(translate(state.i18n_lang, "bad_csrf"), status_code=403)
    entry = _entry(state, fid)
    if entry is None:
        return PlainTextResponse(translate(state.i18n_lang, "cfgfile_err_missing"), status_code=404)
    items = [e for e in _files_list(state) if e.get("id") != fid]
    _save_files_list(state, items)
    ip = request.client.host if request.client else ""
    state.db.audit(ip, request.state.session["username"],
                   "config_file_remove", entry["path"], "ok")
    return state.render("files_list.html", request=request, version=_version(),
                        ok="cfgfile_removed", **_ctx(state, request))


@router.get("/{fid}", response_class=HTMLResponse)
async def editor(request: Request, fid: int, diff: int | None = None):
    state = request.app.state.panel
    entry = _entry(state, fid)
    if entry is None:
        return PlainTextResponse(translate(state.i18n_lang, "cfgfile_err_missing"), status_code=404)
    p = pathlib.Path(entry["path"])
    try:
        content = _read(p)
    except OSError as e:
        return state.render("files_list.html", request=request, version=_version(),
                            err="cfgfile_err_read", **_ctx(state, request, fpath=p))

    diff_text = ""
    diff_from = None
    if diff is not None:
        ver = state.db.config_version(diff)
        if ver is not None and ver["path"] == entry["path"]:
            diff_from = ver
            diff_text = "\n".join(difflib.unified_diff(
                ver["content"].splitlines(), content.splitlines(),
                fromfile=f"#{ver['id']} ({_ts(ver['ts'])})",
                tofile="current", lineterm=""))

    return state.render(
        "files_edit.html", request=request, version=_version(),
        **_ctx(state, request, entry=entry, content=content, versions=_versions(state, entry),
               diff_text=diff_text, diff_from=diff_from,
               is_json=p.suffix == ".json", fpath=p))


@router.post("/{fid}/save")
async def save(request: Request, fid: int):
    state = request.app.state.panel
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001
        form = {}
    if form.get("csrf") != request.state.csrf:
        return PlainTextResponse(translate(state.i18n_lang, "bad_csrf"), status_code=403)
    entry = _entry(state, fid)
    if entry is None:
        return PlainTextResponse(translate(state.i18n_lang, "cfgfile_err_missing"), status_code=404)
    p = pathlib.Path(entry["path"])
    content = str(form.get("content", ""))
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        return _editor_err(state, request, entry, "cfgfile_err_size")

    if p.suffix == ".json":
        try:
            json.loads(content)
        except ValueError as e:
            return _editor_err(state, request, entry, "cfgfile_err_json", detail=str(e))

    try:
        _write_atomic(p, content)
    except OSError as e:
        return _editor_err(state, request, entry, "cfgfile_err_write", detail=str(e))

    state.db.add_config_version(entry["path"], content,
                                request.state.session["username"], note="правка")
    ip = request.client.host if request.client else ""
    state.db.audit(ip, request.state.session["username"],
                   "config_file_save", entry["path"], "ok",
                   f"{len(content.encode('utf-8'))} bytes")
    return state.render(
        "files_edit.html", request=request, version=_version(),
        **_ctx(state, request, entry=entry, content=content,
               versions=_versions(state, entry), diff_text="", diff_from=None,
               is_json=p.suffix == ".json", ok="cfgfile_saved", fpath=p))


@router.post("/{fid}/rollback/{vid}")
async def rollback(request: Request, fid: int, vid: int):
    state = request.app.state.panel
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001
        form = {}
    if form.get("csrf") != request.state.csrf:
        return PlainTextResponse(translate(state.i18n_lang, "bad_csrf"), status_code=403)
    entry = _entry(state, fid)
    ver = state.db.config_version(vid)
    if entry is None or ver is None or ver["path"] != entry["path"]:
        return PlainTextResponse(translate(state.i18n_lang, "cfgfile_err_missing"), status_code=404)
    p = pathlib.Path(entry["path"])
    content = ver["content"]
    try:
        _write_atomic(p, content)
    except OSError as e:
        return _editor_err(state, request, entry, "cfgfile_err_write", detail=str(e))
    state.db.add_config_version(entry["path"], content,
                                request.state.session["username"],
                                note=f"rollback от #{vid}")
    ip = request.client.host if request.client else ""
    state.db.audit(ip, request.state.session["username"],
                   "config_file_rollback", entry["path"], "ok", f"from #{vid}")
    return state.render(
        "files_edit.html", request=request, version=_version(),
        **_ctx(state, request, entry=entry, content=content,
               versions=_versions(state, entry), diff_text="", diff_from=None,
               is_json=p.suffix == ".json", ok="cfgfile_rolled", fpath=p))


def _versions(state, entry) -> list:
    return state.db.config_versions(entry["path"])


def _editor_err(state, request: Request, entry, err_key: str, detail: str = ""):
    p = pathlib.Path(entry["path"])
    try:
        content = _read(p)
    except OSError:
        content = ""
    return state.render(
        "files_edit.html", request=request, version=_version(),
        **_ctx(state, request, entry=entry, content=content,
               versions=_versions(state, entry), diff_text="", diff_from=None,
               is_json=p.suffix == ".json", err=err_key, err_detail=detail, fpath=p))


def _ts(ts: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _version() -> str:
    from .. import __version__
    return __version__
