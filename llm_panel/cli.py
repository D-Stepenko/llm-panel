"""CLI панели (ТЗ §6.2, §8).

- init-db: инициализация БД и каталога данных;
- set-password: интерактивный ввод пароля БЕЗ отображения (getpass), не через argv
  (пароль не должен попасть в список процессов);
- lockout-reset: серверное восстановление доступа;
- check-config: проверка конфигурации без запуска;
- serve: запуск uvicorn (loopback only).
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from .config import load_config, ConfigError
from .db import Database
from .security import set_password, reset_lockout, get_user


def _cmd_init_db(args) -> int:
    cfg = load_config(args.config)
    db = Database(cfg.db_path)
    if db.query_one("SELECT * FROM users WHERE username = 'admin'") is None:
        db.execute(
            "INSERT INTO users (username, password_hash, updated_at) VALUES ('admin', '!', 0)"
        )
        print("База данных создана. Пользователь admin без пароля — выполните `llm-panel set-password`.")
    else:
        print("База данных уже инициализирована.")
    try:
        os.chmod(cfg.db_path, 0o600)
    except OSError:
        pass
    return 0


def _password_classes(pw: str) -> int:
    """Число классов символов в пароле (из 4: строчные, прописные, цифры, прочие)."""
    classes = 0
    if any(c.islower() for c in pw):
        classes += 1
    if any(c.isupper() for c in pw):
        classes += 1
    if any(c.isdigit() for c in pw):
        classes += 1
    if any((not c.isalnum()) for c in pw):
        classes += 1
    return classes


def _ask_password() -> str | None:
    # интерактивно, без отображения; не через argv (ТЗ §6.2)
    pw = getpass.getpass("Новый пароль: ")
    repeat = getpass.getpass("Повторите пароль: ")
    if pw != repeat:
        print("Пароли не совпадают.", file=sys.stderr)
        return None
    # Политика (аудит №11): длина + сложность по составу — не менее 3 из 4
    # классов символов (строчные/прописные/цифры/прочие).
    if len(pw) < 10:
        print("Пароль должен быть не короче 10 символов.", file=sys.stderr)
        return None
    classes = _password_classes(pw)
    if classes < 3:
        print(
            "Слабый пароль: используйте не менее 3 из 4 классов символов "
            "(строчные, ПРОПИСНЫЕ, цифры, прочие знаки).",
            file=sys.stderr,
        )
        return None
    return pw


def _cmd_set_password(args) -> int:
    cfg = load_config(args.config)
    db = Database(cfg.db_path)
    pw = _ask_password()
    if pw is None:
        return 2
    set_password(db, "admin", pw)
    print("Пароль учетной записи admin обновлён. Все прежние сессии сброшены.")
    return 0


def _cmd_lockout_reset(args) -> int:
    cfg = load_config(args.config)
    db = Database(cfg.db_path)
    n = reset_lockout(db, "admin")
    print(f"Сброшено ошибок входа: {n}. Блокировка снята.")
    return 0


def _cmd_check_config(args) -> int:
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"ОШИБКА конфигурации: {e}", file=sys.stderr)
        return 2
    print("Конфигурация корректна:")
    print(f"  слушает: {cfg.listen_host}:{cfg.listen_port} (loopback)")
    print(f"  БД: {cfg.db_path}")
    print(f"  services.yaml: {cfg.services_file}")
    print(f"  helper: {cfg.helper_path}")
    print(f"  trusted_hosts: {', '.join(cfg.trusted_hosts)}")
    print(f"  cookie_secure={cfg.cookie_secure}, dev_mode={cfg.dev_mode}")
    return 0


def _cmd_serve(args) -> int:
    import uvicorn

    from .app import create_app
    from .config import load_config

    # Валидация ДО поднятия сервера: безопасная понятная ошибка, если путь/порт
    # заданы неверно (ТЗ §8). Фабрика не используется: uvicorn получает готовый app.
    cfg = load_config(args.config)
    app = create_app(cfg)
    uvicorn.run(
        app,
        host=cfg.listen_host,
        port=cfg.listen_port,
        # Доверяем X-Forwarded-* только от доверенного reverse proxy на loopback
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        log_level="info",
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="llm-panel", description="Управление локальной панелью llm-panel")
    parser.add_argument("--config", default=os.environ.get("LLM_PANEL_CONFIG", "/etc/llm-panel/config.yaml"))
    sub = parser.add_subparsers(dest="cmd", required=True)

    # --config объявляется и на верхнем уровне, и на каждой подкоманде, чтобы её
    # можно было писать ДО или ПОСЛЕ подкоманды (`llm-panel serve --config ...`).
    # SUPPRESS не даёт подкоманде перетереть верхний уровень значением None.
    p = sub.add_parser("init-db", help="создать БД и каталог данных")
    p.add_argument("--config", default=argparse.SUPPRESS)
    p.set_defaults(func=_cmd_init_db)

    p = sub.add_parser("set-password", help="задать пароль администратора интерактивно")
    p.add_argument("--config", default=argparse.SUPPRESS)
    p.set_defaults(func=_cmd_set_password)

    p = sub.add_parser("lockout-reset", help="снять блокировку входа (восстановление доступа)")
    p.add_argument("--config", default=argparse.SUPPRESS)
    p.set_defaults(func=_cmd_lockout_reset)

    p = sub.add_parser("check-config", help="проверить конфигурацию")
    p.add_argument("--config", default=argparse.SUPPRESS)
    p.set_defaults(func=_cmd_check_config)

    p = sub.add_parser("serve", help="запустить панель (loopback)")
    p.add_argument("--config", default=argparse.SUPPRESS)
    p.set_defaults(func=_cmd_serve)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
