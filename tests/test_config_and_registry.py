"""Тесты загрузки конфигурации (ТЗ §8) и реестра сервисов (ТЗ §4.1)."""

import pytest
import yaml

from llm_panel.config import load_config, ConfigError
from llm_panel.registry import load_services


def write(tmp_path, data: dict) -> str:
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return str(p)


BASE = {
    "panel": {
        "listen_host": "127.0.0.1",
        "listen_port": 8443,
        "trusted_hosts": ["llm.local"],
        "cookie_secure": True,
        "dev_mode": True,
    },
    "data": {
        "data_dir": "/var/lib/llm-panel",
        "db_path": "/var/lib/llm-panel/panel.db",
        "services_file": "/etc/llm-panel/services.yaml",
    },
    "systemd": {"helper_path": "/usr/local/sbin/llm-panel-systemd"},
}


def test_valid_config(tmp_path):
    cfg = load_config(write(tmp_path, BASE))
    assert cfg.listen_host == "127.0.0.1"
    assert cfg.trusted_hosts == ("llm.local",)
    assert cfg.lockout_failures == 5


def test_non_loopback_listen_rejected(tmp_path):
    data = dict(BASE)
    data["panel"] = {**BASE["panel"], "listen_host": "0.0.0.0"}
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, data))


def test_lockout_below_five_rejected(tmp_path):
    # ТЗ §6.1: блокировка после 5 ошибок; ослабление запрещено
    data = dict(BASE)
    data["panel"] = {**BASE["panel"], "lockout_failures": 3}
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, data))


def test_bad_port_rejected(tmp_path):
    data = dict(BASE)
    data["panel"] = {**BASE["panel"], "listen_port": 99999}
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, data))


# --- реестр сервисов -------------------------------------------------------

BASE_SVC = {
    "services": [
        {
            "id": "vllm",
            "name": "vLLM",
            "type": "vllm",
            "unit": "vllm.service",
            "managed": "adopted",
            "edit_mode": "manage_only",
            "port": 8000,
            "health": {"kind": "http", "url": "http://127.0.0.1:8000/health"},
        }
    ]
}


def write_svc(tmp_path, data) -> str:
    p = tmp_path / "services.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return str(p)


def test_load_services_ok(tmp_path):
    reg = load_services(write_svc(tmp_path, BASE_SVC))
    assert reg.by_id("vllm").unit == "vllm.service"
    assert reg.by_id("vllm").managed == "adopted"
    assert reg.by_id("vllm").edit_mode == "manage_only"


def test_bad_slug_rejected(tmp_path):
    data = {"services": [{**BASE_SVC["services"][0], "id": "bad id"}]}
    with pytest.raises(ValueError):
        load_services(write_svc(tmp_path, data))


def test_unit_without_suffix_rejected(tmp_path):
    data = {"services": [{**BASE_SVC["services"][0], "unit": "vllm"}]}
    with pytest.raises(ValueError):
        load_services(write_svc(tmp_path, data))


def test_duplicate_unit_rejected(tmp_path):
    data = {"services": [BASE_SVC["services"][0], {**BASE_SVC["services"][0], "id": "vllm2"}]}
    with pytest.raises(ValueError):
        load_services(write_svc(tmp_path, data))


def test_external_health_url_rejected(tmp_path):
    # ТЗ §4.1: health URL только 127.0.0.1
    data = {"services": [{**BASE_SVC["services"][0],
                          "health": {"kind": "http", "url": "http://0.0.0.0:8000/health"}}]}
    with pytest.raises(ValueError):
        load_services(write_svc(tmp_path, data))


def test_gpu_index_instead_of_uuid_rejected(tmp_path):
    # ТЗ §2/§4.1: сохранять только полные UUID, не индексы
    data = {"services": [{**BASE_SVC["services"][0], "gpus": [0, 1]}]}
    with pytest.raises(ValueError):
        load_services(write_svc(tmp_path, data))


def test_gpu_uuid_ok(tmp_path):
    data = {"services": [{**BASE_SVC["services"][0], "gpus": ["GPU-2c1e40a3-0000-0000-0000-000000000000"]}]}
    reg = load_services(write_svc(tmp_path, data))
    assert reg.by_id("vllm").gpus == ("GPU-2c1e40a3-0000-0000-0000-000000000000",)


def test_created_service_forbidden_dropin(tmp_path):
    data = {"services": [{**BASE_SVC["services"][0], "managed": "created", "edit_mode": "dropin"}]}
    with pytest.raises(ValueError):
        load_services(write_svc(tmp_path, data))


def test_stop_signal_sigint_ok_for_maya(tmp_path):
    data = {"services": [{**BASE_SVC["services"][0], "id": "maya", "stop_signal": "SIGINT"}]}
    reg = load_services(write_svc(tmp_path, data), startup_timeout_default=900)
    assert reg.by_id("maya").stop_signal == "SIGINT"


def test_stop_signal_sigkill_rejected(tmp_path):
    data = {"services": [{**BASE_SVC["services"][0], "stop_signal": "SIGKILL"}]}
    with pytest.raises(ValueError):
        load_services(write_svc(tmp_path, data))


def test_cuda_addressing_invalid_rejected(tmp_path):
    data = {"services": [{**BASE_SVC["services"][0], "cuda_addressing": "pci"}]}   # допустимы только uuid|index
    with pytest.raises(ValueError):
        load_services(write_svc(tmp_path, data))


def test_cuda_addressing_index_ok_for_vllm_convention(tmp_path):
    # Конвенция станции (владелец, 2026-10-27): vLLM — по индексам, llama.cpp — по UUID
    data = {"services": [{**BASE_SVC["services"][0], "cuda_addressing": "index"}]}
    reg = load_services(write_svc(tmp_path, data))
    assert reg.by_id("vllm").cuda_addressing == "index"


def test_cuda_addressing_default_uuid(tmp_path):
    reg = load_services(write_svc(tmp_path, BASE_SVC))
    assert reg.by_id("vllm").cuda_addressing == "uuid"
