"""Метрики GPU (NVML) и хоста (psutil), ТЗ §5.1.

Правила из ТЗ: лёгкое поведение — никакого nvidia-smi-спама, общий фоновый цикл;
если драйвер/библиотека не дают данные — показываем «нет данных», не выдумываем
значений. NVML инициализируется один раз; при ошибках возвращается None.
"""

from __future__ import annotations

import dataclasses
import pathlib
import platform
import re
import time

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
    cpu_model: str = ""           # например: «Xeon E5-2680 v4» (статично, читается один раз)
    cpu_cores: int | None = None  # физические ядра
    cpu_threads: int | None = None  # логические потоки
    cpu_ghz: float | None = None  # текущая частота, ГГц
    cpu_temp_c: float | None = None  # температура CPU (Package id 0 / max по сенсорам)
    cpu_power_w: float | None = None  # потребление CPU, Вт (RAPL, дельта за интервал)
    disks: tuple = ()            # ((path, total_gb, free_gb), ...)


_MEM_UNITS = {
    "ru": ("ГБ", "МБ", "КБ", "нет данных"),
    "en": ("GB", "MB", "KB", "n/a"),
}


def fmt_mem_mb(mb, units: str = "ru") -> str:
    """Единицы памяти по-человечески: 13784 → «13.78 ГБ» (ru) / «13.78 GB» (en)

    Десятичные гигабайты (1 ГБ = 1000 МБ), 1–2 знака без лишних нулей;
    ниже 0.95 ГБ — МБ, ниже 1 МБ — КБ. None → «нет данных».
    """
    gb_lbl, mb_lbl, kb_lbl, nd = _MEM_UNITS.get(units, _MEM_UNITS["ru"])
    if mb is None:
        return nd
    try:
        mb = float(mb)
    except (TypeError, ValueError):
        return nd
    if mb >= 950:
        gb = mb / 1000.0
        s = f"{gb:.2f}" if gb < 40 else f"{gb:.1f}"   # 13.78 ГБ, 32.77 ГБ; 50.4 ГБ, 64.2 ГБ
        return f"{s.rstrip('0').rstrip('.')} {gb_lbl}"
    if mb >= 1:
        return f"{int(round(mb))} {mb_lbl}"
    return f"{mb * 1000:.0f} {kb_lbl}"


def fmt_uptime(seconds: float | None, lang: str = "ru") -> str:
    """Время работы: «5ч 12м» (ru) / «5h 12m» (en)."""
    if seconds is None:
        return ""
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    if lang == "ru":
        if days:
            return f"{days}д {hours}ч {mins}м"
        if hours:
            return f"{hours}ч {mins}м {secs}с"
        return f"{mins}м {secs}с"
    if days:
        return f"{days}d {hours}h {mins}m"
    if hours:
        return f"{hours}h {mins}m {secs}s"
    return f"{mins}m {secs}s"


def _clean_cpu_model(raw: str) -> str:
    """«Intel(R) Xeon(R) CPU E5-2680 v4 @ 2.40GHz» → «Xeon E5-2680 v4»."""
    s = raw.replace("(R)", "").replace("(TM)", " ")
    s = re.split(r"@", s)[0]
    s = re.sub(r"\bCPU\b", "", s).replace("Intel", "").replace("AMD", "")
    return re.sub(r"\s+", " ", s).strip()


class MetricsCollector:
    def __init__(self):
        self._nvml_ready = False
        self._nvml_error = ""
        self._init_nvml()
        # Статичные данные CPU (модель/ядра/потоки) читаются один раз
        self._cpu_model, self._cpu_cores, self._cpu_threads = self._read_cpu_static()
        # RAPL: предыдущее чтение energy_uj (Вт считаются по дельте)
        self._rapl_prev: dict[str, tuple[float, float]] = {}  # домен → (мкДж, монотонное время)

    def _read_cpu_static(self) -> tuple[str, int | None, int | None]:
        model = ""
        try:
            with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("model name") and ":" in line:
                        model = _clean_cpu_model(line.split(":", 1)[1])
                        break
        except OSError:
            pass
        if not model:
            try:
                model = _clean_cpu_model(platform.processor() or "")
            except Exception:  # noqa: BLE001 — на экзотике не падаем
                model = ""
        cores = threads = None
        try:
            threads = psutil.cpu_count(logical=True)
            cores = psutil.cpu_count(logical=False)
        except Exception:  # noqa: BLE001
            pass
        return model, cores, threads

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

    def _read_cpu_temp(self) -> float | None:
        """Температура CPU (°C): приоритет «Package id 0» (coretemp/k10temp/
        cpu_thermal), иначе максимум по всем сенсорам. None — если недоступно."""
        try:
            data = psutil.sensors_temperatures() or {}
        except Exception:  # noqa: BLE001 — Linux-only, на ином падаем тихо
            return None
        for chip in ("coretemp", "k10temp", "cpu_thermal", "cpu_thermal_zone", "zenthermal"):
            for entry in data.get(chip, []):
                label = (getattr(entry, "label", "") or "")
                if "package" in label.lower():
                    return float(entry.current)
        # иначе максимум среди всех значений
        vals = [float(e.current) for entries in data.values()
                for e in entries if e.current is not None]
        return max(vals) if vals else None

    def _read_cpu_power(self) -> float | None:
        """Потребление CPU (Вт) из RAPL: дельта energy_uj за интервал.

        Достоверность важнее полноты: нечитаемый счётчик (песочница юнита,
        нет прав), переполнение, странные значения → None (UI просто
        не покажет Вт).
        """
        now = time.monotonic()
        watts: list[float] = []
        for dom_dir in pathlib.Path("/sys/class/powercap").glob("intel-rapl:*"):
            try:
                uj = int((dom_dir / "energy_uj").read_text().strip())
            except (OSError, ValueError):
                continue
            prev = self._rapl_prev.get(dom_dir.name)
            self._rapl_prev[dom_dir.name] = (uj, now)
            if prev is None:
                continue
            d_uj = uj - prev[0]
            if d_uj < 0:            # переполнение счётчика — пропускаем замер
                continue
            d_s = now - prev[1]
            if d_s <= 0:
                continue
            w = (d_uj / 1_000_000.0) / d_s
            if 0 <= w < 10_000:     # правдоподобие: CPU не тянет десятки кВт
                watts.append(w)
        return round(max(watts), 1) if watts else None

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
            h.cpu_model = self._cpu_model
            h.cpu_cores = self._cpu_cores
            h.cpu_threads = self._cpu_threads
            try:
                f = psutil.cpu_freq()
                if f and f.current:
                    h.cpu_ghz = round(f.current / 1000.0, 2)
            except Exception:  # noqa: BLE001 — частота доступна не везде
                pass
        except Exception:  # noqa: BLE001 — «нет данных» вместо значений
            pass
        h.cpu_temp_c = self._read_cpu_temp()
        h.cpu_power_w = self._read_cpu_power()
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
