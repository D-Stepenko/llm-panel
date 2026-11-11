"""Поддержка systemd user-юнитов (v0.1.4): scope, скан, действия, журнал.

Проверяется:
  * registry: парсинг scope (system|user), make_display_def для user-юнитов
    (id = ПОЛНОЕ имя юнита — защита от коллизии с системным id);
  * show_unit: argv содержит --user для scope=user;
  * скан (list_loaded_services): объединение системного и user-менеджера;
  * действия user-ветки: напрямую run_user_action (без helper), аудит;
  * журнал user-юнита: journalctl --user напрямую + маскирование секретов;
  * collector: show_unit вызывается со scope юнита.
"""

import re

import pytest
import yaml

from test_service_display import (
    _csrf,
    _make_config,
    _row,
    _set_snapshot,
)

from llm_panel.app import create_app
from llm_panel.collector import Snapshot
from llm_panel.config import load_config
from llm_panel.registry import make_display_def
from llm_panel.systemd_units import UnitInfo


def test_user_env_runtime_dir(monkeypatch):
    """Для user-вызовов задаётся XDG_RUNTIME_DIR=/run/user/<uid> (из системного
    сервиса шины нет — «No medium found»; фикс станции 2026-10-28)."""
    import llm_panel.systemd_units as su

    captured = {}

    class P:
        returncode = 0
        stdout = ("ActiveState=active\nSubState=running\nResult=success\n"
                  "ExecMainPID=7\nExecMainStartTimestamp=\nNRestarts=0\n"
                  "UnitFileState=enabled\n")
        stderr = ""

    def fake_run(argv, text=None, capture_output=None, timeout=None, env=None):
        captured["argv"] = argv
        captured["env"] = env
        return P()

    monkeypatch.setattr(su.subprocess, "run", fake_run)
    import os as _os
    monkeypatch.setattr(_os, "getuid", lambda: 1000, raising=False)
    su.show_unit("openclaw-gateway.service", scope="user")
    assert captured["env"]["XDG_RUNTIME_DIR"] == "/run/user/1000"
    # системный вызов — окружение не переопределяется
    su.show_unit("vllm.service")
    assert captured["env"] is None


def test_user_env_no_uid_on_windows():
    """На Windows uid нет — окружение не задаётся (dev)."""
    import os
    if not hasattr(os, "getuid"):
        import llm_panel.systemd_units as su
        assert su._user_env() is None


# ----------------------------------------------- registry --------------------

def test_scope_parsing(tmp_path):
    cfg = load_config(_make_config(tmp_path))
    # добавим в services.yaml сервис с scope: user
    svc_file = cfg.services_file
    data = yaml.safe_load(open(svc_file, encoding="utf-8"))
    data["services"].append({"id": "openclaw", "name": "OpenClaw", "type": "generic",
                             "unit": "openclaw-gateway.service", "scope": "user"})
    open(svc_file, "w", encoding="utf-8").write(
        yaml.safe_dump(data, allow_unicode=True))
    app = create_app(cfg)
    svc = app.state.panel.registry.by_id("openclaw")
    assert svc is not None and svc.scope == "user"
    # остальные — system по умолчанию
    assert app.state.panel.registry.by_id("vllm").scope == "system"


def test_scope_invalid_rejected(tmp_path):
    cfg = load_config(_make_config(tmp_path))
    data = yaml.safe_load(open(cfg.services_file, encoding="utf-8"))
    data["services"].append({"id": "bad", "name": "Bad", "unit": "bad.service",
                             "scope": "global"})
    path = tmp_path / "svc2.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    from llm_panel.registry import load_services
    with pytest.raises(Exception):
        load_services(str(path))


def test_make_display_def_user_id_is_full_unit():
    d = make_display_def("openclaw-gateway.service", scope="user")
    assert d.id == "openclaw-gateway.service"   # полное имя — нет коллизии с system id
    assert d.scope == "user"
    d2 = make_display_def("speaches.service")
    assert d2.id == "speaches" and d2.scope == "system"


# ----------------------------------------------- show_unit / скан ------------

def test_show_unit_user_argv(monkeypatch):
    import llm_panel.systemd_units as su

    captured = {}

    class P:
        returncode = 0
        stdout = ("LoadState=loaded\nActiveState=active\nSubState=running\n"
                  "Result=success\nExecMainPID=1531\nExecMainStartTimestamp=\n"
                  "NRestarts=0\nUnitFileState=enabled\n")
        stderr = ""

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return P()

    monkeypatch.setattr(su.subprocess, "run", fake_run)
    info = su.show_unit("openclaw-gateway.service", scope="user")
    assert info.active == "active" and info.pid == 1531
    assert captured["argv"][:3] == ["systemctl", "--user", "show"]
    # system-scope: без --user
    su.show_unit("vllm.service")
    assert "show" in captured["argv"] and "--user" not in captured["argv"]


def test_scan_merges_user_units(monkeypatch):
    import llm_panel.systemd_units as su

    class P:
        def __init__(self, stdout):
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    outputs = {
        "system": ("vllm.service  loaded  active  running  vLLM\n"
                   "cron.service  loaded  active  running  cron\n"),
        "user": ("openclaw-gateway.service  loaded  active  running  OpenClaw\n"
                 "cron.service  loaded  active  running  cron(user)\n"),
    }

    def fake_run(argv, timeout=None, env=None):
        key = "user" if "--user" in argv else "system"
        return P(outputs[key])

    monkeypatch.setattr(su, "_run_list", fake_run)
    out = su.list_loaded_services()
    units = {e["unit"]: e for e in out}
    assert units["vllm.service"].get("scope", "system") == "system"
    assert units["openclaw-gateway.service"]["scope"] == "user"
    # user-юнит, одноимённый системному, не задублирован (остаётся system)
    assert units["cron.service"].get("scope", "system") == "system"


def test_scan_unavailable_when_systemctl_missing(monkeypatch):
    import llm_panel.systemd_units as su
    monkeypatch.setattr(su, "_run_list", lambda argv, timeout: None)
    assert su.list_loaded_services() is None


def test_run_user_action(monkeypatch):
    import llm_panel.systemd_units as su

    captured = {}

    class P:
        returncode = 0
        stdout = ""
        stderr = "Created symlink\n"

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return P()

    monkeypatch.setattr(su.subprocess, "run", fake_run)
    ok, out = su.run_user_action("enable", "openclaw-gateway.service")
    assert ok and "symlink" in out
    assert captured["argv"] == ["systemctl", "--user", "enable",
                                "--no-ask-password", "--", "openclaw-gateway.service"]
    ok2, _ = su.run_user_action("halt", "x.service")     # недопустимое действие
    assert ok2 is False
    ok3, _ = su.run_user_action("start", "../etc/passwd")  # путь не юнит
    assert ok3 is False


def test_run_user_action_failure(monkeypatch):
    import llm_panel.systemd_units as su

    class P:
        returncode = 1
        stdout = ""
        stderr = "Unit not found."

    monkeypatch.setattr(su.subprocess, "run", lambda argv, **kw: P())
    ok, out = su.run_user_action("start", "nope.service")
    assert ok is False and "not found" in out


# ----------------------------------------------- маршруты --------------------

@pytest.fixture
def user_client(tmp_path):
    """Клиент с настроенным openclaw (scope user) в services.yaml."""
    from fastapi.testclient import TestClient
    from llm_panel import security

    cfg = load_config(_make_config(tmp_path))
    svc_file = cfg.services_file
    data = yaml.safe_load(open(svc_file, encoding="utf-8"))
    data["services"].append({
        "id": "openclaw", "name": "OpenClaw Gateway", "type": "generic",
        "unit": "openclaw-gateway.service", "scope": "user",
        "port": 18789, "bind_host": "127.0.0.1",
    })
    open(svc_file, "w", encoding="utf-8").write(
        yaml.safe_dump(data, allow_unicode=True))
    app = create_app(cfg)
    c = TestClient(app)
    app.state.panel.db.execute(
        "INSERT OR REPLACE INTO users (username, password_hash, updated_at) VALUES ('admin', '!', 0)")
    security.set_password(app.state.panel.db, "admin", "correct-password")
    page = c.get("/login", headers={"host": "localhost"})
    token = re.search(r'name="login_token" value="([^"]+)"', page.text).group(1)
    r = c.post("/login", data={"username": "admin", "password": "correct-password",
                               "login_token": token, "next": "/"},
               headers={"host": "localhost"}, follow_redirects=False)
    assert r.status_code == 303
    return c


def test_action_user_scope_no_helper(user_client, monkeypatch):
    """Действие user-юнита идёт напрямую systemctl --user, helper не вызывается."""
    c = user_client
    import llm_panel.routes.services as svc_mod

    calls = []

    def fake_action(action, unit, timeout=90.0):
        calls.append((action, unit))
        return True, "ok"

    monkeypatch.setattr(svc_mod, "run_user_action", fake_action)
    monkeypatch.setattr(svc_mod, "show_unit",
                        lambda unit, timeout=5.0, scope="system": UnitInfo(
                            active="active", sub="running", result="success",
                            pid=1531, since="", nrestarts=0, unit_file_state=""))
    r = c.post("/services/openclaw/restart", data={"csrf": _csrf(c)},
               headers={"host": "localhost"})
    assert r.status_code == 200
    assert calls == [("restart", "openclaw-gateway.service")]
    assert "active/running" in r.text
    state = c.app.state.panel
    tail = state.db.audit_tail(5)
    assert any(a["object"] == "openclaw-gateway.service" and a["result"] == "ok"
               for a in tail)


def test_action_user_scope_failure(user_client, monkeypatch):
    c = user_client
    import llm_panel.routes.services as svc_mod

    monkeypatch.setattr(svc_mod, "run_user_action",
                        lambda action, unit, timeout=90.0: (False, "Unit not found."))
    r = c.post("/services/openclaw/start", data={"csrf": _csrf(c)},
               headers={"host": "localhost"})
    assert r.status_code == 200
    assert "Unit not found" in r.text
    state = c.app.state.panel
    tail = state.db.audit_tail(5)
    assert any(a["result"] == "failed" for a in tail)


# -------------------------------------- reset-failed / daemon-reload (v0.3.18) --

def test_user_reset_failed(user_client, monkeypatch):
    """reset-failed для user-юнита — тот же прямой путь systemctl --user, без helper."""
    c = user_client
    import llm_panel.routes.services as svc_mod

    calls = []

    def fake_action(action, unit, timeout=90.0):
        calls.append((action, unit))
        return True, "ok"

    monkeypatch.setattr(svc_mod, "run_user_action", fake_action)
    monkeypatch.setattr(svc_mod, "show_unit",
                        lambda unit, timeout=5.0, scope="system": UnitInfo(
                            active="failed", sub="failed", result="exit-code",
                            pid=0, since="", nrestarts=0, unit_file_state=""))
    r = c.post("/services/openclaw/reset-failed", data={"csrf": _csrf(c)},
               headers={"host": "localhost"})
    assert r.status_code == 200
    assert calls == [("reset-failed", "openclaw-gateway.service")]
    assert "failed/failed" in r.text
    state = c.app.state.panel
    tail = state.db.audit_tail(5)
    assert any(a["action"] == "reset-failed:user" and a["result"] == "ok"
               for a in tail)


