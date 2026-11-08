"""Аутентификация: Argon2-хеш пароля, сессии (БД), CSRF, ограничение попыток входа.

ТЗ §6.1/6.2:
- пароль хранится только как Argon2-хеш;
- cookie: HttpOnly, Secure, SameSite=Strict, ограниченный срок;
- инвалиждение всех сессий после сброса пароля;
- блокировка после 5 последовательных ошибок, счётчик в БД (переживает рестарт панели);
- серверное восстановление доступа — CLI-командой (llm_panel.cli lockout-reset).
"""

from __future__ import annotations

import hashlib
import os
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

from .db import Database, utcnow

_ph = PasswordHasher()  # параметры по умолчанию (argon2id) достаточны

SESSION_COOKIE = "llm_panel_session"


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError, ValueError):
        return False


def csrf_matches(form_value: str | None, expected: str | None) -> bool:
    """Constant-time сравнение CSRF-токена (аудит №10: одна копия проверки
    на все POST-роуты вместо 17 разных; сравнение — secrets.compare_digest).
    Пустой ожидаемый токен (нет сессии) — отказ."""
    if not expected or not form_value:
        return False
    return secrets.compare_digest(str(form_value), expected)


def hash_token(token: str) -> str:
    """Токен сессии хранится в БД в хешированном виде."""
    return hashlib.sha256(token.encode()).hexdigest()


# -- сессии -------------------------------------------------------------------

def create_session(db: Database, username: str, session_minutes: int) -> tuple[str, str]:
    """Возвращает (token, csrf). Токен в cookie, в БД только его SHA-256."""
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    now = utcnow()
    db.execute(
        "INSERT INTO sessions (token_hash, username, csrf, created_at, expires_at, last_seen) "
        "VALUES (?,?,?,?,?,?)",
        (hash_token(token), username, csrf, now, now + session_minutes * 60, now),
    )
    return token, csrf


def get_session(db: Database, token: str | None):
    if not token:
        return None
    row = db.query_one("SELECT * FROM sessions WHERE token_hash = ?", (hash_token(token),))
    if row is None:
        return None
    now = utcnow()
    if row["expires_at"] < now:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(token),))
        return None
    # обновление активности не критично для stale-read'ов; пишем без блокировки нагрузки
    db.execute("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (now, hash_token(token)))
    return row


def delete_session(db: Database, token: str | None) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(token),))


def delete_sessions_for_user(db: Database, username: str) -> None:
    """Инвалидация всех сессий (например, после сброса пароля)."""
    db.execute("DELETE FROM sessions WHERE username = ?", (username,))


# -- ограничение попыток входа --------------------------------------------------
#
# Аудит №3: раньше считались неудачи ТОЛЬКО по username — любой знающий логин
# держал учётку заблокированной перманентно (5 POST /login каждые N минут),
# а успешный вход сбрасывал счётчик со ВСЕХ IP. Теперь два контура:
#   1) пара (username, ip) — failures_max неудач подряд с одного адреса
#      блокируют ЭТОТ адрес на lockout_minutes (стандартный сценарий ошибки
#      пароля у легитимного пользователя);
#   2) аккаунт в целом — failures_max * GLOBAL_MULT неудач подряд с любых
#      адресов блокируют логин на lockout_minutes * GLOBAL_WINDOW_MULT
#      (распределённый перебор требует заметно больше времени; легитимный
#      вход «рвёт» глобальную цепочку).

GLOBAL_MULT = 3          # глобальный порог на аккаунт: failures_max * 3 попыток
GLOBAL_WINDOW_MULT = 3   # глобальное окно блокировки: lockout_minutes * 3


def _consecutive_failures(db: Database, username: str, ip: str | None = None) -> int:
    if ip is not None:
        rows = db.query_all(
            "SELECT ok FROM login_attempts WHERE username = ? AND ip = ? "
            "ORDER BY id DESC LIMIT 64", (username, ip),
        )
    else:
        rows = db.query_all(
            "SELECT ok FROM login_attempts WHERE username = ? ORDER BY id DESC LIMIT 64",
            (username,),
        )
    n = 0
    for r in rows:
        if r["ok"]:
            break
        n += 1
    return n


def check_lockout(db: Database, username: str, failures_max: int, lockout_minutes: int,
                  ip: str = "") -> tuple[bool, int]:
    """(заблокирован, секунд до разблокировки). Пара username+ip + глобальный
    порог на аккаунт с бóльшими порогом и окном (аудит №3)."""
    remain = 0
    pair_fail = _consecutive_failures(db, username, ip or "")
    if pair_fail >= failures_max:
        row = db.query_one(
            "SELECT ts FROM login_attempts WHERE username = ? AND ip = ? AND ok = 0 "
            "ORDER BY id DESC LIMIT 1", (username, ip or ""),
        )
        if row is not None:
            remain = int(row["ts"] + lockout_minutes * 60 - utcnow())
    global_fail = _consecutive_failures(db, username)
    if global_fail >= failures_max * GLOBAL_MULT:
        row = db.query_one(
            "SELECT ts FROM login_attempts WHERE username = ? AND ok = 0 "
            "ORDER BY id DESC LIMIT 1", (username,),
        )
        if row is not None:
            g_minutes = lockout_minutes * GLOBAL_WINDOW_MULT
            remain = max(remain, int(row["ts"] + g_minutes * 60 - utcnow()))
    if remain <= 0:
        return False, 0
    return True, remain


def record_login(db: Database, ip: str, username: str, ok: bool) -> None:
    db.execute(
        "INSERT INTO login_attempts (ts, ip, username, ok) VALUES (?,?,?,?)",
        (utcnow(), ip or "", username, 1 if ok else 0),
    )
    if ok:
        # успешный вход сбрасывает счётчик ТОЛЬКО этого адреса: неудачи с других IP
        # (перебор) числиться не перестают (аудит №3)
        db.execute(
            "DELETE FROM login_attempts WHERE username = ? AND ip = ? AND ok = 0",
            (username, ip or ""),
        )


def reset_lockout(db: Database, username: str) -> int:
    """Серверное восстановление доступа: очистить счётчик (CLI)."""
    cur = db.execute("DELETE FROM login_attempts WHERE username = ? AND ok = 0", (username,))
    return cur.rowcount


def set_password(db: Database, username: str, password: str) -> None:
    db.execute(
        "INSERT INTO users (username, password_hash, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(username) DO UPDATE SET password_hash = excluded.password_hash, "
        "updated_at = excluded.updated_at",
        (username, hash_password(password), utcnow()),
    )
    delete_sessions_for_user(db, username)  # ТЗ §6.1: сессии сбрасываются после смены пароля


def get_user(db: Database, username: str):
    return db.query_one("SELECT * FROM users WHERE username = ?", (username,))


__all__ = [
    "SESSION_COOKIE", "hash_password", "verify_password", "hash_token", "csrf_matches",
    "create_session", "get_session", "delete_session", "delete_sessions_for_user",
    "check_lockout", "record_login", "reset_lockout", "set_password", "get_user",
    "os",
]
