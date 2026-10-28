"""Тесты отображения кодов возврата helper в сообщения (helper_client._map_exit).

Код 1 helper'у не принадлежит — это код sudo при отказе (sudoers не установлен)
или необработанного исключения. Сообщение обязано включать фрагмент stderr,
иначе диагностика на станции невозможна.
"""

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