def test_user_daemon_reload_no_helper(user_client, monkeypatch):
    """daemon-reload для user-юнита — напрямую systemctl --user daemon-reload."""
    c = user_client
    import llm_panel.routes.services as svc_mod

    called = []

    def fake_reload(timeout=60.0):
        called.append(True)
        return True, ""

    monkeypatch.setattr(svc_mod, "daemon_reload_user", fake_reload)
    monkeypatch.setattr(svc_mod, "show_unit",
                        lambda unit, timeout=5.0, scope="system": UnitInfo(
                            active="active", sub="running", result="success",
                            pid=1, since="", nrestarts=0, unit_file_state=""))
    r = c.post("/services/openclaw/daemon-reload", data={"csrf": _csrf(c)},
               headers={"host": "localhost"})
    assert r.status_code == 200
    assert called == [True]
    assert "daemon-reload" in r.text
    state = c.app.state.panel
    tail = state.db.audit_tail(5)
    assert any(a["action"] == "daemon-reload:user" and a["result"] == "ok"
               for a in tail)


def test_user_daemon_reload_failure(user_client, monkeypatch):
    c = user_client
    import llm_panel.routes.services as svc_mod
    monkeypatch.setattr(svc_mod, "daemon_reload_user",
                        lambda timeout=60.0: (False, "Failed to connect to bus."))
    r = c.post("/services/openclaw/daemon-reload", data={"csrf": _csrf(c)},
               headers={"host": "localhost"})
    assert r.status_code == 200
    assert "Failed to connect to bus" in r.text
    tail = c.app.state.panel.db.audit_tail(5)
    assert any(a["action"] == "daemon-reload:user" and a["result"] == "failed"
               for a in tail)


# -------------------------------------- unit-config, user-юниты (v0.3.19) ----

def test_user_unit_config_read(user_client, monkeypatch):
    c = user_client
    import llm_panel.routes.services as svc_mod

    monkeypatch.setattr(svc_mod, "unit_cat_user",
                        lambda unit: (True, "[Unit]\nDescription=user unit\n"))
    page = c.get("/services/openclaw/unit-config", headers={"host": "localhost"})
    assert page.status_code == 200
    assert "Description=user unit" in page.text
    assert 'action="/services/openclaw/unit-config/save"' in page.text
    assert 'action="/services/openclaw/daemon-reload"' in page.text


def test_user_unit_config_save(user_client, monkeypatch):
    c = user_client
    import llm_panel.routes.services as svc_mod

    captured = {}

    def fake_write(unit, content):
        captured["unit"], captured["content"] = unit, content
        return True, "ok (backup)"

    def fake_cat(unit):
        return True, captured.get("content", "")

    monkeypatch.setattr(svc_mod, "unit_write_user", fake_write)
    monkeypatch.setattr(svc_mod, "unit_cat_user", fake_cat)
    r = c.post("/services/openclaw/unit-config/save",
               data={"csrf": _csrf(c), "content": "[Unit]\nDescription=new\n"},
               headers={"host": "localhost"})
    assert r.status_code == 200
    assert captured == {"unit": "openclaw-gateway.service",
                        "content": "[Unit]\nDescription=new\n"}
    assert "Saved" in r.text or "Сохранено" in r.text
    tail = c.app.state.panel.db.audit_tail(5)
    assert any(a["action"] == "unit-write:user" and a["result"] == "ok"
               for a in tail)


def test_unit_write_user_roundtrip(tmp_path, monkeypatch):
    """Файловый roundtrip unit_write_user/unit_cat_user: backup, запрет создания."""
    import llm_panel.systemd_units as su

    base = tmp_path / "user-units"
    base.mkdir()
    (base / "openclaw-gateway.service").write_text(
        "[Unit]\nDescription=a\n\n[Service]\nExecStart=/bin/x\n", encoding="utf-8")
    monkeypatch.setattr(su, "_user_unit_base", lambda: base)

    ok, msg = su.unit_write_user(
        "openclaw-gateway.service",
        "[Unit]\nDescription=b\n\n[Service]\nExecStart=/bin/y\n")
    assert ok, msg
    assert "/bin/y" in (base / "openclaw-gateway.service").read_text(encoding="utf-8")
    backups = list((base / ".panel-backups").glob("openclaw-gateway.service.*.bak"))
    assert len(backups) == 1
    assert "/bin/x" in backups[0].read_text(encoding="utf-8")

    ok3, text = su.unit_cat_user("openclaw-gateway.service")
    assert ok3 and "/bin/y" in text

    ok4, _ = su.unit_write_user("new-unit.service", "[Unit]\nDescription=n\n")
    assert not ok4                       # создание НОВЫХ юнитов запрещено
    assert not (base / "new-unit.service").exists()

    ok5, _ = su.unit_write_user("openclaw-gateway.service", "no sections")
    assert not ok5                       # не похож на systemd-юнит

    ok6, _ = su.unit_write_user("../escape.service", "[Unit]\n")
    assert not ok6                       # путь вне каталога юнитов


