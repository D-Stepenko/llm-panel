"""Тесты расчёта состояний (ТЗ §4.2)."""

import llm_panel.status as st
from llm_panel.systemd_units import UnitInfo


def ui(active, sub="", result="success", pid=0, since="", nrestarts=0, ufs="enabled"):
    return UnitInfo(active=active, sub=sub, result=result, pid=pid,
                    since=since, nrestarts=nrestarts, unit_file_state=ufs)


READY = type("H", (), {"ok": True, "detail": "HTTP 200"})()
FAIL = type("H", (), {"ok": False, "detail": "нет соединения"})()


def test_failed_when_systemd_failed():
    d = st.compute_display(ui("failed"), READY, (), None, uptime_s=10, startup_timeout=600,
                           last_ready_ts=None, now_ts=1)
    assert d.state is st.DisplayState.FAILED


def test_inactive_with_bad_result_is_failed():
    d = st.compute_display(ui("inactive", result="signal"), None, (), None, uptime_s=0,
                           startup_timeout=600, last_ready_ts=None, now_ts=1)
    assert d.state is st.DisplayState.FAILED


def test_stopped():
    d = st.compute_display(ui("inactive"), None, (), None, uptime_s=None, startup_timeout=600,
                           last_ready_ts=None, now_ts=1)
    assert d.state is st.DisplayState.STOPPED


def test_starting_then_readiness_timeout():
    young = st.compute_display(ui("active"), FAIL, (), None, uptime_s=10, startup_timeout=600,
                               last_ready_ts=None, now_ts=1)
    assert young.state is st.DisplayState.STARTING
    old = st.compute_display(ui("active"), FAIL, (), None, uptime_s=700, startup_timeout=600,
                             last_ready_ts=None, now_ts=1)
    assert old.state is st.DisplayState.READINESS_TIMEOUT  # процесс не убивается


def test_ready_and_unhealthy():
    d = st.compute_display(ui("active"), READY, (), None, uptime_s=100, startup_timeout=600,
                           last_ready_ts=None, now_ts=1)
    assert d.state is st.DisplayState.READY
    d2 = st.compute_display(ui("active"), FAIL, (), None, uptime_s=700, startup_timeout=600,
                            last_ready_ts=123.0, now_ts=200)
    assert d2.state is st.DisplayState.UNHEALTHY


def test_missing_gpu_blocks_start():
    # сохранённая привязка по UUID, карта отсутствует в NVML (ТЗ §4.2, §9)
    d = st.compute_display(ui("active"), READY, ("GPU-abc",), ["GPU-xyz"],
                           uptime_s=1, startup_timeout=600, last_ready_ts=None, now_ts=1)
    assert d.state is st.DisplayState.MISSING_GPU


def test_no_nvml_data_does_not_claim_missing():
    d = st.compute_display(ui("active"), READY, ("GPU-abc",), None,
                           uptime_s=1, startup_timeout=600, last_ready_ts=None, now_ts=1)
    assert d.state is not st.DisplayState.MISSING_GPU


def test_unknown_when_no_health_configured():
    d = st.compute_display(ui("active"), None, (), None, uptime_s=100, startup_timeout=600,
                           last_ready_ts=None, now_ts=1)
    assert d.state is st.DisplayState.UNKNOWN  # готовность не проверяется — не подменять


def test_unit_unavailable_is_unknown():
    d = st.compute_display(None, None, (), None, uptime_s=None, startup_timeout=600,
                           last_ready_ts=None, now_ts=1)
    assert d.state is st.DisplayState.UNKNOWN


# ------------------------------------- uptime остановленного юнита -----------

def test_uptime_s_stopped_unit_is_none():
    """Регрессия (наблюдение станции 2026-10-28): у inactive/dead юнита
    ExecMainStartTimestamp ещё заполнен (юнит не выгружен), и время работы
    продолжало расти. Считаем только пока процесс идёт."""
    import datetime as dt_mod
    from llm_panel.collector import _uptime_s
    from llm_panel.systemd_units import UnitInfo

    now = 1_700_000_000.0

    def info(active, since_s):
        return UnitInfo(active=active, sub="dead", result="success", pid=1,
                        since=dt_mod.datetime.fromtimestamp(now - since_s,
                                                            dt_mod.timezone.utc).isoformat(),
                        nrestarts=0, unit_file_state="")

    assert _uptime_s(info("active", 60), now) == 60.0
    assert _uptime_s(info("activating", 10), now) == 10.0
    assert _uptime_s(info("deactivating", 30), now) == 30.0
    assert _uptime_s(info("inactive", 3600), now) is None   # остановлен: не растёт
    assert _uptime_s(info("failed", 3600), now) is None
    assert _uptime_s(None, now) is None
    no_since = UnitInfo(active="active", sub="running", result="success", pid=1,
                        since="", nrestarts=0, unit_file_state="")
    assert _uptime_s(no_since, now) is None
