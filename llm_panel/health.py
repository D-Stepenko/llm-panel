"""Проверки готовности API (health-check), ТЗ §4.1, §4.2.

Состояние готовности — отдельная сущность от состояния systemd, не подменяет его.
Проверка обращается только к 127.0.0.1 (или локальному host для tcp) и порту
самого сервиса: валидация ограничений сделана в registry (_parse_health).
"""

from __future__ import annotations

import asyncio
import dataclasses

import httpx


@dataclasses.dataclass(frozen=True)
class HealthResult:
    ok: bool
    detail: str
    latency_ms: int | None = None


async def _check_http(url: str, timeout_s: float) -> HealthResult:
    try:
        async with httpx.AsyncClient(timeout=timeout_s, verify=True, follow_redirects=False) as client:
            resp = await client.get(url)
        if 200 <= resp.status_code < 300:
            return HealthResult(ok=True, detail=f"HTTP {resp.status_code}")
        if resp.status_code in (401, 403):
            # «Живой» ответ API с требованием авторизации считаем признаком работоспособности
            return HealthResult(ok=True, detail=f"HTTP {resp.status_code} (требуется авторизация)")
        return HealthResult(ok=False, detail=f"HTTP {resp.status_code}")
    except httpx.ConnectError:
        return HealthResult(ok=False, detail="нет соединения")
    except (httpx.TimeoutException, TimeoutError):
        return HealthResult(ok=False, detail="тайм-аут")
    except Exception as e:  # noqa: BLE001 — не блокировать остальные проверки
        return HealthResult(ok=False, detail=f"ошибка: {type(e).__name__}")


async def run_health(hdef) -> HealthResult:
    """hdef: registry.HealthDef → HealthResult."""
    if hdef.kind == "http":
        return await _check_http(hdef.url, hdef.timeout_s)
    try:
        fut = asyncio.open_connection(hdef.host or "127.0.0.1", hdef.port)
        _, writer = await asyncio.wait_for(fut, timeout=hdef.timeout_s)
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, AttributeError):
            pass
        return HealthResult(ok=True, detail=f"TCP {hdef.host}:{hdef.port} доступен")
    except OSError:
        return HealthResult(ok=False, detail="TCP недоступен")
    except asyncio.TimeoutError:
        return HealthResult(ok=False, detail="тайм-аут")
