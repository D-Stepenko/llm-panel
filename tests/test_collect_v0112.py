"""v0.1.12: фактические данные для юнитов без services.yaml.

Проверяется:
  * _listen_ports: psutil LISTEN у процесса и его детей, дедуп портов,
    обработка AccessDenied;
  * _gpu_attribution: NVML-процессы карты → сервис (pid_owner),
    proc["owner"] и g["services"];
  * _collect интеграция: юнит без health в yaml, но с слушающим портом,
    получает авто-TCP-health → состояние «запущен», порт и фактически
    занятые GPU в карточке (кейс Maya: active, 3 карты, сниппет без gpus).
"""

import asyncio
import collections
import datetime as dt_mod
import pathlib
import sys
import time

import psutil
import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from llm_panel.app import create_app
from llm_panel.config import load_config

from test_service_display import _make_config


# ------------------------------------------------ _listen_ports --------------

class _Conn:
    def __init__(self, status, port):
        LAddr = collections.namedtuple("LAddr", ["port"])
        self.status = status
        self.laddr = LAddr(port)


class _Proc:
    def __init__(self, conns=(), children=(), denied=False):
        self._conns = list(conns)
        self._children = list(children)
        self._denied = denied

    def children(self, recursive=True):
        return list(self._children)

    def connections(self, kind="inet"):
        if self._denied:
            raise psutil.AccessDenied(pid=1)
        return list(self._conns)


def test_listen_ports(monkeypatch):
    import llm_panel.collector as col_mod

    root = _Proc(
        conns=[_Conn(psutil.CONN_LISTEN, 8010), _Conn(psutil.CONN_ESTABLISHED, 8010)],
        children=[
            _Proc(conns=[_Conn(psutil.CONN_LISTEN, 8010)]),        # дубликат — дедуп
            _Proc(conns=[_Conn(psutil.CONN_LISTEN, 9090)]),        # движок-ребёнок
            _Proc(denied=True),                                    # нет доступа — пропустить
        ],
    )
    procs = {100: root}
    monkeypatch.setattr(psutil, "Process", lambda pid: procs[pid])
    assert col_mod._listen_ports(100) == [8010, 9090]


def test_listen_ports_missing_process(monkeypatch):
    import llm_panel.collector as col_mod

    def boom(pid):
        raise psutil.NoSuchProcess(pid=pid)

    monkeypatch.setattr(psutil, "Process", boom)
    assert col_mod._listen_ports(4242) == []


# ------------------------------------------------ _gpu_attribution -----------

def test_gpu_attribution():
    import llm_panel.collector as col_mod

    gpu_rows = [
        {"uuid": "U1", "index": 0, "name": "V100-32GB",
         "procs": [{"pid": 10, "name": "maya", "mem_mb": 1},
                   {"pid": 20, "name": "other", "mem_mb": 2}]},
        {"uuid": "U2", "index": 1, "name": "V100-16GB", "procs": []},
    ]
    actual = col_mod._gpu_attribution(gpu_rows, {10: "maya"})

    assert actual == {"maya": [{"uuid": "U1", "index": 0, "name": "V100-32GB"}]}
    assert gpu_rows[0]["services"] == ["maya"]
    assert gpu_rows[0]["procs"][0]["owner"] == "maya"
    assert gpu_rows[0]["procs"][1]["owner"] == ""    # pid не из панели
    assert gpu_rows[1]["services"] == []


def test_gpu_attribution_none_rows():
    import llm_panel.collector as col_mod

    assert col_mod._gpu_attribution(None, {10: "maya"}) == {}


# ------------------------------------------------ _pid_tree ------------------

class _TreeProc:
    def __init__(self, pid, children=()):
        self.pid = pid
        self._children = list(children)

    def children(self, recursive=True):
        # эмуляция recursive=True из psutil: все потомки (вглубь)
        out = []
        for c in self._children:
            out.append(c)
            out.extend(c.children(recursive=True))
        return out


def test_pid_tree_children_recursive(monkeypatch):
    import llm_panel.collector as col_mod

    grand = _TreeProc(51879)          # движок build/maya
    child = _TreeProc(51870, [grand])  # maya.py
    root = _TreeProc(51800, [child])   # обёртка maya.sh (MainPID)
    monkeypatch.setattr(psutil, "Process", lambda pid: root)
    assert col_mod._pid_tree(51800) == [51800, 51870, 51879]


def test_pid_tree_missing_process(monkeypatch):
    import llm_panel.collector as col_mod

    def boom(pid):
        raise psutil.NoSuchProcess(pid=pid)

    monkeypatch.setattr(psutil, "Process", boom)
    assert col_mod._pid_tree(4242) == [4242]   # процесс умер — остаётся сам pid


# ------------------------------------------------ _collect интеграция --------

def test_collect_auto_health_and_gpu_actual(tmp_path, monkeypatch):
    """Юнит БЕЗ health в yaml (как Maya): состояние по авто-TCP-health
    слушающего порта, порт и фактически занятые карты — в карточке."""
    import llm_panel.collector as col_mod
    import llm_panel.health as health_mod
    from llm_panel.systemd_units import UnitInfo

    cfg = load_config(_make_config(tmp_path))
    svc_file = cfg.services_file
    data = yaml.safe_load(open(svc_file, encoding="utf-8"))
    data["services"].append({"id": "maya", "name": "Maya", "type": "generic",
                             "unit": "maya-maya-m.service"})
    open(svc_file, "w", encoding="utf-8").write(
        yaml.safe_dump(data, allow_unicode=True))
    app = create_app(cfg)
    state = app.state.panel

    now = time.time()

    def fake_show(unit, timeout=5.0, scope="system"):
        if unit == "maya-maya-m.service":
            return UnitInfo(active="active", sub="running", result="success",
                            pid=51875,
                            since=dt_mod.datetime.fromtimestamp(
                                now - 60, dt_mod.timezone.utc).isoformat(),
                            nrestarts=0, unit_file_state="disabled",
                            load_state="loaded")
        return None

    monkeypatch.setattr(col_mod, "show_unit", fake_show)
    monkeypatch.setattr(col_mod, "_listen_ports", lambda pid: [9090, 8010])

    def fake_gpu_rows(metrics):
        return [
            {"uuid": "GPU-U1", "index": 0, "name": "Tesla V100-32GB", "mem_pct": 50,
             "procs": [{"pid": 51875, "name": "build/maya", "mem_mb": 12000}]},
            {"uuid": "GPU-U2", "index": 1, "name": "Tesla V100-32GB", "mem_pct": 30,
             "procs": [{"pid": 4242, "name": "alien", "mem_mb": 100}]},
        ]

    monkeypatch.setattr(col_mod, "build_gpu_rows", fake_gpu_rows)

    async def fake_run_health(hdef):
        assert hdef.kind == "tcp" and hdef.port == 8010   # первый порт из listen
        return health_mod.HealthResult(ok=True, detail="TCP 127.0.0.1:8010 доступен")

    monkeypatch.setattr(health_mod, "run_health", fake_run_health)

    state.collector._last_health_check = 0.0   # health-блок выполняется в этом цикле
    snap = asyncio.run(state.collector._collect())
    row = next(r for r in snap.services if r["id"] == "maya")

    # авто-health: активный юнит с LISTEN больше не «неизвестно»
    assert row["state"] == "ready"
    assert row["health_ok"] is True
    assert row["listen"] == [8010, 9090]     # отсортированы, лишние отброшены
    assert row["gpu_actual"] == [{"uuid": "GPU-U1", "index": 0, "name": "Tesla V100-32GB"}]

    g0, g1 = snap.gpus
    assert g0["services"] == ["maya"]                 # фактический владелец
    assert g0["procs"][0]["owner"] == "maya"
    assert g1["services"] == []                        # чужой pid не атрибутируется
    # запланированной привязки у maya нет (сниппет без gpus)
    assert row["gpus"] == []
    assert all("maya" not in g["assigned"] for g in snap.gpus)


def test_collect_gpu_actual_via_child(tmp_path, monkeypatch):
    """Кейс Maya (v0.2.1): MainPID юнита — обёртка (maya.sh), карты держит
    движок-ПОТОМОК (build/maya). gpu_actual должен атрибутироваться и по нему."""
    import llm_panel.collector as col_mod
    import llm_panel.health as health_mod
    from llm_panel.systemd_units import UnitInfo

    cfg = load_config(_make_config(tmp_path))
    svc_file = cfg.services_file
    data = yaml.safe_load(open(svc_file, encoding="utf-8"))
    data["services"].append({"id": "maya", "name": "Maya", "type": "generic",
                             "unit": "maya-maya-m.service"})
    open(svc_file, "w", encoding="utf-8").write(
        yaml.safe_dump(data, allow_unicode=True))
    app = create_app(cfg)
    state = app.state.panel

    now = time.time()

    def fake_show(unit, timeout=5.0, scope="system"):
        if unit == "maya-maya-m.service":
            return UnitInfo(active="active", sub="running", result="success",
                            pid=51800,   # MainPID — обёртка maya.sh
                            since=dt_mod.datetime.fromtimestamp(
                                now - 60, dt_mod.timezone.utc).isoformat(),
                            nrestarts=0, unit_file_state="disabled",
                            load_state="loaded")
        return None

    monkeypatch.setattr(col_mod, "show_unit", fake_show)
    monkeypatch.setattr(col_mod, "_listen_ports", lambda pid: [8010])

    # дерево процессов: MainPID 51800 → 51870 (maya.py) → 51879 (build/maya,
    # именно он держит карты по NVML)
    grand = _TreeProc(51879)
    child = _TreeProc(51870, [grand])
    wrapper = _TreeProc(51800, [child])
    monkeypatch.setattr(psutil, "Process", lambda pid: wrapper)

    def fake_gpu_rows(metrics):
        return [
            {"uuid": "GPU-U1", "index": 0, "name": "Tesla V100-32GB", "mem_pct": 50,
             "procs": [{"pid": 51879, "name": "build/maya", "mem_mb": 12000}]},
            {"uuid": "GPU-U2", "index": 1, "name": "Tesla V100-32GB", "mem_pct": 30,
             "procs": []},
        ]

    monkeypatch.setattr(col_mod, "build_gpu_rows", fake_gpu_rows)

    async def fake_run_health(hdef):
        return health_mod.HealthResult(ok=True, detail="ok")

    monkeypatch.setattr(health_mod, "run_health", fake_run_health)

    state.collector._last_health_check = 0.0
    snap = asyncio.run(state.collector._collect())
    row = next(r for r in snap.services if r["id"] == "maya")

    # движок-ребёнок атрибутирован юниту → «факт» виден в матрице/карточке
    assert row["gpu_actual"] == [{"uuid": "GPU-U1", "index": 0,
                                  "name": "Tesla V100-32GB"}]
    assert snap.gpus[0]["services"] == ["maya"]


def test_collect_auto_health_cleared_when_not_listening(tmp_path, monkeypatch):
    """Если процесс перестал слушать порт — авто-health снимается."""
    import llm_panel.collector as col_mod
    import llm_panel.health as health_mod
    from llm_panel.systemd_units import UnitInfo

    cfg = load_config(_make_config(tmp_path))
    svc_file = cfg.services_file
    data = yaml.safe_load(open(svc_file, encoding="utf-8"))
    data["services"].append({"id": "maya", "name": "Maya", "type": "generic",
                             "unit": "maya-maya-m.service"})
    open(svc_file, "w", encoding="utf-8").write(
        yaml.safe_dump(data, allow_unicode=True))
    app = create_app(cfg)
    state = app.state.panel
    col = state.collector

    async def fake_ok(hdef):
        return health_mod.HealthResult(ok=True, detail="ok")

    monkeypatch.setattr(health_mod, "run_health", fake_ok)
    now = time.time()
    maya_info = UnitInfo(active="active", sub="running", result="success", pid=51875,
                         since=dt_mod.datetime.fromtimestamp(
                             now - 60, dt_mod.timezone.utc).isoformat(),
                         nrestarts=0, unit_file_state="disabled", load_state="loaded")

    def fake_show(unit, timeout=5.0, scope="system"):
        return maya_info if unit == "maya-maya-m.service" else None

    monkeypatch.setattr(col_mod, "show_unit", fake_show)
    monkeypatch.setattr(col_mod, "_listen_ports", lambda pid: [8010])

    col._last_health_check = 0.0
    asyncio.run(col._collect())
    assert "maya-maya-m.service" in col._auto_health_cache

    # вторая проверка: порт больше не слушается
    col._last_health_check = 0.0
    monkeypatch.setattr(col_mod, "_listen_ports", lambda pid: [])
    asyncio.run(col._collect())
    assert "maya-maya-m.service" not in col._auto_health_cache