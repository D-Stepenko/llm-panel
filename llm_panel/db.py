"""SQLite-хранилище панели: пользователи, сессии, попытки входа, аудит (ТЗ §6.4)."""

from __future__ import annotations

import datetime as _dt
import pathlib
import sqlite3
import threading

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    username    TEXT NOT NULL,
    csrf        TEXT NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    last_seen   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS login_attempts (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    ip       TEXT NOT NULL,
    username TEXT NOT NULL,
    ok       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_user_ts ON login_attempts (username, ts);
CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    ip      TEXT NOT NULL,
    username TEXT NOT NULL,
    action  TEXT NOT NULL,
    object  TEXT NOT NULL,
    result  TEXT NOT NULL,
    detail  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit (ts);
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS config_versions (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    path     TEXT NOT NULL,
    ts       REAL NOT NULL,
    username TEXT NOT NULL,
    size     INTEGER NOT NULL,
    note     TEXT NOT NULL,
    content  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_config_versions_path_ts ON config_versions (path, ts);
"""


def utcnow() -> float:
    return _dt.datetime.now(_dt.timezone.utc).timestamp()


class Database:
    """Синхронный sqlite (низкий объём записи). Вызовы из async-кода — через to_thread."""

    def __init__(self, path: str | pathlib.Path):
        path = pathlib.Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def execute(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query_all(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- audit ------------------------------------------------------------
    def audit(self, ip: str, username: str, action: str, obj: str, result: str, detail: str = "") -> None:
        """Запись события аудита. ТЗ §6.4: пароли/токены/полный environment не записывать."""
        self.execute(
            "INSERT INTO audit (ts, ip, username, action, object, result, detail) VALUES (?,?,?,?,?,?,?)",
            (utcnow(), ip or "", username or "", action, obj or "", result, detail[:2000]),
        )

    def audit_tail(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.query_all("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (int(limit),))

    # -- housekeeping ------------------------------------------------------
    def cleanup(self) -> None:
        now = utcnow()
        self.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        self.execute("DELETE FROM login_attempts WHERE ts < ?", (now - 90 * 86400,))

    # -- settings (ключ-значение: порядок сервисов и пр.) ------------------
    def get_setting(self, key: str) -> str | None:
        row = self.query_one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, utcnow()),
        )

    # -- конфиги сервисов: версии/diff/rollback (этап 2) --------------------
    CONFIG_VERSIONS_KEEP = 50

    def add_config_version(self, path: str, content: str, username: str,
                           note: str = "") -> int | None:
        """Снимок содержимого файла. Одинаковый подряд — не дублируем."""
        with self._lock:
            last = self._conn.execute(
                "SELECT content FROM config_versions WHERE path = ? "
                "ORDER BY id DESC LIMIT 1", (path,)).fetchone()
        if last is not None and last["content"] == content:
            return None
        cur = self.execute(
            "INSERT INTO config_versions (path, ts, username, size, note, content) "
            "VALUES (?,?,?,?,?,?)",
            (path, utcnow(), username or "", len(content.encode("utf-8")), note, content))
        self.purge_config_versions(path)
        return cur.lastrowid

    def config_versions(self, path: str, limit: int = 30) -> list[sqlite3.Row]:
        return self.query_all(
            "SELECT id, ts, username, size, note FROM config_versions "
            "WHERE path = ? ORDER BY id DESC LIMIT ?", (path, int(limit)))

    def config_version(self, vid: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM config_versions WHERE id = ?", (int(vid),))

    def purge_config_versions(self, path: str, keep: int | None = None) -> None:
        keep = self.CONFIG_VERSIONS_KEEP if keep is None else int(keep)
        self.execute(
            "DELETE FROM config_versions WHERE path = ? AND id NOT IN ("
            "SELECT id FROM config_versions WHERE path = ? ORDER BY id DESC LIMIT ?)",
            (path, path, keep))
