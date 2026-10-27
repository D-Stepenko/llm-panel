"""Тесты доработок GUI: порядок карточек (Порядок), авто-CSRF в шаблонах, имена GPU в карточках."""

import re

import pytest
import yaml

from llm_panel.app import create_app
from llm_panel.collector import Snapshot
from llm_panel.config import load_config


def _make_config(tmp_path, svc_ids=("vllm", "llama"), gpu_uuid="GPU-9f764cdf-08bd-48a9-c219-a8660a71c1d6"):
    services = [
        {"id": sid, "name": sid.upper(), "type": "vllm", "unit": f"{sid}.service",
         "managed": "adopted", "edit_mode": "manage_only", "port": 8000}
        for sid in svc_ids
    ]
    services[0]["gpus"] = [gpu_uuid]
    services_file = tmp_path / "services.yaml"
    services_file.write_text(yaml.safe_dump({"services": services}, allow_unicode=True),
                             encoding="utf-8")
    cfg_data = {
        "panel": {
            "listen_host": "127.0.0.1",
            "listen_port": 8999,
            "trusted_hosts": ["localhost", "127.0.0.1", "testserver"],
            "cookie_secure": False,
            "session_minutes": 60,
            "dev_mode": True,
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


def _row(sid, gpu_info=(), autostart="disabled"):
    return {
        "id": sid, "name": sid.upper(), "type": "vllm", "unit": f"{sid}.service",
        "managed": "adopted", "edit_mode": "manage_only", "port": 8000,
        "bind_host": "127.0.0.1", "gpus": [g["uuid"] for g in gpu_info],
        "gpu_info": list(gpu_info), "state": "stopped", "state_ru": "остановлен",
        "note": "", "system_active": "inactive", "system_sub": "dead",
        "system_result": "success", "uptime": "1м", "uptime_s": 60, "nrestarts": 0,
        "autostart": autostart, "health": "", "health_ok": None,
        "ram_exclusive": False, "stop_signal": "SIGTERM", "notes": "",
    }


@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient

    cfg = load_config(_make_config(tmp_path))
    app = create_app(cfg)
    c = TestClient(app)
    app.state.panel.db.execute(
        "INSERT OR REPLACE INTO users (username, password_hash, updated_at) VALUES ('admin', '!', 0)"
    )
    llm_panel_db = app.state.panel.db
    assert llm_panel_db.get_setting("services_order") is None
    return c


@pytest.fixture
def authed_client(client):
    from llm_panel import security
    import re as _re

    security.set_password(client.app.state.panel.db, "admin", "correct-password")
    c = client
    page = c.get("/login", headers={"host": "localhost"})
    token = _re.search(r'name="login_token" value="([^"]+)"', page.text).group(1)
    r = c.post("/login", data={"username": "admin", "password": "correct-password",
                               "login_token": token, "next": "/"},
               headers={"host": "localhost"}, follow_redirects=False)
    assert r.status_code == 303
    return c


def _set_snapshot(c, services, gpus=None):
    c.app.state.panel.collector.snapshot = Snapshot(
        ts=0, services=services, gpus=gpus,
        host={"ram_used_mb": None, "swap_total_mb": None, "cpu_percent": None, "disks": []},
        warnings=[],
    )


# ---------------------------------------------------------------- settings
def test_setting_roundtrip(client):
    db = client.app.state.panel.db
    db.set_setting("services_order", '["llama","vllm"]')
    assert db.get_setting("services_order") == '["llama","vllm"]'
    db.set_setting("services_order", '["vllm"]')
    assert db.get_setting("services_order") == '["vllm"]'


# ---------------------------------------------------------------- порядок
def test_order_requires_csrf(authed_client):
    r = authed_client.post("/ui/order", data={"order": "llama,vllm"},
                           headers={"host": "localhost"})
    assert r.status_code == 403


def test_order_save_and_apply(authed_client):
    c = authed_client
    csrf = ""
    page = c.get("/", headers={"host": "localhost"})
    m = re.search(r'name="csrf" value="([^"]+)"', page.text)
    assert m, "csrf должен быть доступен в шаблоне автоматически"
    csrf = m.group(1)

    _set_snapshot(c, [_row("vllm"), _row("llama")])
    page = c.get("/", headers={"host": "localhost"})
    vllm_pos = page.text.find('id="svc-vllm"')
    llama_pos = page.text.find('id="svc-llama"')
    assert vllm_pos < llama_pos          # исходный порядок из registry

    r = c.post("/ui/order", data={"order": "llama,vllm", "csrf": csrf},
               headers={"host": "localhost"})
    assert r.status_code == 200
    page = c.get("/", headers={"host": "localhost"})
    assert page.text.find('id="svc-llama"') < page.text.find('id="svc-vllm"')

    db = c.app.state.panel.db
    import json as _json
    assert _json.loads(db.get_setting("services_order")) == ["llama", "vllm"]
    assert any(r_["action"] == "ui_order" for r_ in db.audit_tail(10))


def test_order_unknown_and_foreign_ids_filtered(authed_client):
    c = authed_client
    page = c.get("/", headers={"host": "localhost"})
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    r = c.post("/ui/order", data={"order": "nope,http://evil.com,vllm,,vllm", "csrf": csrf},
               headers={"host": "localhost"})
    assert r.status_code == 200
    assert c.app.state.panel.services_order == ["vllm"]   # чужие/неизвестные/дубли отброшены


def test_order_survives_app_restart(tmp_path):
    """Сохранённый порядок считывается заново при старте приложения."""
    from llm_panel.db import Database

    cfg = load_config(_make_config(tmp_path))
    pre = Database(cfg.db_path)
    pre.set_setting("services_order", '["llama","vllm"]')
    pre.close()

    app = create_app(cfg)
    assert app.state.panel.services_order == ["llama", "vllm"]
    app.state.panel.db.close()


# ------------------------------------------------------- авто-CSRF шаблонов
def test_logout_with_auto_csrf(authed_client):
    """Раньше csrf не передавался в базовый шаблон — «Выйти» ломалось на статусах (403)."""
    c = authed_client
    page = c.get("/", headers={"host": "localhost"})
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    r = c.post("/logout", data={"csrf": csrf}, headers={"host": "localhost"},
               follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


# ------------------------------------------------------------ имена GPU
def test_fmt_mem_mb_units():
    from llm_panel.metrics import fmt_mem_mb

    assert fmt_mem_mb(13784) == "13.78 ГБ"
    assert fmt_mem_mb(64197) == "64.2 ГБ"
    assert fmt_mem_mb(50413) == "50.4 ГБ"
    assert fmt_mem_mb(32768) == "32.77 ГБ"
    assert fmt_mem_mb(512) == "512 МБ"
    assert fmt_mem_mb(0.5) == "500 КБ"
    assert fmt_mem_mb(None) == "нет данных"


def test_host_cpu_and_units(authed_client):
    c = authed_client
    _set_snapshot(c, [], gpus=None)
    c.app.state.panel.collector.snapshot.host = {
        "ram_used_mb": 13784, "ram_total_mb": 64197, "ram_available_mb": 50413,
        "swap_used_mb": 512, "swap_total_mb": 8192, "cpu_percent": 45.7,
        "cpu_model": "Xeon E5-2680 v4", "cpu_cores": 14, "cpu_threads": 28,
        "cpu_ghz": 2.0, "disks": [],
    }
    page = c.get("/", headers={"host": "localhost"})
    assert ">RAM:</span>" in page.text and "13.78 ГБ / 64.2 ГБ (доступно 50.4 ГБ)" in page.text  # RAM: красивые ГБ
    assert ">Swap:</span>" in page.text and "512 МБ / 8.19 ГБ" in page.text                      # Swap в ГБ/МБ
    assert ">CPU:</span>" in page.text and "Xeon E5-2680 v4 · 14 ядер / 28 потоков · 2.0 ГГц — 46 %" in page.text  # модель/ядра/частота/загрузка
    assert 'p46' in page.text                                                                # track заполнен
    # диски в одной строке единого блока
    c.app.state.panel.collector.snapshot.host["disks"] = [["/models", 457.0, 72.3], ["/ling", 500.0, 61.0]]
    page = c.get("/", headers={"host": "localhost"})
    assert ">Диск:</span>" in page.text
    assert "/models: 72.3 ГБ свободно из 457 ГБ · /ling: 61.0 ГБ свободно из 500 ГБ" in page.text
    assert 'p86' in page.text    # итоговая полоса занятости диска
    # ---- GPU-карточки в стиле макета ----
    gpu = {"index": 0, "uuid": "GPU-9f764cdf-08bd-48a9-c219-a8660a71c1d6", "name": "Tesla V100-SXM2-32GB",
           "mem_total_mb": 32768, "mem_used_mb": 31159, "mem_pct": 95,
           "power_w": 56.5, "power_limit_w": 150.0, "power_pct": 38,
           "procs": [{"pid": 777, "name": "vllm", "mem_mb": 31000, "owner": None}],
           "assigned": ["vllm"]}
    c.app.state.panel.collector.snapshot.gpus = [gpu]
    page = c.get("/", headers={"host": "localhost"})
    assert "31.16 ГБ / 32.77 ГБ" in page.text                             # память GPU в ГБ
    assert "Вт / 150 Вт" in page.text                                     # мощность с единицей у лимита
    assert 'load-pill pill-err">95%' in page.text                         # pill загрузки (красный при >80)
    # строки процессов и UUID из карточки GPU убраны
    assert "PID" not in page.text and "Сервис не определён" not in page.text and "UUID:" not in page.text

    # пустые данные не должны падать
    c.app.state.panel.collector.snapshot.gpus = [dict(gpu, mem_pct=None, power_w=None, mem_used_mb=None, temp_c=None, procs=[], assigned=[])]
    page = c.get("/", headers={"host": "localhost"})
    assert "нет данных" in page.text

def test_gpu_names_in_card(authed_client):
    c = authed_client
    gpu = {"uuid": "GPU-9f764cdf-08bd-48a9-c219-a8660a71c1d6", "name": "Tesla V100-SXM2-32GB"}
    _set_snapshot(c, [_row("vllm", gpu_info=[gpu])], gpus=[gpu])
    page = c.get("/", headers={"host": "localhost"})
    assert "Tesla V100-SXM2-32GB" in page.text       # имя как в nvidia-smi
    assert 'title="GPU-9f764cdf' in page.text        # полный UUID в подсказке
    settings = c.get("/services/vllm/settings", headers={"host": "localhost"})
    assert settings.status_code == 200
    assert "Tesla V100-SXM2-32GB" in settings.text


# ------------------------------------------------- кнопки автозапуска юнита
def test_autostart_toggle_buttons(authed_client):
    c = authed_client
    _set_snapshot(c, [
        _row("vllm", autostart="enabled"),
        _row("llama", autostart="disabled"),
    ])
    page = c.get("/", headers={"host": "localhost"})
    assert 'action="/services/vllm/disable"' in page.text
    assert "Отключить автозапуск" in page.text
    assert 'action="/services/llama/enable"' in page.text
    assert "Включить автозапуск" in page.text
    # «Включить автозапуск» — бледная кнопка (autostart off), «Отключить…» — обычная
    assert 'class="action-btn btn-muted" title="Автозапуск выключен' in page.text
    assert 'class="action-btn" title="Автозапуск включён' in page.text
    # кнопка «Логи» убрана из карточки
    assert 'href="/services/vllm/logs"' not in page.text
    # шестерёнка настроек присутствует
    assert 'href="/services/vllm/settings"' in page.text
    logs_page = c.get("/services/vllm/settings", headers={"host": "localhost"})
    assert 'href="/services/vllm/logs"' in logs_page.text   # логи — в настройках
