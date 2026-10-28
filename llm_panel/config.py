"""Загрузка и валидация конфигурации панели (ТЗ §8).

Валидация должна давать безопасную понятную ошибку без секретов, если
указанный путь/порт/каталог отсутствует или значение недопустимо.
"""

from __future__ import annotations

import dataclasses
import pathlib

import yaml

DEFAULT_REDACT_PATTERNS = (
    r"hf_[A-Za-z0-9]{20,}",                # HF-токены
    r"eyJ[A-Za-z0-9_.-]{10,}",             # JWT
    r"(?i)bearer\s+[A-Za-z0-9._-]{15,}",   # Authorization: Bearer ...
    r"sk-[A-Za-z0-9]{20,}",                # OpenAI-совместимые ключи
)


class ConfigError(Exception):
    """Понятная ошибка конфигурации (без секретов)."""


def _get(d: dict, key: str, default, type_=None):
    if key not in d or d[key] is None:
        return default
    v = d[key]
    if type_ is not None and not isinstance(v, type_):
        raise ConfigError(f"Настройка `{key}`: ожидался тип {type_}, получен {type(v).__name__}")
    return v


@dataclasses.dataclass(frozen=True)
class PanelConfig:
    config_path: str
    listen_host: str
    listen_port: int
    cookie_secure: bool
    session_minutes: int
    lockout_failures: int
    lockout_minutes: int
    trusted_hosts: tuple
    data_dir: str
    db_path: str
    services_file: str
    helper_path: str
    log_max_lines: int
    log_download_max_mb: int
    metrics_interval: float
    health_interval: float
    startup_timeout_default: int
    ports_min: int
    ports_max: int
    model_roots: tuple
    redact_patterns: tuple
    dev_mode: bool


