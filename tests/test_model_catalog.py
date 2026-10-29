"""Каталог моделей (этап 3, v0.3.0): скан gguf/safetensors, ограничения, роут."""

import json
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from llm_panel.app import create_app
from llm_panel.config import load_config
from llm_panel.model_catalog import human_bytes, scan_models

from test_service_display import _make_config


def _gguf(name="qwen3-27b-Q4_K_M.gguf", size=1024 * 1024):
    return name, b"\x00" * size


def test_scan_gguf_single_and_quant(tmp_path):
    d = tmp_path / "models" / "qwen3-27b-IQ4_XS"
    d.mkdir(parents=True)
    name, blob = _gguf("qwen3-27b-Q4_K_M.gguf", 4096)
    (d / name).write_bytes(blob)
    (d / "tokenizer.json").write_text("{}", encoding="utf-8")

    m = scan_models([str(tmp_path / "models")])
    assert len(m["models"]) == 1
    e = m["models"][0]
    assert e.kind == "gguf" and e.quant == "Q4_K_M" and e.family == "qwen3"
    assert e.size_bytes == 4096 and e.n_files == 1
    assert e.name == "qwen3-27b-Q4_K_M"


def test_scan_gguf_shards_grouped(tmp_path):
    d = tmp_path / "models" / "llama-8b"
    d.mkdir(parents=True)
    for n in ("llama-8b-00001-of-00003.gguf", "llama-8b-00002-of-00003.gguf",
              "llama-8b-00003-of-00003.gguf"):
        (d / n).write_bytes(b"x" * 100)
    m = scan_models([str(tmp_path / "models")])
    assert len(m["models"]) == 1
    e = m["models"][0]
    assert e.name == "llama-8b" and e.n_files == 3 and e.size_bytes == 300


def test_scan_safetensors_config(tmp_path):
    d = tmp_path / "models" / "Qwen3.8-27B-FP8"
    d.mkdir(parents=True)
    (d / "config.json").write_text(json.dumps({
        "model_type": "qwen3", "torch_dtype": "bfloat16",
        "quantization_config": {"method": "fp8"},
    }), encoding="utf-8")
    (d / "model.safetensors").write_bytes(b"y" * 2048)
    (d / "tokenizer.json").write_bytes(b"{}")
    m = scan_models([str(tmp_path / "models")])
    e = m["models"][0]
    assert e.kind == "safetensors" and e.model_type == "qwen3"
    assert e.dtype == "bfloat16"
    assert e.quant == "FP8"          # из имени каталога
    expect = sum((d / f).stat().st_size for f in ("config.json", "model.safetensors", "tokenizer.json"))
    assert e.size_bytes == expect and e.n_files == 3


def test_scan_limits_and_missing_root(tmp_path):
    (tmp_path / "models").mkdir()
    deep = tmp_path / "models" / "a" / "b" / "c" / "d"   # глубина 4 > 3
    deep.mkdir(parents=True)
    (deep / "deep.gguf").write_bytes(b"z" * 10)
    near = tmp_path / "models" / "a" / "b" / "c"
    (near / "near.gguf").write_bytes(b"z" * 20)

    m = scan_models([str(tmp_path / "models"), str(tmp_path / "no-such")])
    assert [e.name for e in m["models"]] == ["near"]
    missing = [r for r in m["roots"] if not r["exists"]]
    assert missing and missing[0]["path"].endswith("no-such")


def test_human_bytes():
    assert human_bytes(None) == ""
    assert human_bytes(0) == "0 Б"
    assert human_bytes(1024 * 1024) == "1.0 МБ"


# ------------------------------------------------ роут -----------------------

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


def test_models_page_lists_entries(tmp_path):
    c = _client(tmp_path)
    state = c.app.state.panel
    d = tmp_path / "m"
    d.mkdir()
    (d / "qwen3-Q8_0.gguf").write_bytes(b"x" * 512)
    state.model_roots = [str(d)]
    state.models_scan = None
    page = c.get("/models", headers={"host": "localhost"})
    assert page.status_code == 200
    assert "qwen3-Q8_0" in page.text
    assert "GGUF" in page.text


def test_models_refresh_requires_csrf(tmp_path):
    c = _client(tmp_path)
    r = c.post("/models/refresh", data={"csrf": "wrong"},
               headers={"host": "localhost"})
    assert r.status_code == 403
