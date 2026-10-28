"""Тесты helper: валидация action/unit/allowlist/лимитов журнала (ТЗ §6.3, §10 п.13).

Helper запускается как обычный процесс с --dry-run (разрешён только не от root,
только при тестировании) и временным allowlist через LLM_PANEL_ALLOWLIST_TEST
(env-правка игнорируется от root, поэтому через sudo smuggle невозможен).
"""

import json
import os
import subprocess
import sys
import pathlib

HELPER = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "helper" / "llm-panel-systemd"

ALLOWLIST = {
    "units": {
        "vllm.service": {
            "actions": ["start", "stop", "restart", "enable", "disable", "status", "journal", "journal-follow"],
        },
        "gpu-power-limit.service": {"actions": ["status", "journal"]},
    }
}


def run_helper(args: list[str], allowlist: dict | None = None):
    env = dict(os.environ)
    if allowlist is None:
        allowlist = ALLOWLIST
    env["LLM_PANEL_ALLOWLIST_TEST"] = _write_allowlist(allowlist)
    return subprocess.run(
        [sys.executable, str(HELPER), "--dry-run", *args],
        capture_output=True, text=True, env=env, timeout=30,
    )


def _write_allowlist(data: dict) -> str:
    import tempfile
    tmp = tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    )
    json.dump(data, tmp, ensure_ascii=False)
    tmp.close()
    return tmp.name


def test_valid_plan_start():
    r = run_helper(["start", "vllm.service"])
    assert r.returncode == 0
    plan = json.loads(r.stdout)
    assert plan["argv"] == ["systemctl", "start", "--no-ask-password", "vllm.service"]


def test_unknown_unit_denied_by_default():
    r = run_helper(["start", "stranger.service"])
    assert r.returncode == 3  # безопасный дефолт — отказ
    assert "allowlist" in r.stderr


def test_action_not_allowed_for_unit():
    r = run_helper(["stop", "gpu-power-limit.service"])
    assert r.returncode == 3
    assert "не разрешено" in r.stderr


def test_unit_with_slash_rejected():
    r = run_helper(["start", "../etc/passwd.service"])
    assert r.returncode == 2


def test_unit_with_control_chars_rejected():
    r = run_helper(["start", "vllm\n.service"])
    assert r.returncode == 2


def test_unit_without_service_suffix_rejected():
    r = run_helper(["start", "vllm"])
    assert r.returncode == 2


def test_journal_lines_over_limit_rejected():
    r = run_helper(["journal", "vllm.service", "5001"])
    assert r.returncode == 2


def test_journal_lines_nonnumeric_rejected():
    r = run_helper(["journal", "vllm.service", "abc"])
    assert r.returncode == 2


def test_journal_lines_valid_plan():
    r = run_helper(["journal", "vllm.service", "200"])
    assert r.returncode == 0
    plan = json.loads(r.stdout)
    # фиксированные аргументы: браузер не задаёт флаги journalctl (ТЗ §5.4)
    assert plan["argv"] == ["journalctl", "--unit", "vllm.service", "--lines", "200", "--no-pager"]


def test_unknown_action_rejected():
    r = run_helper(["exec", "vllm.service"])
    assert r.returncode == 2


# ------------------------------- --no-block (самоперезапуск панели, 0.1.6) ----

def test_no_block_accepted_for_restart():
    r = run_helper(["restart", "vllm.service", "--no-block"])
    assert r.returncode == 0
    plan = json.loads(r.stdout)
    assert plan["argv"] == ["systemctl", "restart", "--no-ask-password",
                            "--no-block", "vllm.service"]


def test_no_block_without_flag_unchanged():
    r = run_helper(["restart", "vllm.service"])
    assert r.returncode == 0
    plan = json.loads(r.stdout)
    assert plan["argv"] == ["systemctl", "restart", "--no-ask-password", "vllm.service"]


def test_no_block_double_extra_rejected():
    r = run_helper(["start", "vllm.service", "--no-block", "--something"])
    assert r.returncode == 2


def test_no_block_not_allowed_for_status():
    r = run_helper(["status", "vllm.service", "--no-block"])
    assert r.returncode == 2


def test_status_with_extra_rejected():
    r = run_helper(["status", "vllm.service", "anything"])
    assert r.returncode == 2


def test_broken_allowlist_is_refused():
    import tempfile
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    tmp.write("{ broken json")
    tmp.close()
    env = dict(os.environ)
    env["LLM_PANEL_ALLOWLIST_TEST"] = tmp.name
    r = subprocess.run(
        [sys.executable, str(HELPER), "--dry-run", "start", "vllm.service"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert r.returncode == 3


def test_missing_allowlist_is_refused():
    r = run_helper(["start", "vllm.service"],
                   allowlist={"units": {}})  # пустой allowlist: юнитов нет → отказ
    assert r.returncode == 3
