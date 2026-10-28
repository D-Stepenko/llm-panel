"""Настройка отображения сервисов + карточка самой панели (v0.1.3).

Проверяется:
  * встроенная карточка llm-panel (systemd.panel_unit) в реестре, отключаемая;
  * сохранение видимости (hidden_ids) и найденных юнитов (extra_units);
  * фильтрация скрытых карточек на GET / (и в SSE — через ту же _ordered_services);
  * «Поиск сервисов»: фрагмент со найденными юнитами, недоступность systemd;
  * действия для найденных юнитов работают через тот же роут (/services/<id>/...).
"""

import asyncio
import json
import re
import sys
import pathlib

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from llm_panel.app import create_app
from llm_panel.collector import Snapshot
from llm_panel.config import load_config


def _make_config(tmp_path, panel_unit="llm-panel.service"):
    svc = {
        "services": [
            {"id": "vllm", "name": "vLLM", "type": "vllm", "unit": "vllm.service",
             "managed": "adopted", "edit_mode": "manage_only", "port": 8000},
            {"id": "llama", "name": "llama.cpp", "type": "llamacpp",
             "unit": "llama-qwen38_gguf.service", "managed": "adopted",
             "edit_mode": "manage_only", "port": 8002},
        ]
    }
    services_file = tmp_path / "services.yaml"
    services_file.write_text(yaml.safe_dump(svc, allow_unicode=True), encoding="utf-8")
    cfg_data = {
        "panel": {
            "listen_host": "127.0.0.1", "listen_port": 8999,
            "trusted_hosts": ["localhost", "127.0.0.1", "testserver"],
            "cookie_secure": False, "session_minutes": 60, "dev_mode": True,
        },
        "data": {
            "data_dir": str(tmp_path / "data"),
            "db_path": str(tmp_path / "data" / "panel.db"),
            "services_file": str(services_file),
        },
        "systemd": {"helper_path": str(tmp_path / "no-such-helper"),
                    "panel_unit": panel_unit},
    }
    (tmp_path / "data").mkdir(exist_ok=True)
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg_data, allow_unicode=True), encoding="utf-8")
    return cfg_path


@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient

    cfg = load_config(_make_config(tmp_path))
    app = create_app(cfg)
    c = TestClient(app)
    app.state.panel.db.execute(
        "INSERT OR REPLACE INTO users (username, password_hash, updated_at) VALUES ('admin', '!', 0)"
    )
    return c


@pytest.fixture
def authed_client(client):
    from llm_panel import security

    security.set_password(client.app.state.panel.db, "admin", "correct-password")
    c = client
    page = c.get("/login", headers={"host": "localhost"})
    token = re.search(r'name="login_token" value="([^"]+)"', page.text).group(1)
    r = c.post("/login", data={"username": "admin", "password": "correct-password",
                               "login_token": token, "next": "/"},
               headers={"host": "localhost"}, follow_redirects=False)
    assert r.status_code == 303
    return c


def _row(sid):
    return {
        "id": sid, "name": sid.upper(), "type": "generic", "unit": f"{sid}.service",
        "managed": "adopted", "edit_mode": "manage_only", "port": None,
        "bind_host": None, "gpus": [], "gpu_info": [], "state": "stopped",
        "state_ru": "остановлен", "note": "", "system_active": "inactive",
        "system_sub": "dead", "system_result": "success", "uptime_s": 60,
        "nrestarts": 0, "autostart": "disabled", "health": "", "health_ok": None,
        "ram_exclusive": False, "stop_signal": "SIGTERM", "notes": "",
    }


def _set_snapshot(c, ids):
    c.app.state.panel.collector.snapshot = Snapshot(
        ts=0, services=[_row(i) for i in ids], gpus=None,
        host={"ram_used_mb": None, "swap_total_mb": None, "cpu_percent": None, "disks": []},
        warnings=[],
    )


def _csrf(c):
    page = c.get("/config", headers={"host": "localhost"})
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


# ----------------------------------------------- карточка самой панели -------

def test_panel_unit_builtin_card(authed_client):
    """systemd.panel_unit по умолчанию даёт карточку llm-panel наравне с другими."""
    state = authed_client.app.state.panel
    assert state.registry.by_id("llm-panel") is not None
    assert state.registry.by_unit("llm-panel.service") is not None
    _set_snapshot(authed_client, ["vllm", "llama", "llm-panel"])
    page = authed_client.get("/", headers={"host": "localhost"})
    assert 'id="svc-llm-panel"' in page.text
    assert "llm-panel.service" in page.text


