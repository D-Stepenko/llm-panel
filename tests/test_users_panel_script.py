"""Тесты llm_panel/users_panel_config_.sh (v0.3.16): меню управления учётками.

Bash-часть проверяется вне pytest (bash -n на станции); здесь исполняется
встроенный в скрипт python-блок (heredoc PYEOF) с подменой stdin/getpass —
покрывает create / passwd / delete / list на временной базе.
"""

import ast
import builtins
import pathlib
import sys

import pytest

SH = pathlib.Path(__file__).resolve().parent.parent / "llm_panel" / "users_panel_config_.sh"


def extracted_python() -> str:
    """Python-код, встроенный в sh (between <<'PYEOF' and PYEOF)."""
    src = SH.read_text(encoding="utf-8")
    start = src.index("<<'PYEOF'") + len("<<'PYEOF'")
    return src[start:]
# heredoc начинается со следующей строки; код — всё до строки PYEOF
_extracted_python_end_note = None


def _python_block() -> str:
    src = SH.read_text(encoding="utf-8")
    first = src.index('<<\'PYEOF\'')
    begin = src.index("\n", first) + 1
    end = src.index("\nPYEOF", begin)
    block = src[begin:end]
    # де-отступ: строка heredoc начинается без Tab — каждая строка кода
    # отступа не имеет (проверим, что первый непуст-столбец 0)
    return block


def run_upc(monkeypatch, cfg_path, action, answers, capsys=None):
    """Исполнить встроенный python-блок с действием action и ответами."""
    import getpass as gp_mod
    from llm_panel.security import get_user

    it = iter(answers)
    monkeypatch.setattr(gp_mod, "getpass", lambda *a: next(it))
    monkeypatch.setattr(builtins, "input", lambda *a: next(it))
    monkeypatch.setattr(sys, "argv", ["upc", str(cfg_path), action])
    src = SH.read_text(encoding="utf-8")
    first = src.index('<<\'PYEOF\'')
    begin = src.index("\n", first) + 1
    end = src.index("\nPYEOF", begin)
    block = src[begin:end]
    ast.parse(block)  # код обязан оставаться синтаксически корректным
    g = {"__name__": "upc_block"}
    # create/passwd заканчиваются нормальным завершением (без sys.exit),
    # остальные делают sys.exit(n); нормализуем: None = выход с кодом 0
    try:
        exec(compile(block, "<upc>", "exec"), g)
    except SystemExit as e:
        return e if getattr(e, "code", None) not in (None, 0) else None
    return None


def test_block_exists_and_lists_actions():
    src = SH.read_text(encoding="utf-8")
    for act in ("create", "delete", "passwd", "list"):
        assert f'"{act}"' in src


def test_create_and_list_and_passwd(tmp_path, monkeypatch, capsys):
    from llm_panel.security import get_user, verify_password
    from tests.test_ui_features import _make_config

    cfg_path = _make_config(tmp_path)
    ei = run_upc(monkeypatch, cfg_path, "create",
                 ["check-reviewer", "Pw-check-123", "Pw-check-123"])
    assert ei is None                  # нормальное завершение
    db = get_db(tmp_path, cfg_path)
    user = get_user(db, "check-reviewer")
    assert user is not None

    # список: печатает учётку
    ei = run_upc(monkeypatch, cfg_path, "list", [])
    assert ei is None
    out = capsys.readouterr().out
    assert "check-reviewer" in out and "итого" in out

    # смена пароля: новый пароль подтверждается verify_password
    ei = run_upc(monkeypatch, cfg_path, "passwd",
                 ["check-reviewer", "Pw-new-456a", "Pw-new-456a"])
    assert ei is None
    user = get_user(db, "check-reviewer")
    assert verify_password(user["password_hash"], "Pw-new-456a")


def test_delete_protects_admin_and_removes_user(tmp_path, monkeypatch, capsys):
    from llm_panel.security import get_user
    from tests.test_ui_features import _make_config

    cfg_path = _make_config(tmp_path)
    ei = run_upc(monkeypatch, cfg_path, "delete", ["admin"])
    assert getattr(ei, "code", None) == 3   # admin защищён (отказ до удаления)

    # создадим пользователя и удалим
    run_upc(monkeypatch, cfg_path, "create",
            ["tmp-user", "Pw-tmp-12345a", "Pw-tmp-12345a"])
    db = get_db(tmp_path, cfg_path)
    assert get_user(db, "tmp-user") is not None
    ei = run_upc(monkeypatch, cfg_path, "delete", ["tmp-user", "yes"])
    assert ei is None                   # нормальное завершение
    assert get_user(db, "tmp-user") is None


def get_db(tmp_path, cfg_path):
    from llm_panel.config import load_config
    from llm_panel.db import Database
    return Database(load_config(cfg_path).db_path)
