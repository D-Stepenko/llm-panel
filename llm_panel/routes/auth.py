"""Вход/выход (ТЗ §6.1: блокировка после 5 ошибок, аудит входов)."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse

from ..db import utcnow
from ..i18n import translate
from ..security import (
    SESSION_COOKIE,
    create_session,
    delete_session,
    get_session,
    get_user,
    verify_password,
    check_lockout,
    csrf_matches,
    record_login,
)

router = APIRouter()

_LOGIN_TOKENS: dict[str, float] = {}  # одноразовые токены формы входа → ts (10 мин)

# Аудит №5: несуществующий пользователь раньше отвечал заметно быстрее
# (verify_password не выполнялся) — перечисление логинов по времени ответа.
# Прогоняем притворную проверку на ФИКТИВНОМ, но настоящем argon2id-хеше
# (лениво сгенерированном один раз на процесс), чтобы время ответа совпало.
_DUMMY_HASH: list[str] = []


def _dummy_hash() -> str:
    if not _DUMMY_HASH:
        from ..security import hash_password
        _DUMMY_HASH.append(hash_password("bogus-dummy-password-for-timing-equalization"))
    return _DUMMY_HASH[0]


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
                            error=translate(state.i18n_lang, "login_stale"),
                            login_token=_issue_login_token())

    # ТЗ §6.1: ограничение попыток, счётчик в БД (переживает рестарт панели);
    # пара username+ip + глобальный порог на аккаунт (аудит №3)
    locked, remain_s = check_lockout(db, username, cfg.lockout_failures, cfg.lockout_minutes, ip)
    if locked:
        db.audit(ip, username, "login", "panel", "denied",
                 f"блокировка активна ещё {remain_s} c")
        return state.render(
            "login.html", request=request,
            error=translate(state.i18n_lang, "login_locked",
                            m=max(1, remain_s // 60 + 1)),
            login_token=_issue_login_token(),
        )

    user = get_user(db, username)
    if user and user["password_hash"] != "!":
        ok = verify_password(user["password_hash"], password)
    else:
        # Аудит №5: несуществующий пользователь раньше отвечал ЗНАЧИМО быстрее
        # (verify_password не выполнялся) — перечисление логинов по времени.
        # Всегда прогоняем притворную проверку на фиктивном хеше: результат
        # всегда False, время сравнимое.
        verify_password(_dummy_hash(), password)
        ok = False
    record_login(db, ip, username, ok)
    if not ok:
        db.audit(ip, username, "login", "panel", "failed", "неверный логин или пароль")
        from ..security import _consecutive_failures
        n = _consecutive_failures(db, username, ip)
        remaining = max(0, cfg.lockout_failures - n) if not locked else 0
        err = translate(state.i18n_lang, "login_bad")
        if remaining and remaining <= cfg.lockout_failures:
            err += translate(state.i18n_lang, "login_attempts_left", n=remaining)
        return state.render("login.html", request=request, error=err,
                            login_token=_issue_login_token())

    token, _csrf = create_session(db, username, cfg.session_minutes)
    db.audit(ip, username, "login", "panel", "ok", "")
    next_url = request.query_params.get("next") or "/"
    # некорректный next не позволит переход на сторонний источник.
    # Аудит №7: раньше отклонялось только "//…" — но "\\<host>" ряд браузеров
    # трактуют как scheme-relative (тот же домен). Отклоняем оба префикса.
    if (not next_url.startswith("/")
            or next_url.startswith("//")
            or next_url.startswith("/\\")
            or "\\\\" in next_url):
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
    csrf_ok = bool(request.state.session) and csrf_matches(form.get("csrf"), request.state.csrf)
    if not csrf_ok:
        return PlainTextResponse(translate(state.i18n_lang, "bad_csrf"), status_code=403)
    delete_session(state.db, token)
    state.db.audit(request.client.host if request.client else "", user,
                   "logout", "panel", "ok", "")
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp
