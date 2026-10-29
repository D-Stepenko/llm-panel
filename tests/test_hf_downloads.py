"""HF-загрузки (этап 3, v0.3.1): валидация, уведомление об отсутствии инструмента,
жизненный цикл задач, роуты."""

import errno
import os
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from llm_panel.app import create_app
from llm_panel.config import load_config
from llm_panel import hf_downloads as hf

from test_service_display import _make_config


# ------------------------------------------------ валидация ------------------

def test_validate_repo():
    assert hf.validate_repo("Qwen/Qwen3-8B-GGUF")
    assert hf.validate_repo(" unsloth/Llama-3.2-1B ")
    assert not hf.validate_repo("no-slash")
    assert not hf.validate_repo("owner/name; rm -rf /")
    assert not hf.validate_repo("../owner/name")
    assert not hf.validate_repo("owner/")


def test_resolve_dest(tmp_path):
    root = str(tmp_path / "models")
    pathlib.Path(root).mkdir()
    # без подкаталога — сам корень
    dest, err = hf.resolve_dest(root, "")
    assert dest == str(pathlib.Path(root).resolve()) and err == ""
    # подкаталог внутри
    dest, err = hf.resolve_dest(root, "Qwen/Qwen3-8B-GGUF")
    assert err == "" and dest.startswith(str(pathlib.Path(root).resolve()))
    # выход за корень
    dest, err = hf.resolve_dest(root, "../escape")
    assert dest is None
    dest, err = hf.resolve_dest(root, "a/b/../../..")
    assert dest is None
    # мусор в подкаталоге
    dest, err = hf.resolve_dest(root, "a b;rm")
    assert dest is None
    # несуществующий корень
    dest, err = hf.resolve_dest(str(tmp_path / "no-such"), "x")
    assert dest is None and err == "hf_err_root_missing"


def test_find_tool_missing(monkeypatch):
    monkeypatch.setattr(hf.shutil, "which", lambda name: None)
    assert hf.find_tool() is None


