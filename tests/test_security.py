"""Тесты безопасности: пароли (Argon2), последовательные неудачи/блокировка, аудит (ТЗ §6.1/§6.4)."""

import pytest

from llm_panel.db import Database, utcnow
from llm_panel import security


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def test_password_hash_roundtrip(db):
    security.set_password(db, "admin", "p@ssw0rd1234")
    u = security.get_user(db, "admin")
    assert u["password_hash"] != "p@ssw0rd1234"
    assert u["password_hash"].startswith("$argon2")
    assert security.verify_password(u["password_hash"], "p@ssw0rd1234")
    assert not security.verify_password(u["password_hash"], "wrong")


def test_lockout_after_five_consecutive_failures(db):
    for _ in range(5):
        locked, _ = security.check_lockout(db, "admin", 5, 15)
        assert not locked
        security.record_login(db, "10.0.0.1", "admin", False)
    locked, remain = security.check_lockout(db, "admin", 5, 15)
    assert locked
    assert 0 < remain <= 15 * 60


def test_success_resets_counter(db):
    for _ in range(4):
        security.record_login(db, "10.0.0.1", "admin", False)
    security.record_login(db, "10.0.0.1", "admin", True)
    for _ in range(4):
        security.record_login(db, "10.0.0.1", "admin", False)
    locked, _ = security.check_lockout(db, "admin", 5, 15)
    assert not locked
    security.record_login(db, "10.0.0.1", "admin", False)
    locked, _ = security.check_lockout(db, "admin", 5, 15)
    assert locked


def test_lockout_expires(db):
    for _ in range(5):
        security.record_login(db, "10.0.0.1", "admin", False)
    # искусственно «старая» последняя неудача: сдвигаем ts назад
    old = utcnow() - 16 * 60
    db.execute("UPDATE login_attempts SET ts = ?", (old - 1,))
    db.execute("UPDATE login_attempts SET ts = ? WHERE id = (SELECT MAX(id) FROM login_attempts)", (old,))
    locked, remain = security.check_lockout(db, "admin", 5, 15)
    assert not locked


def test_counter_persistent_across_restart(db, tmp_path):
    """Счётчик хранится в БД — переживает «рестарт панели» (новый коннект к той же БД)."""
    for _ in range(5):
        security.record_login(db, "10.0.0.1", "admin", False)
    db2 = Database(tmp_path / "t.db")  # новое соединение к той же БД после «перезапуска»
    locked, _ = security.check_lockout(db2, "admin", 5, 15)
    assert locked
    db2.close()


def test_session_invalidated_after_password_change(db):
    token, csrf = security.create_session(db, "admin", 60)
    assert security.get_session(db, token) is not None
    security.set_password(db, "admin", "newpassword1")
    assert security.get_session(db, token) is None  # ТЗ §6.1: серверная инвалидизация


def test_reset_lockout(db):
    for _ in range(5):
        security.record_login(db, "10.0.0.1", "admin", False)
    assert security.reset_lockout(db, "admin") == 5
    locked, _ = security.check_lockout(db, "admin", 5, 15)
    assert not locked


def test_audit_records_without_secrets(db):
    db.audit("10.0.0.1", "admin", "start", "vllm.service", "ok", "rc=0")
    db.audit("10.0.0.1", "admin", "login", "panel", "failed", "неверный пароль")
    rows = db.audit_tail(2)
    assert rows[0]["action"] == "login"
    assert "p@ss" not in rows[0]["detail"]