def test_user_unit_config_csrf(user_client):
    c = user_client
    r = c.post("/services/openclaw/unit-config/save",
               data={"csrf": "WRONG", "content": "[Unit]\n"},
               headers={"host": "localhost"})
    assert r.status_code == 403


def test_logs_user_scope(user_client, monkeypatch):
    """Журнал user-юнита: прямой journalctl --user + маскирование секретов."""
    c = user_client
    import llm_panel.routes.logs as logs_mod

    def fake_journal(unit, lines):
        assert unit == "openclaw-gateway.service"
        return 0, "ok hf_" + "a" * 25 + " token"

    monkeypatch.setattr(logs_mod, "journal_user_text", fake_journal)
    page = c.get("/services/openclaw/logs", headers={"host": "localhost"})
    assert page.status_code == 200
    assert "hf_" + "a" * 25 not in page.text      # замаскировано
    assert "ok" in page.text


def test_logs_user_scope_error(user_client, monkeypatch):
    c = user_client
    import llm_panel.routes.logs as logs_mod
    monkeypatch.setattr(logs_mod, "journal_user_text",
                        lambda unit, lines: (1, "No journal files were found."))
    page = c.get("/services/openclaw/logs", headers={"host": "localhost"})
    assert page.status_code == 200
    assert "No journal files were found" in page.text


def test_logs_download_user_scope(user_client, monkeypatch):
    c = user_client
    import llm_panel.routes.logs as logs_mod
    monkeypatch.setattr(logs_mod, "journal_user_text",
                        lambda unit, lines: (0, "line1\nline2\n"))
    r = c.get("/services/openclaw/logs/download", headers={"host": "localhost"})
    assert r.status_code == 200
    assert "line1" in r.text


def test_card_shows_user_marker(user_client):
    """Карточка user-юнита помечена «user-юнит»/«user unit» в сабтайтле."""
    c = user_client
    state = c.app.state.panel
    state.collector.snapshot = Snapshot(
        ts=0, services=[
            dict(_row("vllm")),
            dict(_row("openclaw-gateway.service"), name="OpenClaw Gateway", scope="user"),
        ], gpus=None,
        host={"ram_used_mb": None, "swap_total_mb": None, "cpu_percent": None, "disks": []},
        warnings=[])
    page = c.get("/", headers={"host": "localhost"})
    assert 'id="svc-openclaw-gateway.service"' in page.text
    # маркер user-юнита (en-дефолт)
    assert "user unit" in page.text


def test_scan_fragment_user_prefix(user_client, monkeypatch):
    """Найденный user-юнит кодируется value="user:<unit>" и бейджем."""
    c = user_client
    import llm_panel.routes.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "list_loaded_services", lambda: [
        {"unit": "my-tts.service", "active": "active", "sub": "running",
         "desc": "TTS", "scope": "user"},
        {"unit": "other.service", "active": "active", "sub": "running",
         "desc": "Other"},
    ])
    page = c.get("/config/services-scan", headers={"host": "localhost"})
    assert 'value="user:my-tts.service"' in page.text
    assert 'value="other.service"' in page.text
    assert "user unit" in page.text