def test_find_tool_prefers_hf(monkeypatch):
    monkeypatch.setattr(hf.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert hf.find_tool() == "/usr/bin/hf"


def test_find_tool_custom_path(tmp_path, monkeypatch):
    """v0.3.2: настроенный полный путь (conda env без активации) имеет приоритет."""
    monkeypatch.setattr(hf.shutil, "which", lambda name: None)   # в PATH ничего нет
    exe = tmp_path / "envs" / "x" / "bin" / "hf"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    # валидный custom → он и возвращается
    p = str(exe)
    assert hf.find_tool(p) == p
    # несуществующий/неисполняемый → fallback в PATH → None
    assert hf.find_tool(str(tmp_path / "no-such" / "hf")) is None


# ------------------------------------------------ Config Panel: путь к hf ----

def _csrf(c):
    page = c.get("/models", headers={"host": "localhost"})
    return re.search(r'data-csrf="([^"]+)"', page.text).group(1)


def test_config_save_hf_tool_path(tmp_path):
    """v0.3.2: путь к hf сохраняется в settings и показывается в форме."""
    c, panel = _client(tmp_path)
    exe = tmp_path / "hf"
    exe.write_text("#!/bin/sh\n")
    page = c.get("/config", headers={"host": "localhost"})
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    r = c.post("/config", data={"lang": "ru", "temp_unit": "C", "refresh_interval": "3",
                                "audit_limit": "30",
                                "hf_tool_path": str(exe), "csrf": csrf},
               headers={"host": "localhost"}, follow_redirects=False)
    assert r.status_code == 303
    assert panel.hf_tool_path == str(exe)
    assert panel.db.get_setting("hf_tool_path") == str(exe)
    page = c.get("/config", headers={"host": "localhost"})
    assert str(exe) in page.text


def test_config_save_hf_tool_path_invalid(tmp_path):
    """Несуществующий путь — редирект err=hfpath, значение не меняется."""
    c, panel = _client(tmp_path)
    page = c.get("/config", headers={"host": "localhost"})
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    bad = str(tmp_path / "no-such" / "hf")
    r = c.post("/config", data={"lang": "ru", "temp_unit": "C", "refresh_interval": "3",
                                "audit_limit": "30", "hf_tool_path": bad, "csrf": csrf},
               headers={"host": "localhost"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/config?err=hfpath"
    assert panel.hf_tool_path == ""                       # не изменилось
    err_page = c.get("/config?err=hfpath", headers={"host": "localhost"})
    assert "cfg_hf_path_err" not in err_page.text         # переведён, не сырой ключ


def test_page_banner_mentions_conda(tmp_path, monkeypatch):
    """Баннер без инструмента: три варианта установки + подсказка про conda-путь."""
    c, _ = _client(tmp_path)
    monkeypatch.setattr(hf, "find_tool", lambda *a, **k: None)
    page = c.get("/models", headers={"host": "localhost"})
    assert "pipx install" in page.text
    assert "pip install -U" in page.text                  # вариант для conda env
    assert "break-system-packages" in page.text
    assert "conda activate" in page.text                  # подсказка which hf


def test_download_uses_custom_tool_path(tmp_path, monkeypatch):
    """v0.3.2: загрузка использует настроенный путь к hf из conda env."""
    c, state = _client(tmp_path)
    monkeypatch.setattr(hf.shutil, "which", lambda name: None)  # PATH пуст
    exe = tmp_path / "envs" / "hf"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    state.hf_tool_path = str(exe)
    root = tmp_path / "models"
    root.mkdir()
    state.model_roots = [str(root)]
    launched = {}

    async def fake_start(st, tool, repo, dest):
        launched["tool"] = tool
        job = hf.HFJob(id="jobcx", repo=repo, dest=dest, proc=None)
        st.hf_jobs[job.id] = job
        return job

    monkeypatch.setattr(hf, "start_job", fake_start)
    csrf = _csrf(c)
    r = c.post("/models/download", data={"repo": "Qwen/Qwen3-8B-GGUF",
                                         "root": str(root),
                                         "subpath": "", "csrf": csrf},
               headers={"host": "localhost"})
    assert r.status_code == 200
    assert launched["tool"] == str(exe)


# ------------------------------------------------ read-only destination ------

def test_check_writable(tmp_path, monkeypatch):
    """Пробная запись: обычный каталог → ""; EROFS → ключ hf_err_ro."""
    d = tmp_path / "w"
    d.mkdir()
    assert hf.check_writable(str(d)) == ""

    def fake_open(path, flags, *a, **k):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(os, "open", fake_open)
    assert hf.check_writable(str(d)) == "hf_err_ro"


def test_download_readonly_dest_shows_notice(tmp_path, monkeypatch):
    """read-only корень (как /home/dispot/models): уведомление вместо 500."""
    c, state = _client(tmp_path)
    monkeypatch.setattr(hf, "find_tool", lambda *a, **k: "/usr/bin/hf")
    root = tmp_path / "models"
    root.mkdir()
    state.model_roots = [str(root)]
    monkeypatch.setattr(hf, "check_writable", lambda dest: "hf_err_ro")
    page = c.get("/models", headers={"host": "localhost"})
    csrf = re.search(r'data-csrf="([^"]+)"', page.text).group(1)
    r = c.post("/models/download", data={"repo": "ornith-ai/Ornith-1.5-35B-A3B-FP8",
                                         "root": str(root), "subpath": "",
                                         "csrf": csrf},
               headers={"host": "localhost"})
    assert r.status_code == 200
    assert "ReadWritePaths" in r.text          # инструкция по песочнице юнита
    assert not state.hf_jobs          # задача не стартовала


# ------------------------------------------------ роуты ----------------------

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
    return c, app.state.panel


def test_page_shows_install_banner_when_tool_missing(tmp_path, monkeypatch):
    c, state = _client(tmp_path)
    monkeypatch.setattr(hf, "find_tool", lambda *a, **k: None)
    page = c.get("/models", headers={"host": "localhost"})
    assert page.status_code == 200
    assert "pip3 install --user" in page.text          # команда для копирования
    assert "hf_err_no_tool" not in page.text or "not installed" in page.text


def test_page_shows_form_when_tool_present(tmp_path, monkeypatch):
    c, state = _client(tmp_path)
    monkeypatch.setattr(hf, "find_tool", lambda *a, **k: "/usr/bin/hf")
    page = c.get("/models", headers={"host": "localhost"})
    assert 'action="/models/download"' in page.text
    assert page.text.count("name=\"root\"") == 1


def test_download_bad_repo_rejected(tmp_path, monkeypatch):
    c, state = _client(tmp_path)
    monkeypatch.setattr(hf, "find_tool", lambda *a, **k: "/usr/bin/hf")
    page = c.get("/models", headers={"host": "localhost"})
    csrf = re.search(r'data-csrf="([^"]+)"', page.text).group(1)
    r = c.post("/models/download", data={"repo": "no slash", "root": "/tmp",
                                         "subpath": "", "csrf": csrf},
               headers={"host": "localhost"})
    assert "owner/name" in r.text


def test_download_starts_job_and_audits(tmp_path, monkeypatch):
    c, state = _client(tmp_path)
    monkeypatch.setattr(hf, "find_tool", lambda *a, **k: "/usr/bin/hf")
    root = tmp_path / "models"
    root.mkdir()
    state.model_roots = [str(root)]
    launched = {}

    async def fake_start(st, tool, repo, dest):
        launched["args"] = (tool, repo, dest)
        job = hf.HFJob(id="job01", repo=repo, dest=dest, proc=None)
        st.hf_jobs[job.id] = job
        return job

    monkeypatch.setattr(hf, "start_job", fake_start)
    page = c.get("/models", headers={"host": "localhost"})
    csrf = re.search(r'data-csrf="([^"]+)"', page.text).group(1)
    r = c.post("/models/download", data={"repo": "Qwen/Qwen3-8B-GGUF",
                                         "root": str(root),
                                         "subpath": "Qwen3", "csrf": csrf},
               headers={"host": "localhost"})
    assert r.status_code == 200
    assert launched["args"][0] == "/usr/bin/hf"
    assert launched["args"][1] == "Qwen/Qwen3-8B-GGUF"
    assert any(a["action"] == "hf_download" for a in state.db.audit_tail(5))


def test_stop_and_clear_lifecycle(tmp_path, monkeypatch):
    c, state = _client(tmp_path)
    monkeypatch.setattr(hf, "find_tool", lambda *a, **k: "/usr/bin/hf")
    page = c.get("/models", headers={"host": "localhost"})
    csrf = re.search(r'data-csrf="([^"]+)"', page.text).group(1)

    class FakeProc:
        def __init__(self):
            self.terminated = False

        def terminate(self):
            self.terminated = True

    job = hf.HFJob(id="job02", repo="a/b", dest=str(tmp_path), proc=FakeProc())
    state.hf_jobs[job.id] = job

    r = c.post("/models/jobs/job02/stop", data={"csrf": csrf},
               headers={"host": "localhost"})
    assert r.status_code == 200
    assert job.status == "stopping" and job.proc.terminated

    # остановку/завершение имитируем: статус не running → clear работает
    job.status = "stopped"
    r = c.post("/models/jobs/job02/clear", data={"csrf": csrf},
               headers={"host": "localhost"})
    assert r.status_code == 200
    assert "job02" not in state.hf_jobs

    # неизвестная задача → 404
    r = c.post("/models/jobs/nope/clear", data={"csrf": csrf},
               headers={"host": "localhost"})
    assert r.status_code == 404


def test_fragment_polling_renders_jobs(tmp_path):
    c, state = _client(tmp_path)
    job = hf.HFJob(id="job03", repo="x/y", dest=str(tmp_path), proc=None)
    job.tail.append("Downloading model.safetensors: 45%")
    state.hf_jobs[job.id] = job
    frag = c.get("/models/jobs", headers={"host": "localhost"})
    assert frag.status_code == 200
    assert "x/y" in frag.text and "45%" in frag.text
    assert "hx-trigger=\"every 2s\"" in frag.text
    assert 'hx-post="/models/jobs/job03/stop"' in frag.text   # стоп — без перехода со страницы
