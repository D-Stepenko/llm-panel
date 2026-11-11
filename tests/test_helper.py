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
            "actions": ["start", "stop", "restart", "enable", "disable", "reset-failed",
                        "status", "journal", "journal-follow"],
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


# ------------------------------------------------- reset-failed (v0.3.18) ----

def test_reset_failed_plan():
    """reset-failed разрешён как обычное per-unit действие; extra не принимает."""
    r = run_helper(["reset-failed", "vllm.service"])
    assert r.returncode == 0
    plan = json.loads(r.stdout)
    assert plan["argv"] == ["systemctl", "reset-failed", "--no-ask-password", "vllm.service"]


def test_reset_failed_with_extra_rejected():
    assert run_helper(["reset-failed", "vllm.service", "--no-block"]).returncode == 2
    assert run_helper(["reset-failed", "vllm.service", "anything"]).returncode == 2


def test_reset_failed_not_allowed_for_unit():
    r = run_helper(["reset-failed", "gpu-power-limit.service"])
    assert r.returncode == 3
    assert "не разрешено" in r.stderr


def test_reset_failed_unknown_unit_denied():
    r = run_helper(["reset-failed", "stranger.service"])
    assert r.returncode == 3


# --------------------------------------------- daemon-reload (v0.3.18) -------

def test_daemon_reload_allowed_plan():
    """daemon-reload разрешён ключом "daemon_reload": true; юнит не участвует."""
    wl = {**ALLOWLIST, "daemon_reload": True}
    r = run_helper(["daemon-reload"], allowlist=wl)
    assert r.returncode == 0
    plan = json.loads(r.stdout)
    assert plan["argv"] == ["systemctl", "daemon-reload", "--no-ask-password"]
    assert plan["action"] == "daemon-reload"


def test_daemon_reload_denied_by_default():
    """Ключа нет / false — отказ (код 3, безопасный дефолт)."""
    assert run_helper(["daemon-reload"]).returncode == 3
    wl = {**ALLOWLIST, "daemon_reload": False}
    assert run_helper(["daemon-reload"], allowlist=wl).returncode == 3


def test_daemon_reload_takes_no_arguments():
    """daemon-reload — менеджер-целая операция: любой аргумент = ошибка (код 2)."""
    wl = {**ALLOWLIST, "daemon_reload": True}
    assert run_helper(["daemon-reload", "vllm.service"], allowlist=wl).returncode == 2
    assert run_helper(["daemon-reload", "extra"], allowlist=wl).returncode == 2


# -------------------------- глобальный ключ reset_failed (v0.3.19) -----------

def test_reset_failed_global_key_allows_without_per_unit():
    """Ключ "reset_failed": true разрешает reset-failed ЛЮБОМУ allowlist-юниту
    (текущему и будущему) — без перечисления в каждом actions."""
    wl = {"units": {"vllm.service": {"actions": ["status", "journal"]}},
          "reset_failed": True}
    r = run_helper(["reset-failed", "vllm.service"], allowlist=wl)
    assert r.returncode == 0
    plan = json.loads(r.stdout)
    assert plan["argv"] == ["systemctl", "reset-failed", "--no-ask-password", "vllm.service"]


def test_reset_failed_without_global_key_still_per_unit():
    """Без ключа (или false) — только юнитам с явным вхождением в actions."""
    wl = {"units": {"vllm.service": {"actions": ["status", "journal"]}}}
    assert run_helper(["reset-failed", "vllm.service"], allowlist=wl).returncode == 3
    wl2 = {**wl, "reset_failed": False}
    assert run_helper(["reset-failed", "vllm.service"], allowlist=wl2).returncode == 3


# -------------------------- unit-cat / unit-write (v0.3.19) ------------------

UNIT_FILE = """[Unit]
Description=Test unit

[Service]
ExecStart=/bin/true

[Install]
WantedBy=multi-user.target
"""

# Для real-ранов unit-cat/unit-write: действия должны быть явно разрешены юниту.
UNITS_ALLOWLIST = {
    "units": {
        "vllm.service": {"actions": ["status", "journal", "unit-cat", "unit-write"]},
    }
}


def test_unit_cat_dry_run_plan():
    r = run_helper(["unit-cat", "vllm.service"], allowlist=UNITS_ALLOWLIST)
    assert r.returncode == 0
    plan = json.loads(r.stdout)
    assert plan["argv"] == ["cat", os.path.join("/etc/systemd/system", "vllm.service")]


