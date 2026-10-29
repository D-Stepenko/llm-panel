"""Маршруты панели."""

from . import auth, dashboard, services, logs, config, matrix, config_files, models  # noqa: F401

__all__ = ["auth", "dashboard", "services", "logs", "config", "matrix",
           "config_files", "models"]
