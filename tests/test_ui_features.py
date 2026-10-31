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
    # язык интерфейса по умолчанию — en (ТЗ); для этих тестов включаем ru,
    # отдельные тесты проверяют en-дефолт и переключение через Config Panel
    app.state.panel.db.set_setting("lang", "ru")
    app.state.panel.i18n_lang = "ru"
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
        "cpu_ghz": 2.0, "cpu_temp_c": 30.0, "cpu_power_w": 34.9, "disks": [],
    }
    page = c.get("/", headers={"host": "localhost"})
    assert ">RAM:</span>" in page.text and "13.78 ГБ / 64.2 ГБ (доступно 50.4 ГБ)" in page.text  # RAM: красивые ГБ
    assert ">Swap:</span>" in page.text and "512 МБ / 8.19 ГБ" in page.text                      # Swap в ГБ/МБ
    # модель/ядра/частота + температура/мощность; процент загрузки — только на шкале
    assert ">CPU:</span>" in page.text and "Xeon E5-2680 v4 · 14 ядер / 28 потоков · 2.0 ГГц 30 °C 35 Вт" in page.text
    assert "— 46 %" not in page.text
    assert 'p46' in page.text and 'bar-pct">46%</span>' in page.text   # шкала заполнена, процент по центру
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
    assert '<span class="nb">31.16 ГБ</span> / <span class="nb">32.77 ГБ</span>' in page.text  # память GPU, неразрывные куски
    assert '<span class="nb">56.5 Вт</span> / <span class="nb">150 Вт</span>' in page.text    # мощность с единицей у лимита
    assert 'load-pill">95%' in page.text                              # pill нейтральный (v0.3.9)
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

