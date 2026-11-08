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
    # пара (username, ip): 5 неудач с одного адреса блокируют этот адрес
    for _ in range(5):
        locked, _ = security.check_lockout(db, "admin", 5, 15, ip="10.0.0.1")
        assert not locked
        security.record_login(db, "10.0.0.1", "admin", False)
    locked, remain = security.check_lockout(db, "admin", 5, 15, ip="10.0.0.1")
    assert locked
    assert 0 < remain <= 15 * 60


def test_success_resets_counter(db):
    for _ in range(4):
        security.record_login(db, "10.0.0.1", "admin", False)
    security.record_login(db, "10.0.0.1", "admin", True)
    for _ in range(4):
        security.record_login(db, "10.0.0.1", "admin", False)
    locked, _ = security.check_lockout(db, "admin", 5, 15, ip="10.0.0.1")
    assert not locked
    security.record_login(db, "10.0.0.1", "admin", False)
    locked, _ = security.check_lockout(db, "admin", 5, 15, ip="10.0.0.1")
    assert locked


def test_lockout_expires(db):
    for _ in range(5):
        security.record_login(db, "10.0.0.1", "admin", False)
    # искусственно «старая» последняя неудача: сдвигаем ts назад
    old = utcnow() - 16 * 60
    db.execute("UPDATE login_attempts SET ts = ?", (old - 1,))
    db.execute("UPDATE login_attempts SET ts = ? WHERE id = (SELECT MAX(id) FROM login_attempts)", (old,))
    locked, remain = security.check_lockout(db, "admin", 5, 15, ip="10.0.0.1")
    assert not locked


def test_counter_persistent_across_restart(db, tmp_path):
    """Счётчик хранится в БД — переживает «рестарт панели» (новый коннект к той же БД)."""
    for _ in range(5):
        security.record_login(db, "10.0.0.1", "admin", False)
    db2 = Database(tmp_path / "t.db")  # новое соединение к той же БД после «перезапуска»
    locked, _ = security.check_lockout(db2, "admin", 5, 15, ip="10.0.0.1")
    assert locked
    db2.close()


# --- аудит №3: пара username+ip + глобальный порог на аккаунт -----------------

def test_lockout_pair_isolated_by_ip(db):
    """5 неудач с чужого IP НЕ блокируют доступ легитимному клиенту с другого IP."""
    for _ in range(10):  # даже дважды перебрали порог пары
        security.record_login(db, "203.0.113.9", "admin", False)
    # глобальный порог (5*3=15) ещё не достигнут, пара для 10.0.0.2 пуста
    locked, remain = security.check_lockout(db, "admin", 5, 15, ip="10.0.0.2")
    assert not locked
    assert remain == 0


def test_lockout_global_threshold(db):
    """15 неудач подряд (5*3) с разных IP блокируют аккаунт на lockout_minutes*3."""
    ips = [f"203.0.113.{i}" for i in range(15)]
    for i, ip in enumerate(ips):
        locked, _ = security.check_lockout(db, "admin", 5, 15, ip=ip)
        assert not locked, f"преждевременный lockout на попытке {i + 1}"
        security.record_login(db, ip, "admin", False)
    locked, remain = security.check_lockout(db, "admin", 5, 15, ip="203.0.113.99")
    assert locked
    # глобальное окно 45 мин > обычных 15
    assert 40 * 60 < remain <= 45 * 60


def test_success_clears_only_own_ip(db):
    """Успешный вход чистит неудачи только своего адреса; чужие остаются."""
    for _ in range(5):
        security.record_login(db, "203.0.113.9", "admin", False)
    locked, _ = security.check_lockout(db, "admin", 5, 15, ip="203.0.113.9")
    assert locked
    # легитимный вход с другого адреса
    security.record_login(db, "10.0.0.2", "admin", True)
    failed = security._consecutive_failures(db, "admin", "203.0.113.9")
    assert failed == 5                      # чужие неудачи числятся
    assert security._consecutive_failures(db, "admin", "10.0.0.2") == 0
    # и повторная проверка с легитимного адреса не заперта (пара чиста)
    locked, _ = security.check_lockout(db, "admin", 5, 15, ip="10.0.0.2")
    assert not locked


# --- аудит №10: constant-time CSRF-хелпер --------------------------------------

def test_csrf_matches(db):
    m = security.csrf_matches
    assert m("tok", "tok") is True
    assert m("tok", "other") is False
    assert m(None, "tok") is False          # пустая форма — отказ
    assert m("tok", None) is False           # нет сессии/токена — отказ
    assert m("", "") is False                # оба пустые — отказ (не «совпадение»)
    assert m("t" * 64, "t" * 64) is True


def test_session_invalidated_after_password_change(db):
    token, csrf = security.create_session(db, "admin", 60)
    assert security.get_session(db, token) is not None
    security.set_password(db, "admin", "newpassword1")
    assert security.get_session(db, token) is None  # ТЗ §6.1: серверная инвалидизация


def test_reset_lockout(db):
    for _ in range(5):
        security.record_login(db, "10.0.0.1", "admin", False)
    assert security.reset_lockout(db, "admin") == 5
    locked, _ = security.check_lockout(db, "admin", 5, 15, ip="10.0.0.1")
    assert not locked


def test_audit_records_without_secrets(db):
    db.audit("10.0.0.1", "admin", "start", "vllm.service", "ok", "rc=0")
    db.audit("10.0.0.1", "admin", "login", "panel", "failed", "неверный пароль")
    rows = db.audit_tail(2)
    assert rows[0]["action"] == "login"
    assert "p@ss" not in rows[0]["detail"]
