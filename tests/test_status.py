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
