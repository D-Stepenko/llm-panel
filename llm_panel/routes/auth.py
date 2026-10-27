"""Вход/выход (ТЗ §6.1: блокировка после 5 ошибок, аудит входов)."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse

from ..db import utcnow
from ..security import (
    SESSION_COOKIE,
    create_session,
    delete_session,
    get_session,
    get_user,
    verify_password,
    check_lockout,
    record_login,
)

router = APIRouter()

_LOGIN_TOKENS: dict[str, float] = {}  # одноразовые токены формы входа → ts (10 мин)


def _issue_login_token() -> str:
    token = secrets.token_urlsafe(24)
    # лёгкая чистка: держим максимум 1000 токенов
    if len(_LOGIN_TOKENS) > 1000:
        cutoff = utcnow() - 600
        for k, ts in list(_LOGIN_TOKENS.items()):
            if ts < cutoff:
                _LOGIN_TOKENS.pop(k, None)
    _LOGIN_TOKENS[token] = utcnow()
    return token


def _consume_login_token(token: str) -> bool:
    ts = _LOGIN_TOKENS.pop(token or "", 0)
    return bool(ts and utcnow() - ts < 600)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    state = request.app.state.panel
    # уже вошёл — на главную
    if request.state.session is not None:
        return RedirectResponse("/", status_code=302)
    return state.render("login.html", request=request, error="", login_token=_issue_login_token())


@router.post("/login")
async def login_submit(request: Request, username: str = Form(default=""),
                       password: str = Form(default=""), login_token: str = Form(default="")):
    state = request.app.state.panel
    cfg = state.cfg
    db = state.db
    ip = request.client.host if request.client else ""

    if not _consume_login_token(login_token):
        return state.render("login.html", request=request,
                            error="Форма устарела, обновите страницу.",
                            login_token=_issue_login_token())

    # ТЗ §6.1: ограничение попыток, счётчик в БД (переживает рестарт панели)
    locked, remain_s = check_lockout(db, username, cfg.lockout_failures, cfg.lockout_minutes)
    if locked:
        db.audit(ip, username, "login", "panel", "denied",
                 f"блокировка активна ещё {remain_s} c")
        return state.render(
            "login.html", request=request,
            error=f"Слишком много неудачных попыток. Повторите через {max(1, remain_s // 60 + 1)} мин.",
            login_token=_issue_login_token(),
        )

    user = get_user(db, username)
    ok = bool(user and user["password_hash"] != "!" and verify_password(user["password_hash"], password))
    record_login(db, ip, username, ok)
    if not ok:
        db.audit(ip, username, "login", "panel", "failed", "неверный логин или пароль")
        from ..security import _consecutive_failures
        n = _consecutive_failures(db, username)
        remaining = max(0, cfg.lockout_failures - n) if not locked else 0
        err = "Неверный логин или пароль."
        if remaining and remaining <= cfg.lockout_failures:
            err += f" Осталось попыток: {remaining}."
        return state.render("login.html", request=request, error=err,
                            login_token=_issue_login_token())

    token, _csrf = create_session(db, username, cfg.session_minutes)
    db.audit(ip, username, "login", "panel", "ok", "")
    next_url = request.query_params.get("next") or "/"
    # некорректный next не позволит переход на сторонний источник
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/"
    resp = RedirectResponse(next_url, status_code=303)
    resp.set_cookie(
        SESSION_COOKIE, token,
        max_age=cfg.session_minutes * 60,
        httponly=True,
        secure=cfg.cookie_secure,
        samesite="strict",
        path="/",
    )
    return resp


@router.post("/logout")
async def logout(request: Request):
    state = request.app.state.panel
    token = request.cookies.get(SESSION_COOKIE)
    user = request.state.session["username"] if request.state.session else ""
    # CSRF для POST (ТЗ §6.1): форма содержит токен сессии
    form = await request.form()
    csrf_ok = bool(request.state.session) and form.get("csrf") == request.state.csrf
    if not csrf_ok:
        return PlainTextResponse("Недопустимый CSRF-токен", status_code=403)
    delete_session(state.db, token)
    state.db.audit(request.client.host if request.client else "", user,
                   "logout", "panel", "ok", "")
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp
