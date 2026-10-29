"""Мастер «модель → сервис» (этап 3, v0.3.5): slug/юнит/порт, генерация
services.yaml (валидна для реестра), шаблон юнита, роуты /wizard."""

import pathlib
import re
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from llm_panel import model_service as ms
from llm_panel.app import create_app
from llm_panel.config import load_config
from llm_panel.model_catalog import ModelEntry
from llm_panel.registry import load_services

from test_service_display import _make_config


def _entry(name="Qwen3.8-27B-FP8", path="/models/Qwen3.8-27B-FP8", kind="safetensors"):
    return ModelEntry(name=name, path=path, root="/models", kind=kind,
                      size_bytes=1024, n_files=3, family="Qwen", quant="FP8",
                      dtype="", model_type="")


# ------------------------------------------------ утилиты --------------------

def test_slugify_and_unit():
    assert ms.slugify("Qwen3.8-27B-FP8") == "qwen3-8-27b-fp8"
    assert ms.slugify("GLM-5.3-Flash-Maya-M-IQ2_S") == "glm-5-3-flash-maya-m-iq2-s"
    assert ms.suggest_unit("Qwen3.8-27B-GGUF", "llamacpp") == "llama-qwen3-8-27b-gguf.service"
    assert ms.suggest_unit("Ornith-1.5-35B-A3B-FP8", "vllm") == "vllm-ornith-1-5-35b-a3b-fp8.service"


def test_free_port_auto_and_preferred(tmp_path):
    st = types.SimpleNamespace(
        cfg=types.SimpleNamespace(ports_min=8100, ports_max=8199),
        registry=types.SimpleNamespace(services=[]),
        extra_defs={})
    port, err = ms.free_port(st, "")
    assert err == "" and port == 8100
    port, err = ms.free_port(st, "8155")
    assert err == "" and port == 8155
    # вне диапазона / мусор
    assert ms.free_port(st, "90")[1] == "wizard_err_range"
    assert ms.free_port(st, "abc")[1] == "wizard_err_port"


def test_free_port_skips_used_and_listening(tmp_path):
    import socket
    st = types.SimpleNamespace(
        cfg=types.SimpleNamespace(ports_min=8100, ports_max=8199),
        registry=types.SimpleNamespace(
            services=[types.SimpleNamespace(port=8100),
                      types.SimpleNamespace(port=8102)]),
        extra_defs={})
    port, err = ms.free_port(st, "")
    assert err == "" and port == 8101            # 8100 занят реестром, 8101 свободен
    # реальный слушатель → bind-проба ловит
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 8103))
    blocker.listen(1)
    try:
        assert ms.free_port(st, "8103")[1] == "wizard_err_busy"
    finally:
        blocker.close()


def test_build_service_and_yaml_registry_valid(tmp_path):
    """Сгенерированный блок проходит валидацию реестра services.yaml."""
    svc = ms.build_service(_entry(), "vllm",
                           ["GPU-9f764cdf-08bd-48a9-c219-a8660a71c1d6"], 8110,
                           "vllm-ornith.service", "index", notes="тест")
    yaml_text = ms.render_yaml(svc)
    p = tmp_path / "services.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    reg = load_services(p, ports_min=8100, ports_max=8999)
    s = reg.by_id("qwen3-8-27b-fp8")
    assert s is not None
    assert s.type == "vllm" and s.port == 8110
    assert s.gpus == ("GPU-9f764cdf-08bd-48a9-c219-a8660a71c1d6",)
    assert s.cuda_addressing == "index"
    assert s.health.url == "http://127.0.0.1:8110/health"


def test_unit_template_llamacpp_and_vllm():
    e = _entry(kind="gguf")
    t1 = ms.unit_template("llama-x.service", e, "llamacpp", 8111,
                          ["GPU-cfec683a-1069-2f9c-0425-2e26192519e6"], ())
    assert "llama-server -m /models/Qwen3.8-27B-FP8/<model.gguf>" in t1
    assert "CUDA_VISIBLE_DEVICES=\"GPU-cfec683a" in t1
    t2 = ms.unit_template("vllm-x.service", e, "vllm", 8112,
                          ["GPU-9f764cdf-08bd-48a9-c219-a8660a71c1d6"], (0, 1))
    assert "vllm serve /models/Qwen3.8-27B-FP8" in t2
    assert 'CUDA_VISIBLE_DEVICES="0,1"' in t2


def test_gpu_conflicts(tmp_path):
    """Выбор карты, уже запланированной/занятой, даёт предупреждение."""
    client_state = _state_with_gpu(tmp_path, planned=True, actual=True)
    warns = ms.gpu_conflicts(client_state, ["GPU-aaaabbbbb-cccc-dddd-eeee"])
    assert any("запланирована" in w for w in warns)
    assert any("занята" in w for w in warns)


