"""Smoke-тест CLI: init-db/set-password/check-config (ТЗ §6.2)."""

import subprocess
import sys
import pathlib
import textwrap

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _cfg(tmp_path):
    svc = {"services": [{"id": "vllm", "name": "vLLM", "type": "vllm", "unit": "vllm.service"}]}
    sf = tmp_path / "services.yaml"
    sf.write_text(yaml.safe_dump(svc, allow_unicode=True), encoding="utf-8")
    data = {
        "panel": {"listen_host": "127.0.0.1", "listen_port": 8999,
                  "trusted_hosts": ["localhost"], "cookie_secure": False, "dev_mode": True},
        "data": {"data_dir": str(tmp_path), "db_path": str(tmp_path / "p.db"),
                 "services_file": str(sf)},
        "systemd": {"helper_path": str(tmp_path / "h")},
    }
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return cfg


def run_cli(args, cwd=None):
    return subprocess.run(
        [sys.executable, "-m", "llm_panel", *args],
        capture_output=True, text=True, cwd=str(cwd or ROOT), timeout=120,
    )


def test_check_config_ok(tmp_path):
    r = run_cli(["--config", str(_cfg(tmp_path)), "check-config"])
    assert r.returncode == 0, r.stderr
    assert "Конфигурация корректна" in r.stdout


def test_init_db_and_password(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    r = run_cli(["--config", str(cfg), "init-db"])
    assert r.returncode == 0, r.stderr

    input_lines = iter(["s3cr3t-password\n", "s3cr3t-password\n"])
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: next(input_lines))
    from llm_panel.cli import _cmd_set_password
    import argparse

    args = argparse.Namespace(config=str(cfg))
    rc = _cmd_set_password(args)
    assert rc == 0

    # сессии/хеш в БД
    import sqlite3
    conn = sqlite3.connect(tmp_path / "p.db")
    h = conn.execute("SELECT password_hash FROM users WHERE username='admin'").fetchone()
    conn.close()
    assert h is not None and h[0] != "!" and h[0].startswith("$argon2")


def test_mismatched_password_rejected(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    run_cli(["--config", str(cfg), "init-db"])
    lines = iter(["s3cr3t-password\n", "other\n"])
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: next(lines))
    from llm_panel.cli import _cmd_set_password
    import argparse
    rc = _cmd_set_password(argparse.Namespace(config=str(cfg)))
    assert rc == 2


def test_short_password_rejected(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    run_cli(["--config", str(cfg), "init-db"])
    lines = iter(["short\n", "short\n"])
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: next(lines))
    from llm_panel.cli import _cmd_set_password
    import argparse
    rc = _cmd_set_password(argparse.Namespace(config=str(cfg)))
    assert rc == 2
