"""Метрики GPU (NVML) и хоста (psutil), ТЗ §5.1.

Правила из ТЗ: лёгкое поведение — никакого nvidia-smi-спама, общий фоновый цикл;
если драйвер/библиотека не дают данные — показываем «нет данных», не выдумываем
значений. NVML инициализируется один раз; при ошибках возвращается None.
"""

from __future__ import annotations

import dataclasses

import psutil

# NVML подгружается только при наличии (на машине разработки его нет — это нормально)
try:
    import pynvml  # входит в пакет nvidia-ml-py
    _PYNVML_AVAILABLE = True
except ImportError:
    _PYNVML_AVAILABLE = False


@dataclasses.dataclass
class GpuMetrics:
    """None-поля означают «нет данных» (не показывать нули как факты)."""

    uuid: str | None = None
    index: int | None = None     # текущий индекс, только подсказка (ТЗ §2)
    name: str | None = None
    mem_total_mb: int | None = None
    mem_used_mb: int | None = None
    util_gpu_pct: int | None = None
    temp_c: int | None = None
    power_w: float | None = None
    power_limit_w: float | None = None
    procs: tuple = ()            # ((pid, proc_name, mem_mb), ...)


@dataclasses.dataclass
class HostMetrics:
    ram_used_mb: int | None = None
    ram_total_mb: int | None = None
    ram_available_mb: int | None = None
    swap_used_mb: int | None = None
    swap_total_mb: int | None = None
    cpu_percent: float | None = None
    disks: tuple = ()            # ((path, total_gb, free_gb), ...)


class MetricsCollector:
    def __init__(self):
        self._nvml_ready = False
        self._nvml_error = ""
        self._init_nvml()

    def _init_nvml(self) -> None:
        if not _PYNVML_AVAILABLE:
            self._nvml_error = "модуль nvidia-ml-py не установлен в этом окружении"
            return
        try:
            pynvml.nvmlInit()
            self._nvml_ready = True
        except Exception as e:  # noqa: BLE001 — NVML может отсутствовать/не отвечать
            self._nvml_ready = False
            self._nvml_error = f"NVML недоступен ({type(e).__name__})"

    @property
    def nvml_error(self) -> str:
        return self._nvml_error

    @property
    def nvml_ready(self) -> bool:
        return self._nvml_ready

    # -- GPU ---------------------------------------------------------------

    def gpu_uuids(self) -> list[str] | None:
        """Список полных UUID или None, если NVML недоступен (не выдумывать)."""
        if not self._nvml_ready:
            return None
        try:
            count = pynvml.nvmlDeviceGetCount()
            out = []
            for i in range(count):
                h = pynvml.nvmlDeviceGetHandleByIndex(i)
                out.append(pynvml.nvmlDeviceGetUUID(h))
            return out
        except Exception:  # noqa: BLE001
            return None

    def read_gpus(self) -> list[GpuMetrics] | None:
        if not self._nvml_ready:
            return None
        try:
            count = pynvml.nvmlDeviceGetCount()
        except Exception:  # noqa: BLE001
            return None
        gpus = []
        for i in range(count):
            gpus.append(self._read_gpu(i))
        return gpus

    def _read_gpu(self, index: int) -> GpuMetrics:
        m = GpuMetrics(index=index)
        try:
            h = pynvml.nvmlDeviceGetHandleByIndex(index)
            m.uuid = pynvml.nvmlDeviceGetUUID(h)
            m.name = pynvml.nvmlDeviceGetName(h)
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            m.mem_total_mb = int(mem.total / (1024 * 1024))
            m.mem_used_mb = int(mem.used / (1024 * 1024))
            try:
                m.util_gpu_pct = pynvml.nvmlDeviceGetUtilizationRates(h).gpu
            except Exception:  # noqa: BLE001 — утилизация доступна не на всех картах
                pass
            try:
                m.temp_c = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
            except Exception:  # noqa: BLE001
                pass
            try:
                m.power_w = pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0
                m.power_limit_w = pynvml.nvmlDeviceGetEnforcedPowerLimit(h) / 1000.0
            except Exception:  # noqa: BLE001
                pass
            procs = []
            for fn, kind in (
                (pynvml.nvmlDeviceGetComputeRunningProcesses, "compute"),
                (pynvml.nvmlDeviceGetGraphicsRunningProcesses, "graphics"),
            ):
                try:
                    for p in fn(h):
                        if any(pr.pid == p.pid for pr in procs):
                            continue
                        name = getattr(p, "procName", "") or ""
                        procs.append((p.pid, name, int(getattr(p, "usedGpuMemory", 0) or 0) // (1024 * 1024)))
                except Exception:  # noqa: BLE001 — на V100 не все виды процессов всегда определимы
                    pass
            m.procs = tuple(procs)
        except Exception as e:  # noqa: BLE001 — вернуть «нет данных» для этой карты
            m.name = m.name or f"<НВМЛ ошибка: {type(e).__name__}>"
        return m

    # -- Host --------------------------------------------------------------

    def read_host(self, model_roots: tuple) -> HostMetrics:
        h = HostMetrics()
        try:
            vm = psutil.virtual_memory()
            h.ram_used_mb = int((vm.total - vm.available) / (1024 * 1024))
            h.ram_total_mb = int(vm.total / (1024 * 1024))
            h.ram_available_mb = int(vm.available / (1024 * 1024))
            s = psutil.swap_memory()
            h.swap_used_mb = int(s.used / (1024 * 1024))
            h.swap_total_mb = int(s.total / (1024 * 1024))
            h.cpu_percent = float(psutil.cpu_percent(interval=None))
        except Exception:  # noqa: BLE001 — «нет данных» вместо значений
            pass
        disks = []
        for root in model_roots:
            try:
                u = psutil.disk_usage(root)
                disks.append((root, round(u.total / (1024 ** 3), 1), round(u.free / (1024 ** 3), 1)))
            except Exception:  # noqa: BLE001
                disks.append((root, None, None))
        h.disks = tuple(disks)
        return h

    def shutdown(self) -> None:
        if self._nvml_ready:
            try:
                pynvml.nvmlShutdown()
            except Exception:  # noqa: BLE001
                pass
            self._nvml_ready = False


def _fmt_gpu(g: GpuMetrics, index: int) -> dict:
    """Строка GPU для снапшота. None-значения остаются None → «нет данных» в UI."""
    mem_pct = None
    if g.mem_used_mb is not None and g.mem_total_mb:
        mem_pct = int(round(100.0 * g.mem_used_mb / g.mem_total_mb))
    power_pct = None
    if g.power_w is not None and g.power_limit_w:
        power_pct = int(round(100.0 * g.power_w / g.power_limit_w))
    return {
        "index": g.index if g.index is not None else index,
        "uuid": g.uuid,
        "name": g.name,
        "mem_total_mb": g.mem_total_mb,
        "mem_used_mb": g.mem_used_mb,
        "mem_pct": mem_pct,
        "util_gpu_pct": g.util_gpu_pct,
        "temp_c": g.temp_c,
        "power_w": round(g.power_w, 1) if g.power_w is not None else None,
        "power_limit_w": g.power_limit_w,
        "power_pct": power_pct,
        "procs": [
            {"pid": p[0], "name": p[1], "mem_mb": p[2], "owner": ""}
            for p in g.procs
        ],
    }


def build_gpu_rows(metrics: MetricsCollector) -> list[dict] | None:
    """Список GPU как JSON-совместимые словари; None, если NVML недоступен."""
    gpus = metrics.read_gpus()
    if gpus is None:
        return None
    return [_fmt_gpu(g, i) for i, g in enumerate(gpus)]