def test_panel_unit_can_be_disabled(tmp_path):
    """Пустая строка panel_unit — карточки панели нет."""
    from fastapi.testclient import TestClient

    cfg = load_config(_make_config(tmp_path, panel_unit=""))
    app = create_app(cfg)
    assert app.state.panel.registry.by_id("llm-panel") is None


def test_panel_unit_invalid_rejected(tmp_path):
    """Невалидное имя юнита в panel_unit — понятная ошибка конфигурации."""
    with pytest.raises(Exception):
        load_config(_make_config(tmp_path, panel_unit="bad name!"))


def test_panel_unit_skipped_if_in_yaml(tmp_path):
    """Если services.yaml уже описывает юнит панели — дубль не создаётся."""
    from fastapi.testclient import TestClient

    p = _make_config(tmp_path)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    svc_file = pathlib.Path(data["data"]["services_file"])
    svc_data = yaml.safe_load(svc_file.read_text(encoding="utf-8"))
    svc_data["services"].append({"id": "panel-custom", "name": "Panel",
                                 "unit": "llm-panel.service"})
    svc_file.write_text(yaml.safe_dump(svc_data, allow_unicode=True), encoding="utf-8")
    app = create_app(load_config(p))
    ids = [s.id for s in app.state.panel.registry]
    assert "llm-panel" not in ids and "panel-custom" in ids


# ----------------------------------------------- настройка отображения -------

