"""Логи юнитов через ограниченный helper (ТЗ §5.4).

- просмотр последних N строк (N ограничено конфигом, по умолчанию кап 2000);
- поток Journal-Follow через SSE;
- скачивание с ограничением размера;
- маскирование известных секретов (TZ: не выводить сохранённые секреты);
- аргументы journalctl фиксированы в helper, браузер не задаёт их (ТЗ §5.4);
"""

from __future__ import annotations

import asyncio
import re
import subprocess

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse

from ..helper_client import HelperError, call_helper, spawn_helper_follow

router = APIRouter(prefix="/services")


def _redact(text: str, patterns: tuple) -> str:
    for p in patterns:
        try:
            text = re.sub(p, "【скрыто】", text)
        except re.error:
            continue
    return text


def _clamp_lines(lines: int, cfg) -> int:
    try:
        lines = int(lines)
    except (TypeError, ValueError):
        lines = 200
    return max(1, min(lines, cfg.log_max_lines))


@router.get("/{svc_id}/logs")
async def logs_page(request: Request, svc_id: str, lines: int = 200, filter: str = ""):
    state = request.app.state.panel
    svc = state.registry.by_id(svc_id)
    if svc is None:
        return PlainTextResponse("Сервис не найден", status_code=404)
    n = _clamp_lines(lines, state.cfg)
    try:
        result = call_helper(state.cfg.helper_path, "journal", svc.unit, extra=(str(n),), timeout=30)
        text = _redact(result.stdout or "(журнал пуст)", state.cfg.redact_patterns)
        err = ""
    except HelperError as e:
        text = ""
        err = e.message
    return state.render(
        "logs.html",
        request=request,
        svc=svc,
        text=text,
        err=err,
        lines=n,
        filt=filter[:200],
    )


@router.get("/{svc_id}/logs/stream")
async def logs_stream(request: Request, svc_id: str, lines: int = 50):
    """SSE-поток журнала через helper journal-follow (аргументы фиксированы)."""
    state = request.app.state.panel
    svc = state.registry.by_id(svc_id)
    if svc is None:
        return PlainTextResponse("Сервис не найден", status_code=404)
    n = _clamp_lines(lines, state.cfg)

    async def gen():
        proc = None
        try:
            proc = spawn_helper_follow(state.cfg.helper_path, svc.unit, min(n, 200))
            # helper либо отдаёт поток journalctl, либо сразу завершается (ошибка)
            assert proc.stdout is not None
            first = True
            while True:
                line = await asyncio.to_thread(proc.stdout.readline)
                if not line:
                    if first:
                        stderr = (proc.stderr.read() if proc.stderr else "")[:300]
                        yield _event("error", stderr or f"helper завершился, код {proc.wait()}")
                    else:
                        yield _event("end", "")
                    break
                first = False
                line = line.rstrip("\r\n")
                if any(ord(c) < 9 for c in line):
                    continue  # управляющие символы не пропускаем
                yield _event("log", _redact(line, state.cfg.redact_patterns))
                if await request.is_disconnected():
                    break
        finally:
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    try:
                        proc.wait(3)
                    except subprocess.TimeoutExpired:  # noqa: F821
                        proc.kill()
                except Exception:  # noqa: BLE001
                    pass

    async def _event(name: str, text: str) -> str:
        data = "".join(f"data: {ln}\n" for ln in text.splitlines()) or "data: \n"
        return f"event: {name}\n{data}\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/{svc_id}/logs/download")
async def logs_download(request: Request, svc_id: str, lines: int = 1000):
    """Скачивание журнала (GET — только чтение). Размер и N ограничены."""
    state = request.app.state.panel
    svc = state.registry.by_id(svc_id)
    if svc is None:
        return PlainTextResponse("Сервис не найден", status_code=404)
    n = _clamp_lines(lines, state.cfg)
    try:
        result = call_helper(state.cfg.helper_path, "journal", svc.unit, extra=(str(n),), timeout=30)
        text = _redact(result.stdout, state.cfg.redact_patterns)
    except HelperError as e:
        text = f"ОШИБКА: {e.message}\n"
    max_bytes = state.cfg.log_download_max_mb * 1024 * 1024
    payload = text.encode("utf-8", "replace")[:max_bytes]
    return PlainTextResponse(
        payload.decode("utf-8", "replace"),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{svc.unit}.log"',
            "Cache-Control": "no-store",
        },
    )
