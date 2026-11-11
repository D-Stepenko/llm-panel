# -*- coding: utf-8 -*-
"""Тесты deploy/allowlist_tool.py (v0.3.20): валидация, backup, команды,
sync-services. Инструмент правит /etc/llm-panel/allowlist.json без ручного
JSON-редактирования; в тестах используется --file во временном каталоге.
"""

import importlib.util
import json
import pathlib
import shutil

import pytest

_TOOL = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "allowlist_tool.py"
_spec = importlib.util.spec_from_file_location("allowlist_tool", _TOOL)
at = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(at)

EXAMPLE = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "examples"
ALLOWLIST_EXAMPLE = EXAMPLE / "allowlist.json.station-llm.json"
SERVICES_EXAMPLE = EXAMPLE / "services.yaml.station-llm.yaml"


@pytest.fixture()
def wl(tmp_path) -> pathlib.Path:
    """Копия примера станции во временном каталоге (--file)."""
    dst = tmp_path / "allowlist.json"
    shutil.copy(ALLOWLIST_EXAMPLE, dst)
    return dst


# --- list/check --------------------------------------------------------------

def test_check_valid_example(wl, capsys):
    assert at.main(["--file", str(wl), "check"]) == 0
    assert "OK" in capsys.readouterr().out


def test_check_invalid_json_dies(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        at.main(["--file", str(bad), "check"])
    assert e.value.code == 2
    assert "невалидный JSON" in capsys.readouterr().err


def test_check_unknown_action_dies(tmp_path, capsys):
    p = tmp_path / "a.json"
    p.write_text(json.dumps({"units": {"x.service": {"actions": ["hack"]}}}),
                 encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        at.main(["--file", str(p), "check"])
    assert e.value.code == 2
    assert "неизвестное действие" in capsys.readouterr().err


def test_list_shows_units_and_globals(wl, capsys):
    at.main(["--file", str(wl), "list"])
    out = capsys.readouterr().out
    assert "vllm.service" in out
    assert "reset_failed: True" in out


# --- add/remove/grant/revoke ---------------------------------------------------

def test_add_unit_default_actions_and_backup(wl):
    at.main(["--file", str(wl), "add-unit", "new-one.service"])
    data = json.loads(wl.read_text(encoding="utf-8"))
    assert "new-one.service" in data["units"]
    assert data["units"]["new-one.service"]["actions"] == list(at.DEFAULT_ACTIONS)
    baks = list(wl.parent.glob("allowlist.json.bak-*"))
    assert len(baks) == 1  # backup перед записью


def test_add_unit_duplicate_dies(wl):
    with pytest.raises(SystemExit):
        at.main(["--file", str(wl), "add-unit", "vllm.service"])


def test_add_unit_bad_name_dies(wl, capsys):
    with pytest.raises(SystemExit) as e:
        at.main(["--file", str(wl), "add-unit", "../evil.service"])
    assert e.value.code == 2
    assert "не соответствует шаблону" in capsys.readouterr().err


def test_grant_and_revoke(wl):
    at.main(["--file", str(wl), "grant", "vllm.service", "status"])
    data = json.loads(wl.read_text(encoding="utf-8"))
    assert data["units"]["vllm.service"]["actions"].count("status") == 1  # без дубля

    at.main(["--file", str(wl), "revoke", "vllm.service", "unit-write", "status"])
    data = json.loads(wl.read_text(encoding="utf-8"))
    acts = data["units"]["vllm.service"]["actions"]
    assert "unit-write" not in acts and "status" not in acts
    assert "unit-cat" in acts  # остальные не тронуты


def test_grant_unknown_unit_dies(wl, capsys):
    with pytest.raises(SystemExit) as e:
        at.main(["--file", str(wl), "grant", "nope.service", "status"])
    assert e.value.code == 2
    assert "нет в allowlist" in capsys.readouterr().err


def test_grant_unknown_action_dies(wl):
    with pytest.raises(SystemExit):
        at.main(["--file", str(wl), "grant", "vllm.service", "format-disk"])


# --- set-global ---------------------------------------------------------------

def test_set_global(wl):
    at.main(["--file", str(wl), "set-global", "reset_failed", "false"])
    data = json.loads(wl.read_text(encoding="utf-8"))
    assert data["reset_failed"] is False

    with pytest.raises(SystemExit):
        at.main(["--file", str(wl), "set-global", "unknown_key", "true"])


def test_set_global_invalid_value_dies(wl):
    with pytest.raises(SystemExit):
        at.main(["--file", str(wl), "set-global", "daemon_reload", "maybe"])


def test_dry_run_does_not_write(wl):
    before = wl.read_text(encoding="utf-8")
    at.main(["--file", str(wl), "add-unit", "dry.service", "--dry-run"])
    data = json.loads(wl.read_text(encoding="utf-8"))
    assert "dry.service" not in data["units"]
    assert wl.read_text(encoding="utf-8") == before


# --- backups ------------------------------------------------------------------

def test_backups_pruned_to_keep(wl, monkeypatch):
    # Часы с точностью до секунды: 8 быстрых записей дали бы одно имя backup.
    # Подменяем время, чтобы каждое имя было уникальным.
    counter = iter(range(1000))
    monkeypatch.setattr(at.time, "strftime", lambda fmt: f"t{next(counter):04d}")
    for i in range(8):
        at.main(["--file", str(wl), "add-unit", f"u{i}.service"])
    baks = sorted(wl.parent.glob("allowlist.json.bak-*"))
    assert len(baks) == at.BACKUPS_KEEP


def test_backup_keeps_previous_content(wl):
    at.main(["--file", str(wl), "remove-unit", "vllm.service"])
    bak = next(iter(wl.parent.glob("allowlist.json.bak-*")))
    old = json.loads(bak.read_text(encoding="utf-8"))
    assert "vllm.service" in old["units"]


# --- sync-services --------------------------------------------------------------

def test_sync_services_adds_missing_skips_existing_and_user(wl, capsys):
    at.main(["--file", str(wl), "sync-services", "--services", str(SERVICES_EXAMPLE)])
    out = capsys.readouterr().out
    data = json.loads(wl.read_text(encoding="utf-8"))

    assert "maya-maya-m.service" in data["units"]          # новый — добавлен
    assert data["units"]["maya-maya-m.service"]["actions"] == list(at.DEFAULT_ACTIONS)
    assert "vllm.service" in data["units"]                  # существующий — не тронут
    assert "unit-cat" in data["units"]["vllm.service"]["actions"]
    assert "openclaw-gateway.service" not in data["units"]  # user-юнит пропущен
    assert "User-юниты пропущены" in out


def test_sync_services_idempotent(wl):
    at.main(["--file", str(wl), "sync-services", "--services", str(SERVICES_EXAMPLE)])
    before = wl.read_text(encoding="utf-8")
    at.main(["--file", str(wl), "sync-services", "--services", str(SERVICES_EXAMPLE)])
    assert wl.read_text(encoding="utf-8") == before  # второй прогон — без изменений


def test_sync_services_custom_actions(wl):
    at.main(["--file", str(wl), "sync-services", "--services", str(SERVICES_EXAMPLE),
             "--actions", "status,journal"])
    data = json.loads(wl.read_text(encoding="utf-8"))
    assert data["units"]["maya-maya-m.service"]["actions"] == ["status", "journal"]


def test_sync_services_missing_file_dies(wl, capsys):
    with pytest.raises(SystemExit) as e:
        at.main(["--file", str(wl), "sync-services",
                 "--services", str(wl.parent / "no-such.yaml")])
    assert e.value.code == 2
    assert "не найден" in capsys.readouterr().err


# --- сохранение ключей-комментариев и порядка ----------------------------------

def test_comment_keys_and_order_preserved(wl):
    before = json.loads(wl.read_text(encoding="utf-8"))
    at.main(["--file", str(wl), "grant", "vllm.service", "status"])
    after = json.loads(wl.read_text(encoding="utf-8"))
    assert after["_note"] == before["_note"]          # комментарии сохранены
    assert list(after) == list(before)                # порядок ключей сохранён