def test_unit_write_dry_run_plan():
    r = run_helper(["unit-write", "vllm.service"], allowlist=UNITS_ALLOWLIST)
    assert r.returncode == 0
    plan = json.loads(r.stdout)
    assert plan["argv"] == ["write", os.path.join("/etc/systemd/system", "vllm.service"),
                            "(stdin)"]


def test_unit_actions_not_allowed_without_per_unit():
    """unit-cat/unit-write — строго per-unit: без явного вхождения — отказ."""
    assert run_helper(["unit-cat", "gpu-power-limit.service"]).returncode == 3
    assert run_helper(["unit-write", "gpu-power-limit.service"]).returncode == 3
    assert run_helper(["unit-cat", "stranger.service"]).returncode == 3


def test_unit_actions_extra_rejected():
    assert run_helper(["unit-cat", "vllm.service", "x"],
                      allowlist=UNITS_ALLOWLIST).returncode == 2
    assert run_helper(["unit-write", "vllm.service", "x"],
                      allowlist=UNITS_ALLOWLIST).returncode == 2


def _real_run(args: list[str], allowlist: dict, units_dir, stdin: str | None = None):
    """Реальный запуск helper (не dry-run) против тестовых каталогов.

    Разрешено только при euid != 0 с ОБОИМИ тестовыми env (см. helper)."""
    env = dict(os.environ)
    env["LLM_PANEL_ALLOWLIST_TEST"] = _write_allowlist(allowlist)
    env["LLM_PANEL_UNITS_DIR_TEST"] = str(units_dir)
    return subprocess.run(
        [sys.executable, str(HELPER), *args],
        input=stdin, capture_output=True, text=True, env=env, timeout=30,
    )


def test_unit_cat_real_run(tmp_path):
    units = tmp_path / "units"
    units.mkdir()
    (units / "vllm.service").write_text(UNIT_FILE, encoding="utf-8")
    r = _real_run(["unit-cat", "vllm.service"], UNITS_ALLOWLIST, units)
    assert r.returncode == 0, r.stderr
    assert "[Service]" in r.stdout and "ExecStart=/bin/true" in r.stdout


def test_unit_cat_missing_file(tmp_path):
    units = tmp_path / "units"
    units.mkdir()
    r = _real_run(["unit-cat", "vllm.service"], UNITS_ALLOWLIST, units)
    assert r.returncode == 4
    assert "не найден" in r.stderr


def test_unit_write_real_run_roundtrip(tmp_path):
    units = tmp_path / "units"
    units.mkdir()
    p = units / "vllm.service"
    p.write_text(UNIT_FILE, encoding="utf-8")
    new = UNIT_FILE.replace("/bin/true", "/bin/echo")
    r = _real_run(["unit-write", "vllm.service"], UNITS_ALLOWLIST, units, stdin=new)
    assert r.returncode == 0, r.stderr
    assert "/bin/echo" in p.read_text(encoding="utf-8")
    backups = list((units / ".panel-backups").glob("vllm.service.*.bak"))
    assert len(backups) == 1
    assert "/bin/true" in backups[0].read_text(encoding="utf-8")


def test_unit_write_rejects_creating_new_unit(tmp_path):
    """Создание НОВЫХ юнитов через панель запрещено (безопасный дефолт)."""
    units = tmp_path / "units"
    units.mkdir()
    r = _real_run(["unit-write", "vllm.service"], UNITS_ALLOWLIST, units, stdin=UNIT_FILE)
    assert r.returncode == 4
    assert not (units / "vllm.service").exists()


def test_unit_write_rejects_invalid_content(tmp_path):
    units = tmp_path / "units"
    units.mkdir()
    (units / "vllm.service").write_text(UNIT_FILE, encoding="utf-8")
    r = _real_run(["unit-write", "vllm.service"], UNITS_ALLOWLIST, units,
                  stdin="no sections here")
    assert r.returncode == 2


def test_unit_write_rejects_empty(tmp_path):
    units = tmp_path / "units"
    units.mkdir()
    (units / "vllm.service").write_text(UNIT_FILE, encoding="utf-8")
    r = _real_run(["unit-write", "vllm.service"], UNITS_ALLOWLIST, units, stdin="   ")
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


# --------------------------------------------------------------- power (v0.3.12)
def test_power_allowed_plan():
    """power разрешён секцией allowlist: план systemctl poweroff/reboot."""
    wl = {**ALLOWLIST, "power": {"poweroff": True, "reboot": True}}
    r = run_helper(["power", "poweroff"], allowlist=wl)
    assert r.returncode == 0
    plan = json.loads(r.stdout)
    assert plan["argv"] == ["systemctl", "poweroff", "--no-ask-password"]
    r = run_helper(["power", "reboot"], allowlist=wl)
    assert r.returncode == 0
    assert json.loads(r.stdout)["argv"] == ["systemctl", "reboot", "--no-ask-password"]


