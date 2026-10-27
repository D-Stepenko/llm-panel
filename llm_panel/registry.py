"""Реестр описаний сервисов (ТЗ §4.1, §5.10).

Описания хранятся в согласованном YAML-формате (services.yaml).
ТЗ: имена существующих юнитов НЕ нормализовать по регистру;
`managed: adopted` + `edit_mode: manage_only` — режим по умолчанию.
Список разрешённых юнитов для helper хранится отдельно в root-owned
allowlist (helper проверяет сам, см. deploy/helper/).
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
from typing import Optional

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
UNIT_RE = re.compile(r"^[A-Za-z0-9_.@-]+\.service$")

# Согласованная на станции конвенция адресации CUDA (проверена владельцем,
# см. README «Факты обследования»): vLLM — по индексам, llama.cpp — по UUID.
# UUID-привязка (поле gpus) остаётся плановой привязкой панели; при генерации
# CUDA_VISIBLE_DEVICES для index-адресации индексы выводятся заново на момент
# запуска из текущего маппинга NVML (проверка missing_gpu — всегда по UUID).
CUDA_ADDRESSING = ("uuid", "index")


@dataclasses.dataclass(frozen=True)
class HealthDef:
    kind: str                 # "http" | "tcp"
    url: Optional[str]        # для http; только 127.0.0.1 + порт сервиса
    host: Optional[str]       # для tcp
    port: Optional[int]
    path: str
    timeout_s: float
    interval_s: float


@dataclasses.dataclass(frozen=True)
class ServiceDef:
    """Описание сервиса. GPU — список полных UUID (ТЗ §4.1: привязка по UUID, не индекс)."""

    id: str
    name: str
    type: str                 # vllm | llamacpp | script | generic
    unit: str
    managed: str              # adopted | created
    edit_mode: str            # manage_only | dropin | panel (для created)
    port: Optional[int]
    bind_host: Optional[str]
    gpus: tuple
    cuda_addressing: str      # "uuid" | "index" (конвенция станции; см. README)
    health: Optional[HealthDef]
    startup_timeout: int
    ram_exclusive: bool
    stop_signal: str
    stop_timeout: int
    notes: str


def _parse_health(d: Optional[dict], sid: str) -> Optional[HealthDef]:
    if d is None:
        return None
    if not isinstance(d, dict):
        raise ValueError(f"Сервис «{sid}»: health должен быть отображением либо отсутствовать")
    kind = d.get("kind", "http")
    if kind not in ("http", "tcp"):
        raise ValueError(f"Сервис «{sid}»: health.kind должен быть http или tcp")
    timeout_s = float(d.get("timeout_s", 3.0))
    interval_s = float(d.get("interval_s", 10.0))
    if not (0.5 <= timeout_s <= 15.0):
        raise ValueError(f"Сервис «{sid}»: health.timeout_s вне диапазона 0.5..15")
    if not (2.0 <= interval_s <= 120.0):
        raise ValueError(f"Сервис «{sid}»: health.interval_s вне диапазона 2..120")
    url = d.get("url")
    if kind == "http":
        if not url:
            raise ValueError(f"Сервис «{sid}»: health.url обязателен для kind http")
        u = str(url)
        # ТЗ §4.1: URL по умолчанию только 127.0.0.1 + порт сервиса.
        if not u.startswith("http://127.0.0.1") and not u.startswith("http://localhost"):
            raise ValueError(
                f"Сервис «{sid}»: health.url должен обращаться к 127.0.0.1 "
                f"(внешняя публикация API делается через Caddy, не через health-check)"
            )
    else:
        if not d.get("port") or not (1 <= int(d["port"]) <= 65535):
            raise ValueError(f"Сервис «{sid}»: health.port обязателен (1..65535) для kind tcp")
    return HealthDef(
        kind=kind,
        url=url if kind == "http" else None,
        host=d.get("host", "127.0.0.1") if kind == "tcp" else None,
        port=int(d["port"]) if d.get("port") else None,
        path=d.get("path", "/") or "/",
        timeout_s=timeout_s,
        interval_s=interval_s,
    )


def _parse_service(index: int, d: dict, startup_timeout_default: int, ports_min: int, ports_max: int) -> ServiceDef:
    sid = d.get("id")
    if not sid or not SLUG_RE.match(str(sid)):
        raise ValueError(
            f"Сервис #{index + 1}: id обязателен и должен быть безопасным slug "
            f"^[a-z0-9][a-z0-9_-]{{0,63}}$ (получено: {sid!r})"
        )
    unit = d.get("unit")
    if not unit or not UNIT_RE.match(str(unit)):
        raise ValueError(f"Сервис «{sid}»: unit обязателен, точное имя в форме <name>.service (получено: {unit!r})")
    if "/" in unit or "\x00" in unit:
        raise ValueError(f"Сервис «{sid}»: unit не должен содержать путей")
    if not d.get("name"):
        raise ValueError(f"Сервис «{sid}»: name обязателен")
    stype = d.get("type", "generic")
    if stype not in ("vllm", "llamacpp", "script", "generic"):
        raise ValueError(f"Сервис «{sid}»: type должен быть vllm|llamacpp|script|generic")
    managed = d.get("managed", "adopted")
    if managed not in ("adopted", "created"):
        raise ValueError(f"Сервис «{sid}»: managed должен быть adopted|created")
    edit_mode = d.get("edit_mode", "manage_only" if managed == "adopted" else "panel")
    if managed == "adopted" and edit_mode not in ("manage_only", "dropin"):
        raise ValueError(f"Сервис «{sid}»: для adopted edit_mode должен быть manage_only|dropin")
    if managed == "created" and edit_mode != "panel":
        raise ValueError(f"Сервис «{sid}»: для created edit_mode должен быть panel")

    gpus = tuple(str(g) for g in (d.get("gpus") or []))
    for g in gpus:
        if not re.match(r"^GPU-[0-9a-fA-F-]{20,}$", g):
            raise ValueError(
                f"Сервис «{sid}»: GPU должен быть полным UUID формата GPU-... "
                f"(индексы не сохранять, ТЗ §2; получено: {g!r})"
            )

    port = d.get("port")
    if port is not None:
        port = int(port)
        if not (1 <= port <= 65535):
            raise ValueError(f"Сервис «{sid}»: port вне диапазона 1..65535")

    startup_timeout = int(d.get("startup_timeout", startup_timeout_default))
    if not (30 <= startup_timeout <= 3600):
        raise ValueError(f"Сервис «{sid}»: startup_timeout вне диапазона 30..3600 (для Maya может быть до 900)")

    stop_signal = str(d.get("stop_signal", "SIGTERM"))
    # SIGKILL запрещён: ТЗ §9 требует штатного сигнала остановки (для Maya — SIGINT)
    if stop_signal not in ("SIGTERM", "SIGINT"):
        raise ValueError(f"Сервис «{sid}»: stop_signal допустимы SIGTERM|SIGINT (SIGKILL запрещён)")
    stop_timeout = int(d.get("stop_timeout", 30))
    if not (1 <= stop_timeout <= 300):
        raise ValueError(f"Сервис «{sid}»: stop_timeout вне диапазона 1..300")

    bind_host = d.get("bind_host")
    if bind_host is not None and not re.match(r"^[0-9a-fA-F.:]+$|^0\.0\.0\.0$|^127\.0\.0\.1$", str(bind_host)):
        raise ValueError(f"Сервис «{sid}»: bind_host имеет недопустимую форму ({bind_host!r})")

    cuda_addressing = str(d.get("cuda_addressing", "uuid"))
    if cuda_addressing not in CUDA_ADDRESSING:
        raise ValueError(f"Сервис «{sid}»: cuda_addressing должен быть uuid|index (получено: {cuda_addressing!r})")

    return ServiceDef(
        id=sid,
        name=str(d["name"]),
        type=stype,
        unit=str(unit),
        managed=managed,
        edit_mode=edit_mode,
        port=port,
        bind_host=bind_host,
        gpus=gpus,
        cuda_addressing=cuda_addressing,
        health=_parse_health(d.get("health"), sid),
        startup_timeout=startup_timeout,
        ram_exclusive=bool(d.get("ram_exclusive", False)),
        stop_signal=stop_signal,
        stop_timeout=stop_timeout,
        notes=str(d.get("notes", "")),
    )


@dataclasses.dataclass(frozen=True)
class ServiceRegistry:
    services: tuple

    def __iter__(self):
        return iter(self.services)

    def by_id(self, sid: str) -> Optional[ServiceDef]:
        for s in self.services:
            if s.id == sid:
                return s
        return None

    def by_unit(self, unit: str):
        return [s for s in self.services if s.unit == unit]


def load_services(path: str | pathlib.Path, startup_timeout_default: int = 600,
                  ports_min: int = 8100, ports_max: int = 8999) -> ServiceRegistry:
    import yaml

    path = pathlib.Path(path)
    if not path.is_file():
        raise ValueError(f"Файл описаний сервисов не найден: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    items = raw.get("services")
    if not isinstance(items, list):
        raise ValueError("services.yaml должен содержать ключ services: (список)")

    services = []
    seen_units = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"Сервис #{i + 1}: элемент должен быть отображением")
        svc = _parse_service(i, item, startup_timeout_default, ports_min, ports_max)
        if svc.unit in seen_units:
            raise ValueError(f"Сервис «{svc.id}»: юнит {svc.unit} уже занят другим описанием сервиса")
        seen_units.add(svc.unit)
        services.append(svc)
    return ServiceRegistry(tuple(services))