# ------------------------------------------------------------ Config Panel
def test_config_panel_requires_auth(client):
    r = client.get("/config", headers={"host": "localhost"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/login")


def test_config_panel_get(authed_client):
    page = authed_client.get("/config", headers={"host": "localhost"})
    assert page.status_code == 200
    assert 'name="lang"' in page.text and 'name="refresh_interval"' in page.text
    assert 'name="temp_unit"' in page.text and 'name="audit_limit"' in page.text
    assert 'value="ru" selected' in page.text      # текущее значение из settings


def test_config_save_switches_language(authed_client):
    c = authed_client
    page = c.get("/config", headers={"host": "localhost"})
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    r = c.post("/config", data={"lang": "en", "temp_unit": "F", "refresh_interval": "5",
                                "audit_limit": "50", "csrf": csrf},
               headers={"host": "localhost"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/config?saved=1"
    panel = c.app.state.panel
    assert panel.i18n_lang == "en" and panel.temp_unit == "F"
    assert panel.collector.interval == 5.0 and panel.audit_limit == 50
    assert panel.db.get_setting("lang") == "en"                      # persist в settings
    assert any(x["action"] == "config" for x in panel.db.audit_tail(10))  # аудит
    _set_snapshot(c, [_row("vllm")])
    page = c.get("/", headers={"host": "localhost"})
    assert ">Services:</div>" in page.text and ">Start</button>" in page.text
    # сохранённый лимит аудита применяется по умолчанию
    audit = c.get("/audit", headers={"host": "localhost"})
    assert audit.status_code == 200


def test_config_invalid_values_rejected(authed_client):
    c = authed_client
    page = c.get("/config", headers={"host": "localhost"})
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    r = c.post("/config", data={"lang": "de", "temp_unit": "C", "refresh_interval": "3",
                                "audit_limit": "30", "csrf": csrf},
               headers={"host": "localhost"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/config"
    assert c.app.state.panel.i18n_lang == "ru"     # неизвестный язык не применился


def test_config_requires_csrf(authed_client):
    r = authed_client.post("/config", data={"lang": "en"}, headers={"host": "localhost"})
    assert r.status_code == 403


# ----------------------------------------------- Config Panel: диски (model_roots)
def test_config_model_roots_saved_and_applied(authed_client):
    """Пути дисковой сводки: сохранение в settings + живое применение в коллекторе."""
    c = authed_client
    page = c.get("/config", headers={"host": "localhost"})
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    r = c.post("/config", data={"lang": "ru", "temp_unit": "C", "refresh_interval": "3",
                                "audit_limit": "30",
                                "model_roots": "/data/models\n /data/backup \n", "csrf": csrf},
               headers={"host": "localhost"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/config?saved=1"
    panel = c.app.state.panel
    assert panel.model_roots == ["/data/models", "/data/backup"]       # пробелы отрезаны
    assert panel.collector.model_roots == ["/data/models", "/data/backup"]  # на ходу
    import json as _json
    assert _json.loads(panel.db.get_setting("model_roots")) == ["/data/models", "/data/backup"]
    page = c.get("/config", headers={"host": "localhost"})
    assert "/data/models" in page.text and "/data/backup" in page.text  # показан в форме


def test_config_model_roots_invalid_rejected(authed_client):
    """Относительные пути/дубликаты — отказ без изменения значений."""
    c = authed_client
    panel = c.app.state.panel
    panel.model_roots = ["/data/models"]
    panel.collector.model_roots = ["/data/models"]
    page = c.get("/config", headers={"host": "localhost"})
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    r = c.post("/config", data={"lang": "ru", "temp_unit": "C", "refresh_interval": "3",
                                "audit_limit": "30",
                                "model_roots": "data/models\n/data/models", "csrf": csrf},
               headers={"host": "localhost"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/config?err=roots"
    assert panel.model_roots == ["/data/models"]            # не изменилось
    assert panel.collector.model_roots == ["/data/models"]
    err_page = c.get("/config?err=roots", headers={"host": "localhost"})
    assert "cfg_roots_err" not in err_page.text             # ключ переведён, а не выведен сырым
    assert "абсолютный путь" in err_page.text or "absolute path" in err_page.text


# ----------------------------------------------- Config Panel: тема оформления
def test_config_theme_saved_and_applied(authed_client):
    """Тема: сохранение в settings + data-theme на <html> всех страниц."""
    c = authed_client
    page = c.get("/config", headers={"host": "localhost"})
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    r = c.post("/config", data={"lang": "ru", "temp_unit": "C", "refresh_interval": "3",
                                "audit_limit": "30", "theme": "light", "csrf": csrf},
               headers={"host": "localhost"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/config?saved=1"
    panel = c.app.state.panel
    assert panel.theme == "light"
    assert panel.db.get_setting("theme") == "light"
    page = c.get("/", headers={"host": "localhost"})
    assert 'data-theme="light"' in page.text
    login = c.get("/login", headers={"host": "localhost"}, follow_redirects=False)
    # вошедшего редиректнет с /login — проверяем главную и config
    cfg = c.get("/config", headers={"host": "localhost"})
    assert 'data-theme="light"' in cfg.text
    # обратно на тёмную
    r = c.post("/config", data={"lang": "ru", "temp_unit": "C", "refresh_interval": "3",
                                "audit_limit": "30", "theme": "dark", "csrf": csrf},
               headers={"host": "localhost"}, follow_redirects=False)
    assert r.status_code == 303
    assert c.app.state.panel.theme == "dark"
    page = c.get("/", headers={"host": "localhost"})
    assert 'data-theme="dark"' in page.text


def test_config_theme_invalid_rejected(authed_client):
    c = authed_client
    page = c.get("/config", headers={"host": "localhost"})
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    r = c.post("/config", data={"lang": "ru", "temp_unit": "C", "refresh_interval": "3",
                                "audit_limit": "30", "theme": "blue", "csrf": csrf},
               headers={"host": "localhost"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/config"
    assert c.app.state.panel.theme == "dark"      # не изменилась


def test_config_theme_absent_keeps_value(authed_client):
    """Поле theme отсутствует (старый клиент) — тема не меняется."""
    c = authed_client
    page = c.get("/config", headers={"host": "localhost"})
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    r = c.post("/config", data={"lang": "ru", "temp_unit": "C", "refresh_interval": "3",
                                "audit_limit": "30", "csrf": csrf},
               headers={"host": "localhost"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/config?saved=1"
    assert c.app.state.panel.theme == "dark"


def test_config_model_roots_absent_keeps_value(authed_client):
    """Поле отсутствует в форме (старый клиент) — значение не трогается."""
    c = authed_client
    panel = c.app.state.panel
    panel.model_roots = ["/keep/me"]
    page = c.get("/config", headers={"host": "localhost"})
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    r = c.post("/config", data={"lang": "ru", "temp_unit": "C", "refresh_interval": "3",
                                "audit_limit": "30", "csrf": csrf},
               headers={"host": "localhost"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/config?saved=1"
    assert panel.model_roots == ["/keep/me"]


def test_model_roots_default_from_config(tmp_path):
    """Без settings.model_roots стартовое значение берётся из config.yaml (models.roots)."""
    from fastapi.testclient import TestClient
    from llm_panel import security

    cfg = load_config(_make_config(tmp_path))
    app = create_app(cfg)
    app.state.panel.db.execute(
        "INSERT OR REPLACE INTO users (username, password_hash, updated_at) VALUES ('admin', '!', 0)"
    )
    security.set_password(app.state.panel.db, "admin", "pw")
    c = TestClient(app)
    assert app.state.panel.model_roots == list(cfg.model_roots)
    assert app.state.panel.collector.model_roots == list(cfg.model_roots)
    app.state.panel.db.close()


def test_default_language_is_en(tmp_path):
    """Язык по умолчанию — английский (без settings.lang)."""
    from fastapi.testclient import TestClient
    from llm_panel import security

    cfg = load_config(_make_config(tmp_path))
    app = create_app(cfg)
    app.state.panel.db.execute(
        "INSERT OR REPLACE INTO users (username, password_hash, updated_at) VALUES ('admin', '!', 0)"
    )
    security.set_password(app.state.panel.db, "admin", "pw")
    c = TestClient(app)
    assert app.state.panel.i18n_lang == "en"
    page = c.get("/login", headers={"host": "localhost"})
    assert "Sign in" in page.text and "Username" in page.text
    app.state.panel.db.close()


# ---------------- v0.3.6: температура/мощность CPU, шкалы по образцу ----------

def test_read_host_temp_power_fields_none_without_sensors():
    """В тестовой среде (Windows/нет сенсоров и RAPL) — оба поля None, без падений."""
    from llm_panel.metrics import HostMetrics, MetricsCollector

    m = MetricsCollector()
    h = m.read_host(())
    assert isinstance(h, HostMetrics)
    assert h.cpu_temp_c is None
    assert h.cpu_power_w is None


def test_cpu_temp_package_id_priority(monkeypatch):
    """Температура CPU: «Package id 0» в приоритете, иначе максимум по сенсорам."""
    from types import SimpleNamespace as NS

    from llm_panel import metrics as met

    sensors = {"coretemp": [NS(label="Core 0", current=41.0),
                            NS(label="Package id 0", current=30.0)]}

    class FakePsutil:
        @staticmethod
        def sensors_temperatures():
            return sensors

    monkeypatch.setattr(met, "psutil", FakePsutil)
    m = met.MetricsCollector.__new__(met.MetricsCollector)
    assert m._read_cpu_temp() == 30.0           # Package важнее ядра

    sensors["coretemp"] = [NS(label="Core 0", current=41.0),
                           NS(label="Core 1", current=37.0)]
    assert m._read_cpu_temp() == 41.0           # без Package — максимум

    class BadPsutil:
        @staticmethod
        def sensors_temperatures():
            raise RuntimeError("no sensors")

    monkeypatch.setattr(met, "psutil", BadPsutil)
    assert m._read_cpu_temp() is None


def test_cpu_power_rapl_delta(monkeypatch):
    """RAPL: Вт = дельта energy_uj за интервал; переполнение/нереалистично → None."""
    from llm_panel import metrics as met

    state = {"uj": 1_500_000_000, "t": 100.0}

    class FakeDom:
        def __init__(self, name):
            self.name = name

        def __truediv__(self, other):
            return self

        def read_text(self):
            return str(state["uj"])

    class FakePath:
        def __init__(self, p):
            pass

        def glob(self, pattern):
            return [FakeDom("intel-rapl:0")]

    class FakeTime:
        @staticmethod
        def monotonic():
            return state["t"]

    class FakePathlib:
        Path = FakePath

    monkeypatch.setattr(met, "pathlib", FakePathlib)
    monkeypatch.setattr(met, "time", FakeTime())

    m = met.MetricsCollector.__new__(met.MetricsCollector)
    m._rapl_prev = {}
    assert m._read_cpu_power() is None            # первое чтение — дельты нет
    state["uj"] = 1_506_000_000
    state["t"] = 103.0
    assert m._read_cpu_power() == 2.0             # 6 Дж за 3 c = 2 Вт
    state["uj"] = 100                             # переполнение счётчика
    state["t"] = 104.0
    assert m._read_cpu_power() is None
    # нереалистично большие значения отбрасываются
    state["uj"] = 1_500_000_000 + 10_000_000_000  # 10 кВт за секунду
    state["t"] = 105.0
    assert m._read_cpu_power() is None


def test_snapshot_carries_cpu_temp_power():
    """Снапшот прокидывает cpu_temp_c/cpu_power_w в host-словарь."""
    import inspect

    from llm_panel import collector

    src = inspect.getsource(collector)
    assert '"cpu_temp_c": host.cpu_temp_c' in src
    assert '"cpu_power_w": host.cpu_power_w' in src


def test_scales_use_sample_style(authed_client):
    """Шкалы по образцу: тон-класс на .mini-track, процент по центру (bar-pct)."""
    c = authed_client
    _set_snapshot(c, [], gpus=[])
    gpu = {"index": 0, "uuid": "GPU-9f764cdf-08bd-48a9-c219-a8660a71c1d6", "name": "V100",
           "mem_total_mb": 32768, "mem_used_mb": 19660, "mem_pct": 60,
           "power_w": 56.5, "power_limit_w": 150.0, "power_pct": 38,
           "procs": [], "assigned": []}
    c.app.state.panel.collector.snapshot.gpus = [gpu]
    page = c.get("/", headers={"host": "localhost"})
    # тон-класс теперь на треке (красит рамку), заливка — первый span
    assert 'mini-track t-warn"><span class="p60">' in page.text
    assert 'bar-pct">60%</span>' in page.text
    assert 'bar-pct">38%</span>' in page.text
    assert '<div class="mini-track"><span' not in page.text  # старый вид шкал исчез


def test_models_numbering_and_manual_sort(authed_client):
    """Модели: нумерация строк + кнопка sort с ручным порядком (drag&drop)."""
    import json as _json
    import time as _time
    from types import SimpleNamespace as NS

    c = authed_client
    state = c.app.state.panel

    def M(name, size):
        return NS(name=name, path=f"/models/{name}", kind="gguf", quant="",
                  dtype="", model_type="", size_bytes=size, n_files=1)

    state.models_scan = {"models": [M("bbb", 20), M("aaa", 100)],
                         "roots": [], "errors": [], "ts": _time.time()}
    page = c.get("/models", headers={"host": "localhost"})
    assert 'class="model-num">1</span>' in page.text
    assert 'data-model="/models/aaa"' in page.text
    assert "models-order-btn" in page.text                       # кнопка sort
    assert 'id="models-list"' in page.text                       # контейнер drag&drop (v0.3.10)
    assert "/models?sort=" not in page.text                      # переключатель убран
    # ручной порядок: aaa в конец → bbb первым
    state.db.set_setting("models_order", _json.dumps(["/models/bbb", "/models/aaa"]))
    page = c.get("/models", headers={"host": "localhost"})
    assert page.text.index('model-name">bbb') < page.text.index('model-name">aaa')
    # новая модель, отсутствующая в порядке, — в конец
    state.models_scan = {"models": [M("ccc", 5)] + state.models_scan["models"],
                         "roots": [], "errors": [], "ts": _time.time()}
    page = c.get("/models", headers={"host": "localhost"})
    assert page.text.index('model-name">ccc') > page.text.index('model-name">aaa')


def test_matrix_conflicts_notice_below_table(authed_client):
    """GPU matrix: блок конфликтов / «нет конфликтов» — ПОД таблицей (v0.3.10)."""
    c = authed_client
    gpu = {"index": 0, "uuid": "GPU-9f764cdf-08bd-48a9-c219-a8660a71c1d6",
           "name": "Tesla V100-SXM2-32GB", "mem_total_mb": 32768,
           "mem_used_mb": None, "mem_pct": None, "power_w": None,
           "power_limit_w": None, "power_pct": None, "procs": [], "assigned": []}
    _set_snapshot(c, [_row("vllm", gpu_info=[gpu])], gpus=[gpu])
    page = c.get("/matrix", headers={"host": "localhost"})
    assert "</table>" in page.text                              # таблица отрисована
    tail = page.text.rsplit("</table>", 1)[1]                   # всё после таблицы
    assert "Конфликтов GPU/RAM/портов не обнаружено." in tail   # уведомление — под ней
    assert page.text.index("Конфликтов GPU/RAM/портов не обнаружено.") > page.text.rindex("<table")
