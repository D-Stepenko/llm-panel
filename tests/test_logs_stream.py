"""SSE-поток логов (v0.2.1): события log/end/error уходят корректными строками.

Регрессия: _event была async def, а yield отдавал корутину без await —
ASGI-слой обрывал соединение, EventSource давал голую «Ошибка потока:».
"""

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import re

from llm_panel.app import create_app
from llm_panel.config import load_config
from llm_panel.i18n import translate

from test_service_display import _make_config


class _Reader:
    """Имитация stdout/stderr файла: readline()/read()."""

    def __init__(self, items):
        self._items = list(items)

    def readline(self):
        return self._items.pop(0) if self._items else ""

    def read(self):
        text = "\n".join(self._items)
        self._items = []
        return text


class FakeProc:
    def __init__(self, lines, stderr=""):
        self.stdout = _Reader([ln + "\n" for ln in lines])
        self.stderr = _Reader([stderr] if stderr else [])

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


def _client(tmp_path):
    from fastapi.testclient import TestClient

    cfg = load_config(_make_config(tmp_path))
    app = create_app(cfg)
    from llm_panel import security

    security.set_password(app.state.panel.db, "admin", "correct-password")
    c = TestClient(app)
    r = c.get("/login", headers={"host": "localhost"})
    m = re.search(r'name="login_token" value="([^"]+)"', r.text)
    c.post("/login", data={"username": "admin", "password": "correct-password",
                           "login_token": m.group(1), "next": "/"},
           headers={"host": "localhost"})
    return c


def test_stream_emits_log_events(tmp_path, monkeypatch):
    import llm_panel.routes.logs as logs_mod

    c = _client(tmp_path)
    monkeypatch.setattr(logs_mod, "spawn_helper_follow",
                        lambda helper, unit, n: FakeProc(
                            ["first log line", "second log line"]))
    r = c.get("/services/vllm/logs/stream?lines=50", headers={"host": "localhost"})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("content-type", "")
    # события — СТРОКИ SSE, а не repr корутины
    assert "event: log\ndata: first log line\n" in r.text
    assert "event: log\ndata: second log line\n" in r.text
    assert "event: end" in r.text
    assert "<coroutine" not in r.text


def test_stream_spawn_error_visible(tmp_path, monkeypatch):
    import llm_panel.routes.logs as logs_mod

    c = _client(tmp_path)

    def boom(helper, unit, n):
        raise RuntimeError("helper missing")

    monkeypatch.setattr(logs_mod, "spawn_helper_follow", boom)
    r = c.get("/services/vllm/logs/stream?lines=50", headers={"host": "localhost"})
    assert r.status_code == 200
    assert "event: error" in r.text
    assert "helper missing" in r.text


def test_stream_helper_exit_before_output(tmp_path, monkeypatch):
    """helper сразу завершился: клиент получает event: error с причиной."""
    import llm_panel.routes.logs as logs_mod

    c = _client(tmp_path)
    monkeypatch.setattr(logs_mod, "spawn_helper_follow",
                        lambda helper, unit, n: FakeProc([], stderr="unit not allowed"))
    r = c.get("/services/vllm/logs/stream?lines=50", headers={"host": "localhost"})
    assert "event: error" in r.text
    assert "unit not allowed" in r.text


def test_logs_page_has_noconn_fallback(tmp_path):
    """data-noconn на #log — осмысленный текст вместо голой «Ошибка потока:»."""
    c = _client(tmp_path)
    page = c.get("/services/vllm/logs", headers={"host": "localhost"})
    assert page.status_code == 200
    assert "data-noconn=" in page.text
    assert translate("en", "logs_stream_noconn") in page.text
