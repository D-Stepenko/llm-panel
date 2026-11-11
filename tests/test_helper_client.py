"""Тесты отображения кодов возврата helper в сообщения (helper_client._map_exit).

Код 1 helper'у не принадлежит — это код sudo при отказе (sudoers не установлен)
или необработанного исключения. Сообщение обязано включать фрагмент stderr,
иначе диагностика на станции невозможна.
"""

import pathlib

from llm_panel.helper_client import _map_exit


def test_code_1_includes_stderr():
    msg = _map_exit(1, "sudo: a password is required\n")
    assert msg.startswith("helper завершился с кодом 1:")
    assert "a password is required" in msg


def test_code_1_without_stderr():
    assert _map_exit(1, "") == "helper завершился с кодом 1"
    assert _map_exit(1, "   \n") == "helper завершился с кодом 1"


def test_code_1_stderr_truncated_to_200():
    msg = _map_exit(1, "x" * 500)
    assert len(msg) < 250  # префикс + не более 200 символов stderr


def test_known_codes_unchanged():
    assert _map_exit(3, "") == "отказано: юнит или операция вне allowlist (проверено самим helper)"
    assert _map_exit(2, "") == "ошибка аргументов helper"
    assert _map_exit(4, "Failed to stop vllm.service").startswith("systemd вернул ошибку:")


def test_usage_includes_helper_reason():
    """v0.3.19: код 2 тоже показывает причину от helper — без неё «неизвестное
    действие» от СТАРОГО helper выглядит как необъяснимая ошибка аргументов."""
    msg = _map_exit(2, "неизвестное действие\n")
    assert msg == "ошибка аргументов helper: неизвестное действие"
    assert _map_exit(2, "b\n").startswith("ошибка аргументов helper: ")
    assert _map_exit(2, "x" * 500).count("x") <= 160  # обрезка как в deny


def test_package_version_matches_pyproject():
    """__version__ и pyproject.toml не должны расходиться (урок v0.3.19:
    шапка панели показывала 0.3.17 после релиза 0.3.18/0.3.19)."""
    import tomllib

    import llm_panel

    root = pathlib.Path(llm_panel.__file__).resolve().parent.parent
    with open(root / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    assert llm_panel.__version__ == data["project"]["version"]


def test_denied_includes_helper_reason():
    """0.1.6: при отказе в сообщение включается точная причина от helper."""
    msg = _map_exit(3, "ОТКАЗ: юнит llm-panel.service не внесён в allowlist\n")
    assert msg.startswith("отказано: юнит или операция вне allowlist")
    assert "llm-panel.service не внесён" in msg