def _state_with_gpu(tmp_path, planned=False, actual=False):
    """Минимальный state: одна карта, матрица видит план/факт по настроению."""
    gpu_uuid = "GPU-aaaabbbbb-cccc-dddd-eeee"
    snap = types.SimpleNamespace(
        services=[],
        gpus=[{"uuid": gpu_uuid, "index": 0, "name": "Test GPU", "mem_total_mb": 24576}],
        warnings=[])
    svc = types.SimpleNamespace(
        id="other", name="Other", unit="other.service", scope="system",
        port=None, gpus=(gpu_uuid,) if planned else (),
        cuda_addressing="uuid", ram_exclusive=False, health=None,
        startup_timeout=600, stop_timeout=None)
    actual_row = ({"id": "other", "state": "ready",
                   "gpu_actual": [{"uuid": gpu_uuid, "pid": 1}]} if actual else {})

    class _Snap:
        def __init__(self):
            self.snapshot = snap

    collector = _Snap()
    collector.snapshot = snap
    # svc_rows для матрицы берутся из snap.services — подменим строку факта
    snap.services = [actual_row] if actual else []
    return types.SimpleNamespace(
        cfg=types.SimpleNamespace(ports_min=8100, ports_max=8199),
        registry=types.SimpleNamespace(services=[svc] if planned else []),
        extra_defs={}, collector=collector)


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


def _seed_model(state, tmp_path):
    d = tmp_path / "models" / "Qwen3.8-27B-FP8"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text('{"model_type": "qwen3"}', encoding="utf-8")
    (d / "model.safetensors").write_bytes(b"x" * 64)
    state.model_roots = [str(tmp_path / "models")]
    state.models_scan = None


def test_wizard_step1_lists_models(tmp_path):
    c, state = _client(tmp_path)
    _seed_model(state, tmp_path)
    page = c.get("/wizard", headers={"host": "localhost"})
    assert page.status_code == 200
    assert "Qwen3.8-27B-FP8" in page.text
    assert 'action="/wizard/select"' in page.text


def test_wizard_step2_preselects_engine_by_kind(tmp_path):
    c, state = _client(tmp_path)
    _seed_model(state, tmp_path)
    page = c.get("/models", headers={"host": "localhost"})
    csrf = re.search(r'data-csrf="([^"]+)"', page.text).group(1)
    r = c.post("/wizard/select", data={"model": str(tmp_path / "models" / "Qwen3.8-27B-FP8"),
                                       "csrf": csrf},
               headers={"host": "localhost"})
    assert r.status_code == 200
    assert 'value="vllm" selected' in r.text       # предвыбор движка по kind
    assert 'name="port"' in r.text
    # в тестовой среде NVML нет: чекбоксов карт не будет — есть подсказка
    assert "No GPUs visible" in r.text or "Карт в снапшоте не видно" in r.text


def test_wizard_generates_valid_snippet(tmp_path):
    c, state = _client(tmp_path)
    _seed_model(state, tmp_path)
    page = c.get("/models", headers={"host": "localhost"})
    csrf = re.search(r'data-csrf="([^"]+)"', page.text).group(1)
    r = c.post("/wizard", data={"model": str(tmp_path / "models" / "Qwen3.8-27B-FP8"),
                                "engine": "vllm",
                                "unit": "vllm-ornith.service",
                                "port": "8110", "csrf": csrf},
               headers={"host": "localhost"})
    assert r.status_code == 200
    assert "vllm-ornith.service" in r.text
    assert "type: vllm" in r.text and "port: 8110" in r.text
    assert "sudo systemctl daemon-reload" in r.text
    # сниппет — валидный YAML для реестра
    m = re.search(r"<pre class=\"code-block wizard-snippet\"><code>(.*?)</code></pre>",
                  r.text, re.S)
    assert m
    import yaml as _yaml
    doc = _yaml.safe_load(m.group(1).replace("&amp;", "&").replace("&#34;", '"')
                          .replace("&gt;", ">").replace("&lt;", "<"))
    assert doc["services"][0]["port"] == 8110
    # аудит
    assert any(a["action"] == "wizard_generate" for a in state.db.audit_tail(5))


def test_wizard_unknown_model_redirects(tmp_path):
    c, state = _client(tmp_path)
    page = c.get("/models", headers={"host": "localhost"})
    csrf = re.search(r'data-csrf="([^"]+)"', page.text).group(1)
    r = c.post("/wizard/select", data={"model": "/no/such/model", "csrf": csrf},
               headers={"host": "localhost"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/wizard?err=")