def load_config(path: str | pathlib.Path) -> PanelConfig:
    path = pathlib.Path(path)
    if not path.is_file():
        raise ConfigError(f"Файл конфигурации не найден: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError("Конфигурация должна быть YAML-отображением (dict)")

    panel = _get(raw, "panel", {}, dict)
    listen_host = str(_get(panel, "listen_host", "127.0.0.1", str))
    if listen_host not in ("127.0.0.1", "::1"):
        # ТЗ §6.1: Uvicorn слушает только loopback. Наружу — только через Caddy по HTTPS.
        raise ConfigError(
            "panel.listen_host должен быть 127.0.0.1 (или ::1). "
            "Публикация панели наружу выполняется только через Caddy по HTTPS."
        )
    listen_port = int(_get(panel, "listen_port", 8443, int))
    if not (1 <= listen_port <= 65535):
        raise ConfigError("panel.listen_port вне диапазона 1..65535")

    trusted_hosts = tuple(
        str(h).strip().lower()
        for h in _get(panel, "trusted_hosts", ["localhost", "127.0.0.1"], list)
        if str(h).strip()
    )
    if not trusted_hosts:
        raise ConfigError("panel.trusted_hosts не может быть пустым (проверка заголовка Host)")

    cookie_secure = bool(_get(panel, "cookie_secure", True, bool))
    session_minutes = int(_get(panel, "session_minutes", 720, int))
    if not (30 <= session_minutes <= 60 * 24 * 30):
        raise ConfigError("panel.session_minutes вне диапазона 30..43200")
    lockout_failures = int(_get(panel, "lockout_failures", 5, int))
    if lockout_failures < 5:
        # ТЗ §6.1: блокировка после 5 последовательных ошибок; ослаблять нельзя.
        raise ConfigError("panel.lockout_failures не может быть меньше 5")
    lockout_minutes = int(_get(panel, "lockout_minutes", 15, int))
    dev_mode = bool(_get(panel, "dev_mode", False, bool))

    data = _get(raw, "data", {}, dict)
    data_dir = str(_get(data, "data_dir", "/var/lib/llm-panel", str))
    db_path = str(_get(data, "db_path", str(pathlib.Path(data_dir) / "panel.db"), str))
    services_file = str(_get(data, "services_file", "/etc/llm-panel/services.yaml", str))

    systemd = _get(raw, "systemd", {}, dict)
    helper_path = str(_get(systemd, "helper_path", "/usr/local/sbin/llm-panel-systemd", str))
    log_max_lines = int(_get(systemd, "log_max_lines", 2000, int))
    if not (10 <= log_max_lines <= 5000):
        raise ConfigError("systemd.log_max_lines вне диапазона 10..5000")
    log_download_max_mb = int(_get(systemd, "log_download_max_mb", 2, int))
    if not (1 <= log_download_max_mb <= 16):
        raise ConfigError("systemd.log_download_max_mb вне диапазона 1..16")

    metrics = _get(raw, "metrics", {}, dict)
    metrics_interval = float(_get(metrics, "interval", 3.0, (int, float)))
    if not (1.0 <= metrics_interval <= 10.0):
        raise ConfigError("metrics.interval вне диапазона 1.0..10.0 секунд")
    health_interval = float(_get(metrics, "health_interval", 5.0, (int, float)))
    if not (2.0 <= health_interval <= 60.0):
        raise ConfigError("metrics.health_interval вне диапазона 2.0..60.0 секунд")
    startup_timeout_default = int(_get(metrics, "startup_timeout_default", 600, int))
    if not (60 <= startup_timeout_default <= 3600):
        raise ConfigError(
            "metrics.startup_timeout_default вне диапазона 60..3600 "
            "(в services.yaml для отдельных сервисов допускается до 900 секунд)"
        )

    ports = _get(raw, "ports", {}, dict)
    ports_min = int(_get(ports, "min", 8100, int))
    ports_max = int(_get(ports, "max", 8999, int))
    if not (1024 <= ports_min < ports_max <= 65535):
        raise ConfigError("ports.min/max: требуется 1024 <= min < max <= 65535")

    models = _get(raw, "models", {}, dict)
    model_roots = tuple(str(r) for r in _get(models, "roots", [], list))

    redact_patterns = tuple(str(p) for p in _get(raw, "redact_patterns", list(DEFAULT_REDACT_PATTERNS), list))

    cfg = PanelConfig(
        config_path=str(path),
        listen_host=listen_host,
        listen_port=listen_port,
        cookie_secure=cookie_secure,
        session_minutes=session_minutes,
        lockout_failures=lockout_failures,
        lockout_minutes=lockout_minutes,
        trusted_hosts=trusted_hosts,
        data_dir=data_dir,
        db_path=db_path,
        services_file=services_file,
        helper_path=helper_path,
        log_max_lines=log_max_lines,
        log_download_max_mb=log_download_max_mb,
        metrics_interval=metrics_interval,
        health_interval=health_interval,
        startup_timeout_default=startup_timeout_default,
        ports_min=ports_min,
        ports_max=ports_max,
        model_roots=model_roots,
        redact_patterns=redact_patterns,
        dev_mode=dev_mode,
    )
    validate_paths(cfg)
    return cfg


def validate_paths(cfg: PanelConfig) -> None:
    """Проверка путей конфигурации (ТЗ §8: безопасная понятная ошибка при отсутствии).

    В dev-режиме (панель запущена на машине разработки, а не на станции)
    отсутствие БД/services.yaml/helper — ожидаемая ситуация: helper живёт на
    станции. Проверяется при выполнении реального действия.
    """
    if cfg.dev_mode:
        return

    db_parent = pathlib.Path(cfg.db_path).parent
    if not db_parent.is_dir():
        raise ConfigError(
            f"Каталог для БД не существует: {db_parent}. "
            "Создайте его и выставьте права (0700, владелец — пользователь, от которого работает панель)."
        )
    if not pathlib.Path(cfg.services_file).is_file():
        raise ConfigError(f"Файл описаний сервисов не найден: {cfg.services_file}")
    if not pathlib.Path(cfg.helper_path).is_file():
        raise ConfigError(
            f"Helper не найден: {cfg.helper_path}. "
            "Установите root-owned helper (см. README, раздел «Привилегированный helper»)."
        )
