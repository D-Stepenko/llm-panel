"""Интеграционные тесты приложения: вход, CSRF, аудит, запрет анонимных данных (ТЗ §10 п.1, п.11, п.12)."""

import re

import pytest
import yaml

from llm_panel.app import create_app
from llm_panel.config import load_config


def _make_config(tmp_path):
    svc = {
        "services": [
            {"id": "vllm", "name": "vLLM", "type": "vllm", "unit": "vllm.service",
             "managed": "adopted", "edit_mode": "manage_only", "port": 8000},
        ]
    }
    services_file = tmp_path / "services.yaml"
    services_file.write_text(yaml.safe_dump(svc, allow_unicode=True), encoding="utf-8")

    cfg_data = {
        "panel": {
            "listen_host": "127.0.0.1",
            "listen_port": 8999,
            "trusted_hosts": ["localhost", "127.0.0.1", "testserver"],
            "cookie_secure": False,
            "session_minutes": 60,
            "dev_mode": True,   # тест работает без установленного helper на станции
        },
        "data": {
            "data_dir": str(tmp_path / "data"),
            "db_path": str(tmp_path / "data" / "panel.db"),
            "services_file": str(services_file),
        },
        "systemd": {"helper_path": str(tmp_path / "no-such-helper")},
    }
    (tmp_path / "data").mkdir()
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg_data, allow_unicode=True), encoding="utf-8")
    return cfg_path


@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient

    cfg = load_config(_make_config(tmp_path))
    app = create_app(cfg)
    c = TestClient(app)
    # стартовая запись admin без пароля ('!') — пароль задаётся отдельно
    app.state.panel.db.execute(
        "INSERT OR REPLACE INTO users (username, password_hash, updated_at) VALUES ('admin', '!', 0)"
    )
    return c


def _login_token_html(client) -> str:
    r = client.get("/login")
    assert r.status_code == 200
    m = re.search(r'name="login_token" value="([^"]+)"', r.text)
    assert m, "login_token не найден на странице входа"
    return m.group(1)


def login(client, password):
    return client.post(
        "/login",
        data={"username": "admin", "password": password, "login_token": _login_token_html(client),
              "next": "/"},
        headers={"host": "localhost"},
        follow_redirects=False,
    )


@pytest.fixture
def authed_client(client):
    from llm_panel import security
    security.set_password(client.app.state.panel.db, "admin", "correct-password")
    return client


def test_unauthenticated_dashboard_redirects(client):
    r = client.get("/", headers={"host": "localhost"}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/login")


def test_bad_host_rejected(client):
    r = client.get("/", headers={"host": "evil.example.com"}, follow_redirects=False)
    assert r.status_code == 403


def test_login_rate_limit(authed_client):
    client = authed_client
    for _ in range(5):
        r = login(client, "wrong-password")
        assert r.status_code == 200
        assert "Осталось попыток" in r.text or "Неверный логин" in r.text
    # 6-я попытка — блокировка, даже с верным паролем (ТЗ §10 п.11)
    r = login(client, "correct-password")
    assert "Слишком много неудачных попыток" in r.text


def test_login_success_and_data_access(authed_client):
    client = authed_client
    r = login(client, "correct-password")
    assert r.status_code == 303
    # подготовим снапшот коллектора (в тесте фоновый цикл не запущен)
    from llm_panel.collector import Snapshot
    row = {
        "id": "vllm", "name": "vLLM", "type": "vllm", "unit": "vllm.service",
        "managed": "adopted", "edit_mode": "manage_only", "port": 8000, "bind_host": "127.0.0.1",
        "gpus": [], "state": "stopped", "state_ru": "остановлен", "note": "",
        "system_active": "inactive", "system_sub": "dead", "uptime": "", "uptime_s": None,
        "nrestarts": 0, "autostart": "disabled", "health": "", "health_ok": None,
        "ram_exclusive": False, "stop_signal": "SIGTERM", "notes": "",
    }
    client.app.state.panel.collector.snapshot = Snapshot(
        ts=0, services=[row], gpus=None, host={"ram_used_mb": None, "swap_total_mb": None,
                                                "cpu_percent": None, "disks": []}, warnings=[])
    page = client.get("/", headers={"host": "localhost"})
    assert page.status_code == 200
    assert "vLLM" in page.text           # данные сервисов НЕ доступны без входа (ТЗ §10 п.1)
    assert "llm_panel_session" in client.cookies


def test_action_requires_csrf(authed_client):
    client = authed_client
    assert login(client, "correct-password").status_code == 303
    r = client.post("/services/vllm/start", data={"csrf": "WRONG"}, headers={"host": "localhost"})
    assert r.status_code == 403


def test_action_unknown_service_404(authed_client):
    client = authed_client
    assert login(client, "correct-password").status_code == 303
    r = client.post("/services/nope/stop", data={"csrf": "x"}, headers={"host": "localhost"})
    assert r.status_code == 404


def test_audit_written_for_login_failure(client):
    login(client, "not-my-password")
    rows = client.app.state.panel.db.audit_tail(10)
    assert any(r["action"] == "login" and r["result"] == "failed" for r in rows)
    # секреты не попадают в аудит (ТЗ §6.4)
    assert all("not-my-password" not in r["detail"] for r in rows)
