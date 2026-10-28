"""Редактор конфигураций (этап 2, v0.2.2): регистрация, версии, diff, rollback.

Границы безопасности: root-owned /etc/llm-panel — отказ; .json валидируется
до записи; rollback пишет версию как новую; дубликаты версий не копятся.
"""

import json
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from llm_panel.app import create_app
from llm_panel.config import load_config
from llm_panel.i18n import translate

from test_service_display import _make_config

# сообщения рендерятся переведёнными (t(err)) — сверяемся с переводом
E = lambda key: translate("en", key)  # noqa: E731


def _authed(tmp_path):
    """App + TestClient с выполненным входом."""
    from fastapi.testclient import TestClient

    cfg = load_config(_make_config(tmp_path))
    app = create_app(cfg)
    from llm_panel import security
    security.set_password(app.state.panel.db, "admin", "correct-password")
    return TestClient(app), app


def _login(client):
    r = client.get("/login", headers={"host": "localhost"})
    m = re.search(r'name="login_token" value="([^"]+)"', r.text)
    client.post("/login", data={"username": "admin", "password": "correct-password",
                                "login_token": m.group(1), "next": "/"},
                headers={"host": "localhost"})


def _add_file(client, csrf, path, label="Maya config"):
    return client.post("/config/files/add",
                       data={"path": str(path), "label": label, "csrf": csrf},
                       headers={"host": "localhost"})


def _csrf(client):
    """CSRF токен сессии — из тела любой страницы (data-csrf в <body>)."""
    r = client.get("/config/files", headers={"host": "localhost"})
    m = re.search(r'data-csrf="([^"]+)"', r.text)
    return m.group(1) if m else ""


def test_add_reject_bad_paths(tmp_path, monkeypatch):
    client, app = _authed(tmp_path)
    _login(client)
    csrf = _csrf(client)

    # relative path
    r = _add_file(client, csrf, "relative/path.json")
    assert E("cfgfile_err_relative") in r.text
    # /etc/llm-panel — root-owned, отказ
    r = _add_file(client, csrf, "/etc/llm-panel/services.yaml")
    assert E("cfgfile_err_forbidden") in r.text
    # несуществующий
    r = _add_file(client, csrf, str(tmp_path / "no-such.json"))
    assert E("cfgfile_err_missing") in r.text
    # директория вместо файла
    r = _add_file(client, csrf, str(tmp_path))
    assert E("cfgfile_err_missing") in r.text
    # ничего не зарегистрировано
    assert json.loads(app.state.panel.db.get_setting("config_files") or "[]") == []


def test_add_save_diff_rollback(tmp_path):
    client, app = _authed(tmp_path)
    _login(client)
    csrf = _csrf(client)
    f = tmp_path / "maya-maya-m.json"
    f.write_text('{"gpu": [0, 1, 2]}', encoding="utf-8")

    r = _add_file(client, csrf, f, "Maya")
    assert E("cfgfile_added") in r.text
    files = json.loads(app.state.panel.db.get_setting("config_files"))
    fid = files[0]["id"]

    # базовая версия записана
    vers = app.state.panel.db.config_versions(str(f))
    assert len(vers) == 1 and vers[0]["note"] == "baseline"

    # сохранение корректного JSON → вторая версия
    r = client.post(f"/config/files/{fid}/save",
                    data={"content": '{"gpu": [0, 1]}', "csrf": csrf},
                    headers={"host": "localhost"})
    assert E("cfgfile_saved") in r.text
    assert f.read_text(encoding="utf-8") == '{"gpu": [0, 1]}'
    vers = app.state.panel.db.config_versions(str(f))
    assert len(vers) == 2

    # невалидный JSON НЕ записывается
    r = client.post(f"/config/files/{fid}/save",
                    data={"content": '{"gpu": [0, broken}', "csrf": csrf},
                    headers={"host": "localhost"})
    assert E("cfgfile_err_json") in r.text
    assert f.read_text(encoding="utf-8") == '{"gpu": [0, 1]}'

    # diff страница: базовая версия против текущего (Jinja экранирует кавычки)
    r = client.get(f"/config/files/{fid}?diff={vers[-1]['id']}",
                   headers={"host": "localhost"})
    assert '-{&#34;gpu&#34;: [0, 1, 2]}' in r.text
    assert '+{&#34;gpu&#34;: [0, 1]}' in r.text

    # rollback к базовой версии
    base_vid = vers[-1]["id"]
    r = client.post(f"/config/files/{fid}/rollback/{base_vid}",
                    data={"csrf": csrf}, headers={"host": "localhost"})
    assert E("cfgfile_rolled") in r.text
    assert f.read_text(encoding="utf-8") == '{"gpu": [0, 1, 2]}'
    vers = app.state.panel.db.config_versions(str(f))
    assert vers[0]["note"] == "rollback от #%d" % base_vid   # DESC — первая запись
    assert len(vers) == 3


def test_duplicate_version_not_recorded(tmp_path):
    client, app = _authed(tmp_path)
    _login(client)
    csrf = _csrf(client)
    f = tmp_path / "x.json"
    f.write_text("{}", encoding="utf-8")
    _add_file(client, csrf, f)
    fid = json.loads(app.state.panel.db.get_setting("config_files"))[0]["id"]
    client.post(f"/config/files/{fid}/save", data={"content": "{}", "csrf": csrf},
                headers={"host": "localhost"})
    # содержимое не изменилось → версии не добавилась
    assert len(app.state.panel.db.config_versions(str(f))) == 1


def test_unknown_file_404(tmp_path):
    client, app = _authed(tmp_path)
    _login(client)
    r = client.get("/config/files/999", headers={"host": "localhost"})
    assert r.status_code == 404
