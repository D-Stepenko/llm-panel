"""Матрица GPU (этап 2, v0.2.1): план/факт/конфликты/сниппеты."""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from llm_panel.matrix import build_matrix  # noqa: E402
from llm_panel.registry import make_display_def  # noqa: E402

GPU_ROWS = [
    {"uuid": "GPU-aaa", "index": 0, "name": "V100-32GB", "mem_total_mb": 32768,
     "procs": [{"pid": 100, "name": "maya", "mem_mb": 1, "owner": "maya"}]},
    {"uuid": "GPU-bbb", "index": 1, "name": "V100-32GB", "mem_total_mb": 32768, "procs": []},
    {"uuid": "GPU-ccc", "index": 2, "name": "V100-16GB", "mem_total_mb": 16384, "procs": []},
]


def _svc(svc_id, unit, gpus=(), addressing="uuid", ram=False, port=None, extra=None):
    d = make_display_def(unit, scope="system")
    # make_display_def даёт id из имени юнита; подменяем нужные поля
    object.__setattr__(d, "id", svc_id)
    object.__setattr__(d, "name", svc_id.upper())
    object.__setattr__(d, "gpus", tuple(gpus))
    object.__setattr__(d, "cuda_addressing", addressing)
    object.__setattr__(d, "ram_exclusive", ram)
    object.__setattr__(d, "port", port)
    if extra:
        for k, v in extra.items():
            object.__setattr__(d, k, v)
    return d


def test_matrix_plan_fact_and_indices():
    defs = (
        _svc("maya", "maya-maya-m.service",
             ("GPU-aaa", "GPU-bbb", "GPU-ccc"), addressing="index"),
        _svc("llama", "llama-qwen38_gguf.service", ("GPU-ccc",), addressing="uuid"),
    )
    svc_rows = [
        {"id": "maya", "state": "ready",
         "gpu_actual": [{"uuid": "GPU-aaa"}, {"uuid": "GPU-bbb"}, {"uuid": "GPU-ccc"}],
         "listen": [8010]},
        {"id": "llama", "state": "stopped", "gpu_actual": [], "listen": []},
    ]
    m = build_matrix(defs, svc_rows, GPU_ROWS)

    maya = m["rows"][0]
    assert maya.planned_idx == (0, 1, 2)          # UUID → текущие индексы NVML
    assert maya.snippet == 'maya-maya-m.json → "gpu": [0,1,2]'
    assert maya.cells["GPU-aaa"] == "both"        # план + факт
    assert m["rows"][1].snippet == 'CUDA_VISIBLE_DEVICES="GPU-ccc"'
    assert m["rows"][1].cells["GPU-ccc"] == "plan"

    # конфликт: GPU-ccc запланирован двум сервисам (оба не ram_exclusive → warn)
    kinds = {(c.kind, c.level) for c in m["conflicts"]}
    assert ("gpu_planned", "warn") in kinds


def test_matrix_conflict_levels():
    defs = (
        _svc("a", "a.service", ("GPU-aaa",), ram=True, extra={"ram_exclusive": True}),
        _svc("b", "b.service", ("GPU-aaa",), ram=True, extra={"ram_exclusive": True}),
    )
    svc_rows = [
        {"id": "a", "state": "ready", "gpu_actual": [], "listen": []},
        {"id": "b", "state": "ready", "gpu_actual": [], "listen": []},
    ]
    m = build_matrix(defs, svc_rows, GPU_ROWS)
    kinds = {(c.kind, c.level) for c in m["conflicts"]}
    assert ("gpu_planned", "err") in kinds   # оба ram_exclusive и оба работают
    assert ("ram", "err") in kinds           # два ram_exclusive сервиса работают


def test_matrix_vllm_cvd_index_snippet():
    d = _svc("vllm", "vllm.service", ("GPU-aaa", "GPU-bbb"), addressing="index")
    m = build_matrix((d,), [{"id": "vllm", "state": "stopped", "gpu_actual": [],
                             "listen": []}], GPU_ROWS)
    assert m["rows"][0].snippet == 'CUDA_VISIBLE_DEVICES="0,1"'
    assert m["conflicts"] == []


def test_matrix_port_conflict_only_for_running():
    defs = (
        _svc("a", "a.service", port=8002),
        _svc("b", "b.service", port=8002),
    )
    rows = [
        {"id": "a", "state": "ready", "gpu_actual": [], "listen": []},
        {"id": "b", "state": "stopped", "gpu_actual": [], "listen": []},
    ]
    m = build_matrix(defs, rows, GPU_ROWS)
    assert all(c.kind != "port" for c in m["conflicts"])   # один из них не работает

    rows[1]["state"] = "ready"
    m = build_matrix(defs, rows, GPU_ROWS)
    assert any(c.kind == "port" and c.level == "warn" for c in m["conflicts"])


def test_matrix_no_nvml_and_empty():
    defs = (_svc("maya", "maya-maya-m.service"),)
    m = build_matrix(defs, [{"id": "maya", "state": "ready", "gpu_actual": [],
                             "listen": []}], None)
    assert m["gpus"] == [] and m["rows"][0].planned_idx == ()
    assert m["rows"][0].snippet == ""    # индексы неизвестны — сниппет не выдумываем
