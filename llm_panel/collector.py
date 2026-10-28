"""Общий фоновый сборщик данных: состояния юнитов, health, GPU/RAM/CPU/диск.

ТЗ §5.1: обновление через SSE примерно раз в 2 секунды; ОДИН общий сборщик
для всех подключённых клиентов (нет отдельного опроса на клиента, нет частых
вызовов nvidia-smi — используется NVML в пределах одного процесса).
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as _dt
import time

from .metrics import build_gpu_rows
from .status import DisplayState, STATE_RU, compute_display
from .systemd_units import show_unit


@dataclasses.dataclass
class Snapshot:
    ts: float
    services: list          # строки таблицы сервисов
    gpus: list | None       # None = нет данных NVML
    host: dict
    warnings: list


class Collector:
    def __init__(self, cfg, registry, metrics, hub):
        self.cfg = cfg
        self.registry = registry
        self.metrics = metrics
        self.hub = hub
        self.snapshot: Snapshot | None = None
        self.interval = float(cfg.metrics_interval)   # интервал цикла (меняется через Config Panel)
        # Пути для дисковой сводки (меняются через Config Panel на ходу;
        # стартовое значение — models.roots из config.yaml)
        self.model_roots: list[str] = list(cfg.model_roots)
        self._last_ready: dict[str, float] = {}     # unit -> прошлый успешный health ts
        self._last_health_check = 0.0
        self._health_cache: dict[str, object] = {}   # unit -> HealthResult
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="collector")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # ------------------------------------------------------------------

    async def _run(self) -> None:
        while True:
            try:
                snap = await self._collect()
                self.snapshot = snap
                await self.hub.publish(snap)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — общий цикл не должен падать
                # НЕ блокировать страницу: сохранить снапшот с предупреждением
                err = f"Ошибка цикла сбора: {type(e).__name__}: {e}"
                if self.snapshot is None:
                    self.snapshot = Snapshot(ts=time.time(), services=[], gpus=None,
                                             host={}, warnings=[err])
                else:
                    self.snapshot.warnings = (self.snapshot.warnings + [err])[:5]
            await asyncio.sleep(self.interval)

    async def _collect(self) -> Snapshot:
        loop = asyncio.get_running_loop()
        warnings: list[str] = []

        # 1) состояния юнитов (read-only systemctl show, без sudo)
        units = {}
        for svc in self.registry:
            info = await loop.run_in_executor(None, show_unit, svc.unit)
            units[svc.unit] = info

        # 2) health-проверки (не чаще health_interval)
        now = time.time()
        if now - self._last_health_check >= self.cfg.health_interval:
            self._last_health_check = now
            import llm_panel.health as health_mod

            for svc in self.registry:
                if svc.health is None:
                    continue
                result = await health_mod.run_health(svc.health)
                prev = self._health_cache.get(svc.unit)
                if result.ok:
                    self._last_ready[svc.unit] = now
                self._health_cache[svc.unit] = result
                _ = prev

        # 3) GPU и хост
        gpu_rows = await loop.run_in_executor(None, build_gpu_rows, self.metrics)
        if gpu_rows is None:
            warnings.append(self.metrics.nvml_error or "NVML недоступен (нет данных по GPU)")
        host = await loop.run_in_executor(None, self.metrics.read_host, tuple(self.model_roots))
        present_uuids = gpu_rows if gpu_rows is None else [g["uuid"] for g in gpu_rows]

        # 4) отображаемые состояния
        # названия карт (как в nvidia-smi) для отображения вместо UUID-хвостов
        uuid_name = {g["uuid"]: (g.get("name") or "") for g in gpu_rows} if gpu_rows else {}
        svc_rows = []
        pid_owner: dict[int, str] = {}
        for svc in self.registry:
            info = units.get(svc.unit)
            health = self._health_cache.get(svc.unit)
            if info is not None and info.pid:
                pid_owner.setdefault(info.pid, svc.id)
            uptime_s = None
            if info is not None and info.since:
                try:
                    dt = _dt.datetime.fromisoformat(info.since)
                    uptime_s = max(0.0, now - dt.timestamp())
                except ValueError:
                    uptime_s = None
            disp = compute_display(
                info,
                health,
                tuple(svc.gpus),
                present_uuids,
                uptime_s=uptime_s,
                startup_timeout=svc.startup_timeout,
                last_ready_ts=self._last_ready.get(svc.unit),
                now_ts=now,
            )
            svc_rows.append({
                "id": svc.id,
                "name": svc.name,
                "type": svc.type,
                "unit": svc.unit,
                "managed": svc.managed,
                "edit_mode": svc.edit_mode,
                "port": svc.port,
                "bind_host": svc.bind_host,
                "gpus": list(svc.gpus),
                "gpu_info": [
                    {"uuid": u, "name": uuid_name.get(u, "")}
                    for u in svc.gpus
                ],
                "state": disp.state.value,
                "state_ru": STATE_RU[disp.state.value],
                "note": disp.note,
                "system_active": disp.system_active,
                "system_sub": disp.system_sub,
                "system_result": info.result if info else "",
                "uptime_s": uptime_s,
                "nrestarts": info.nrestarts if info else None,
                "autostart": info.unit_file_state if info else "",
                "health": health.detail if health else "",
                "health_ok": health.ok if health else None,
                "ram_exclusive": svc.ram_exclusive,
                "stop_signal": svc.stop_signal,
                "notes": svc.notes,
            })

        if gpu_rows is not None:
            for g in gpu_rows:
                g["assigned"] = [
                    s.id for s in self.registry if g["uuid"] in s.gpus
                ]
                for proc in g["procs"]:
                    owned = pid_owner.get(proc["pid"])
                    proc["owner"] = owned if owned else ""

        return Snapshot(
            ts=now,
            services=svc_rows,
            gpus=gpu_rows,
            host={
                "ram_used_mb": host.ram_used_mb,
                "ram_total_mb": host.ram_total_mb,
                "ram_available_mb": host.ram_available_mb,
                "swap_used_mb": host.swap_used_mb,
                "swap_total_mb": host.swap_total_mb,
                "cpu_percent": host.cpu_percent,
                "cpu_model": host.cpu_model,
                "cpu_cores": host.cpu_cores,
                "cpu_threads": host.cpu_threads,
                "cpu_ghz": host.cpu_ghz,
                "disks": [list(d) for d in host.disks],
            },
            warnings=warnings,
        )