def test_save_user_extras_roundtrip(user_client):
    """Сохранение user-extra и чтение настроек: value с префиксом user:."""
    c = user_client
    r = c.post("/config/services-display",
               data={"visible": ["vllm", "llama", "llm-panel", "openclaw"],
                     "extra_units": ["user:my-tts.service", "other.service"],
                     "csrf": _csrf(c)},
               headers={"host": "localhost"}, follow_redirects=False)
    assert r.status_code == 303
    state = c.app.state.panel
    assert state.db.get_setting("extra_units") == '["user:my-tts.service", "other.service"]'
    d = state.extra_defs["my-tts.service"]
    assert d.id == "my-tts.service" and d.scope == "user"
    d2 = state.extra_defs["other.service"]
    assert d2.id == "other" and d2.scope == "system"
    # карточки в реестре снапшота появляются (extra_defs переданы коллектору)
    assert state.collector.extra_defs is not None


# ----------------------------------------------- collector -------------------

def test_collector_passes_scope(tmp_path, monkeypatch):
    """show_unit из коллектора вызывается со scope конкретного юнита."""
    import llm_panel.collector as col_mod

    cfg = load_config(_make_config(tmp_path))
    svc_file = cfg.services_file
    data = yaml.safe_load(open(svc_file, encoding="utf-8"))
    data["services"].append({"id": "openclaw", "name": "OpenClaw", "type": "generic",
                             "unit": "openclaw-gateway.service", "scope": "user"})
    open(svc_file, "w", encoding="utf-8").write(
        yaml.safe_dump(data, allow_unicode=True))
    app = create_app(cfg)
    state = app.state.panel

    seen = {}

    def fake_show(unit, timeout=5.0, scope="system"):
        seen[unit] = scope
        return None

    monkeypatch.setattr(col_mod, "show_unit", fake_show)
    import asyncio
    snap = asyncio.run(state.collector._collect())
    assert seen.get("vllm.service") == "system"
    assert seen.get("openclaw-gateway.service") == "user"
    row = next(r for r in snap.services if r["unit"] == "openclaw-gateway.service")
    assert row["scope"] == "user"


# ------------------------------------------ show: LoadState gate -------------

def test_info_from_show_not_found():
    """Регрессия (баг 2026-10-28): systemctl show отвечает rc=0 и непустым
    ActiveState (inactive) даже для НЕсуществующего юнита. Юнит определяется
    по LoadState: not-found -> None, иначе добавлялись любые имена .service."""
    from llm_panel.systemd_units import _info_from_show

    out = ("LoadState=not-found\nActiveState=inactive\nSubState=dead\n"
           "Result=success\nExecMainPID=0\nExecMainStartTimestamp=\n"
           "ExecMainExitTimestamp=\nNRestarts=0\nUnitFileState=\n")
    assert _info_from_show(out) is None


def test_info_from_show_loaded():
    from llm_panel.systemd_units import _info_from_show

    out = ("LoadState=loaded\nActiveState=active\nSubState=running\n"
           "Result=success\nExecMainPID=1234\nExecMainStartTimestamp=\n"
           "ExecMainExitTimestamp=\nNRestarts=2\nUnitFileState=enabled\n")
    info = _info_from_show(out)
    assert info is not None
    assert info.active == "active"
    assert info.nrestarts == 2
    assert info.unit_file_state == "enabled"
    assert info.load_state == "loaded"


def test_info_from_show_found_not_loaded():
    """Юнит-файл есть, но юнит ни разу не стартовал (LoadState=found)."""
    from llm_panel.systemd_units import _info_from_show

    out = ("LoadState=found\nActiveState=inactive\nSubState=dead\n"
           "Result=success\nExecMainPID=0\nExecMainStartTimestamp=\n"
           "ExecMainExitTimestamp=\nNRestarts=0\nUnitFileState=disabled\n")
    info = _info_from_show(out)
    assert info is not None
    assert info.load_state == "found"
    assert info.active == "inactive"