def test_power_denied_without_section():
    """Секции "power" нет / операция false — отказ (код 3, безопасный дефолт)."""
    assert run_helper(["power", "poweroff"]).returncode == 3          # секции нет
    wl = {**ALLOWLIST, "power": {"poweroff": True, "reboot": False}}
    assert run_helper(["power", "reboot"], allowlist=wl).returncode == 3


def test_power_usage_errors():
    """power требует ровно один аргумент poweroff|reboot (код 2)."""
    wl = {**ALLOWLIST, "power": {"poweroff": True, "reboot": True}}
    assert run_helper(["power"], allowlist=wl).returncode == 2
    assert run_helper(["power", "reboot", "extra"], allowlist=wl).returncode == 2
    assert run_helper(["power", "halt"], allowlist=wl).returncode == 2


# --- аудит №8: биты записи allowlist и его каталога ---------------------------
#
# Subprocess-тесты с --dry-run осознанно пропускают stat-проверки (test-env),
# вне root uid-проверка сработает раньше битовой. Поэтому логику проверки
# исполняем in-process на исходнике helper'а с подставным os.stat: так
# покрываются обе новые ветки (биты записи файла и каталога). Реальная
# root-обвязка проверяется на станции (см. docs/STATION-LLM.md / DEPLOY).

import types  # noqa: E402


def _load_helper_ns():
    src = HELPER.read_text(encoding="utf-8")
    ns: dict = {"__name__": "helper_under_test", "__file__": str(HELPER)}
    exec(compile(src, str(HELPER), "exec"), ns)  # noqa: S102 — изолированный прогон исходника
    return ns


def _patch_stat(monkeypatch, file_mode, dir_mode, uid=0, dir_path="", dir_uid=None):
    """Подставной os.stat: путь каталога → st_dir, всё прочее (файл) → st_file."""
    st_file = types.SimpleNamespace(st_uid=uid, st_mode=file_mode)
    st_dir = types.SimpleNamespace(st_uid=(uid if dir_uid is None else dir_uid),
                                   st_mode=dir_mode)

    def fake_stat(path, *a, **kw):
        return st_dir if str(path) == dir_path else st_file

    import os
    monkeypatch.setattr(os, "stat", fake_stat)


def _allowlist_case(tmp_path, monkeypatch, file_mode, dir_mode, dir_uid=0):
    monkeypatch.delenv("LLM_PANEL_ALLOWLIST_TEST", raising=False)
    p = tmp_path / "allowlist.json"
    p.write_text(json.dumps(ALLOWLIST), encoding="utf-8")
    ns = _load_helper_ns()
    ns["ALLOWLIST_PATH"] = str(p)
    _patch_stat(monkeypatch, file_mode, dir_mode, uid=0, dir_path=str(p.parent),
                dir_uid=dir_uid)
    return ns


def test_allowlist_group_other_writable_refused(tmp_path, monkeypatch):
    """Аудит №8: root-owned allowlist с group/other-write → отказ (код 3)."""
    import pytest as _pytest
    ns = _allowlist_case(tmp_path, monkeypatch, 0o100666, 0o040755)
    with _pytest.raises(SystemExit) as e:
        ns["load_allowlist"]()
    assert e.value.code == 3


def test_allowlist_dir_not_root_refused(tmp_path, monkeypatch):
    """Аудит №8: каталог allowlist не от root → отказ (код 3)."""
    import pytest as _pytest
    ns = _allowlist_case(tmp_path, monkeypatch, 0o100644, 0o040755, dir_uid=1)
    with _pytest.raises(SystemExit) as e:
        ns["load_allowlist"]()
    assert e.value.code == 3


def test_allowlist_dir_group_writable_refused(tmp_path, monkeypatch):
    """Аудит №8: каталог allowlist с group/other-write → отказ (код 3)."""
    import pytest as _pytest
    ns = _allowlist_case(tmp_path, monkeypatch, 0o100644, 0o040777)
    with _pytest.raises(SystemExit) as e:
        ns["load_allowlist"]()
    assert e.value.code == 3


def test_allowlist_safe_modes_pass(tmp_path, monkeypatch):
    """Аудит №8 (позитив): 0644-файл в каталоге 0755 от root — загрузка проходит."""
    ns = _allowlist_case(tmp_path, monkeypatch, 0o100644, 0o040755)
    data = ns["load_allowlist"]()
    assert data["units"]["vllm.service"]["actions"][0] == "start"