def test_display_save_and_hidden_from_dashboard(authed_client):
    """Скрытие сервиса: сохраняется в settings, карточка не рендерится на /."""
    c = authed_client
    _set_snapshot(c, ["vllm", "llama", "llm-panel"])
    r = c.post("/config/services-display",
               data={"visible": ["vllm", "llm-panel"], "csrf": _csrf(c)},
               headers={"host": "localhost"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/config?saved=1"
    state = c.app.state.panel
    assert state.hidden_ids == ["llama"]
    assert json.loads(state.db.get_setting("hidden_ids")) == ["llama"]
    page = c.get("/", headers={"host": "localhost"})
    assert 'id="svc-llama"' not in page.text
    assert 'id="svc-vllm"' in page.text and 'id="svc-llm-panel"' in page.text
    # запись в аудите
    tail = state.db.audit_tail(10)
    assert any(r_["action"] == "config" and r_["object"] == "services_display" for r_ in tail)


def test_display_extra_units_shown_and_persisted(authed_client):
    """Найденный юнит: сохраняется в settings, показывается простой карточкой."""
    c = authed_client
    _set_snapshot(c, ["vllm", "llama", "llm-panel", "speaches"])
    r = c.post("/config/services-display",
               data={"visible": ["vllm", "llama", "llm-panel"],
                     "extra_units": ["speaches.service"], "csrf": _csrf(c)},
               headers={"host": "localhost"}, follow_redirects=False)
    assert r.status_code == 303
    state = c.app.state.panel
    assert json.loads(state.db.get_setting("extra_units")) == ["speaches.service"]
    page = c.get("/", headers={"host": "localhost"})
    assert 'id="svc-speaches"' in page.text
    # Config Panel: сохранённый юнит остаётся отмеченным
    cfg_page = c.get("/config", headers={"host": "localhost"})
    m = re.search(r'name="extra_units" value="speaches.service"([^>]*)', cfg_page.text)
    assert m and "checked" in m.group(1)


def test_display_survives_reload(authed_client):
    """Настройка читается из settings при новой загрузке (перезапуск панели)."""
    c = authed_client
    c.post("/config/services-display",
           data={"visible": ["vllm"], "extra_units": ["speaches.service"],
                 "csrf": _csrf(c)},
           headers={"host": "localhost"}, follow_redirects=False)
    state = c.app.state.panel
    # имитация перечитывания настроек при рестарте: заново применить из БД
    state.hidden_ids = json.loads(state.db.get_setting("hidden_ids"))
    units = json.loads(state.db.get_setting("extra_units"))
    from llm_panel.registry import make_display_def
    state.extra_defs = {u: make_display_def(u) for u in units}
    assert state.hidden_ids == ["llama", "llm-panel"]
    assert set(state.extra_defs) == {"speaches.service"}


def test_display_invalid_units_dropped(authed_client):
    """Невалидные имена юнитов и дубли настроенных отбрасываются."""
    c = authed_client
    r = c.post("/config/services-display",
               data={"visible": ["vllm", "llama", "llm-panel"],
                     "extra_units": ["bad name", "../etc/passwd", "vllm.service",
                                     "speaches.service", "speaches.service"],
                     "csrf": _csrf(c)},
               headers={"host": "localhost"}, follow_redirects=False)
    assert r.status_code == 303
    state = c.app.state.panel
    assert json.loads(state.db.get_setting("extra_units")) == ["speaches.service"]
    assert set(state.extra_defs) == {"speaches.service"}


def test_display_all_hidden(authed_client):
    """Можно скрыть все сервисы (пустой visible) — плашка «нет сервисов»."""
    c = authed_client
    _set_snapshot(c, ["vllm", "llama", "llm-panel"])
    r = c.post("/config/services-display", data={"csrf": _csrf(c)},
               headers={"host": "localhost"}, follow_redirects=False)
    assert r.status_code == 303
    state = c.app.state.panel
    assert state.hidden_ids == ["llama", "llm-panel", "vllm"]
    page = c.get("/", headers={"host": "localhost"})
    assert "service-card" not in page.text
    assert page.text.find("notice info") != -1   # информационная плашка вместо карточек


def test_display_csrf_required(authed_client):
    c = authed_client
    r = c.post("/config/services-display", data={"visible": ["vllm"]},
               headers={"host": "localhost"}, follow_redirects=False)
    assert r.status_code == 403


# ----------------------------------------------- самоперезапуск панели (0.1.6) --

def test_panel_self_restart_uses_no_block(authed_client, monkeypatch):
    """restart/stop для юнита ПАНЕЛИ идут с --no-block: ответ браузеру не рвётся."""
    c = authed_client
    import llm_panel.routes.services as svc_mod

    captured = {}

    def fake_call(helper_path, action, unit, extra=(), timeout=60.0):
        captured["extra"] = extra
        from llm_panel.helper_client import HelperResult
        return HelperResult(ok=True, stdout="", stderr="", code=0)

    monkeypatch.setattr(svc_mod, "call_helper", fake_call)
    monkeypatch.setattr(svc_mod, "show_unit",
                        lambda unit, timeout=5.0, scope="system": None)
    r = c.post("/services/llm-panel/restart", data={"csrf": _csrf(c)},
               headers={"host": "localhost"})
    assert r.status_code == 200
    assert captured["extra"] == ("--no-block",)
    assert "page will reconnect" in r.text or "restarting" in r.text
    # restart обычного сервиса — БЕЗ флага
    r2 = c.post("/services/vllm/restart", data={"csrf": _csrf(c)},
                headers={"host": "localhost"})
    assert r2.status_code == 200
    assert captured["extra"] == ()


def test_panel_stop_uses_no_block_too(authed_client, monkeypatch):
    c = authed_client
    import llm_panel.routes.services as svc_mod

    captured = {}

    def fake_call(helper_path, action, unit, extra=(), timeout=60.0):
        captured["extra"] = extra
        from llm_panel.helper_client import HelperResult
        return HelperResult(ok=True, stdout="", stderr="", code=0)

    monkeypatch.setattr(svc_mod, "call_helper", fake_call)
    monkeypatch.setattr(svc_mod, "show_unit",
                        lambda unit, timeout=5.0, scope="system": None)
    r = c.post("/services/llm-panel/stop", data={"csrf": _csrf(c)},
               headers={"host": "localhost"})
    assert r.status_code == 200
    assert captured["extra"] == ("--no-block",)


def test_panel_denied_shows_allowlist_hint(authed_client, monkeypatch):
    """Отказ helper'а для юнита панели — с подсказкой про allowlist.json."""
    c = authed_client
    import llm_panel.routes.services as svc_mod
    from llm_panel.helper_client import HelperError

    def fake_call(helper_path, action, unit, extra=(), timeout=60.0):
        raise HelperError(kind="denied", message="отказано: юнит или операция вне allowlist")

    monkeypatch.setattr(svc_mod, "call_helper", fake_call)
    r = c.post("/services/llm-panel/restart", data={"csrf": _csrf(c)},
               headers={"host": "localhost"})
    assert r.status_code == 200
    assert "allowlist.json" in r.text
    # отказ для ОБЫЧНОГО сервиса — без подсказки
    r2 = c.post("/services/vllm/restart", data={"csrf": _csrf(c)},
                headers={"host": "localhost"})
    assert r2.status_code == 200
    assert "allowlist.json" not in r2.text


# ----------------------------------------------- поиск сервисов --------------

def test_scan_lists_found_units(authed_client, monkeypatch):
    """«Поиск сервисов»: найденные юниты в фрагменте, настроенные исключены."""
    c = authed_client
    import llm_panel.routes.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "list_loaded_services", lambda: [
        {"unit": "vllm.service", "active": "active", "sub": "running", "desc": "vLLM"},
        {"unit": "speaches.service", "active": "active", "sub": "running", "desc": "Speaches"},
        {"unit": "cron.service", "active": "active", "sub": "running", "desc": "cron"},
    ])
    page = c.get("/config/services-scan", headers={"host": "localhost"})
    assert page.status_code == 200
    assert 'value="speaches.service"' in page.text
    assert 'value="cron.service"' in page.text
    assert 'value="vllm.service"' not in page.text   # настроен — в общем списке, не здесь


def test_scan_systemd_unavailable(authed_client, monkeypatch):
    c = authed_client
    import llm_panel.routes.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "list_loaded_services", lambda: None)
    page = c.get("/config/services-scan", headers={"host": "localhost"})
    assert page.status_code == 200
    assert "systemd" in page.text


def test_scan_keeps_saved_units(authed_client, monkeypatch):
    """Сохранённые юниты остаются отмеченными после повторного поиска."""
    c = authed_client
    c.post("/config/services-display",
           data={"visible": ["vllm", "llama", "llm-panel"],
                 "extra_units": ["speaches.service"], "csrf": _csrf(c)},
           headers={"host": "localhost"}, follow_redirects=False)
    import llm_panel.routes.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "list_loaded_services", lambda: [
        {"unit": "speaches.service", "active": "active", "sub": "running", "desc": ""},
        {"unit": "cron.service", "active": "active", "sub": "running", "desc": ""},
    ])
    page = c.get("/config/services-scan", headers={"host": "localhost"})
    # speaches.service — один раз, отмечен
    assert page.text.count('name="extra_units" value="speaches.service"') == 1
    m = re.search(r'name="extra_units" value="speaches.service"([^>]*)', page.text)
    assert "checked" in m.group(1)


# ----------------------------------------------- действия и порядок ----------

def test_action_for_extra_unit(authed_client):
    """Действие для найденного юнита проходит тот же роут (helper откажет — но не 404)."""
    c = authed_client
    c.post("/config/services-display",
           data={"visible": ["vllm", "llama", "llm-panel"],
                 "extra_units": ["speaches.service"], "csrf": _csrf(c)},
           headers={"host": "localhost"}, follow_redirects=False)
    r = c.post("/services/speaches/start", data={"csrf": _csrf(c)},
               headers={"host": "localhost"})
    assert r.status_code == 200
    # dev-режим: helper отсутствует -> понятное сообщение, а не «сервис не найден»
    assert "svc_not_found" not in r.text


def test_order_with_extra_unit(authed_client):
    c = authed_client
    c.post("/config/services-display",
           data={"visible": ["vllm", "llama", "llm-panel"],
                 "extra_units": ["speaches.service"], "csrf": _csrf(c)},
           headers={"host": "localhost"}, follow_redirects=False)
    r = c.post("/ui/order", data={"order": "speaches,vllm", "csrf": _csrf(c)},
               headers={"host": "localhost"})
    assert r.status_code == 200
    assert c.app.state.panel.services_order == ["speaches", "vllm"]


# ----------------------------------------------- collector -------------------

def test_collector_includes_extras(tmp_path):
    """Коллектор строит строки и для надстроенных юнитов (без порта/GPU)."""
    from fastapi.testclient import TestClient

    cfg = load_config(_make_config(tmp_path))
    app = create_app(cfg)
    state = app.state.panel
    from llm_panel.registry import make_display_def
    state.collector.extra_defs = (make_display_def("speaches.service"),)

    snap = asyncio.run(state.collector._collect())
    ids = [r["id"] for r in snap.services]
    assert "speaches" in ids and "vllm" in ids and "llm-panel" in ids
    row = next(r for r in snap.services if r["id"] == "speaches")
    assert row["port"] is None and row["gpus"] == []
