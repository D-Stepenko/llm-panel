# Обнаружение и идентификация GPU (v0.3.14)

Как llm-panel находит видеокарты станции и определяет их вендора
(NVIDIA / AMD Radeon / Intel Arc) — источник данных, цепочка до UI,
ограничения.

## 1. Источник данных: NVML

Единственный источник GPU-данных — **NVML** (NVIDIA Management Library)
через пакет `nvidia-ml-py` (`import pynvml`), см. `llm_panel/metrics.py`.

Инициализация (`MetricsCollector._init_nvml`):

- `pynvml` не установлен → NVML считается недоступным, `nvml_error =`
  «модуль nvidia-ml-py не установлен в этом окружении»;
- `pynvml.nvmlInit()` падает (нет драйвера/не Linux) → `nvml_error`
  «NVML недоступен (<тип исключения>)».

Если NVML недоступен, в колонке GPU на главной появляется предупреждение
и карточки карт не рисуются — панель НЕ деградирует (`read_gpus()`
возвращает `None`).

## 2. Перечень карт и метрики

`MetricsCollector.read_gpus()` (`metrics.py`):

1. `nvmlDeviceGetCount()` — число карт;
2. для каждой: `nvmlDeviceGetHandleByIndex(i)`, затем:

| поле          | вызов NVML                                   |
|---------------|----------------------------------------------|
| uuid          | `nvmlDeviceGetUUID(h)` — ПОЛНЫЙ UUID         |
| name          | `nvmlDeviceGetName(h)` — как в `nvidia-smi`  |
| mem_used/total| `nvmlDeviceGetMemoryInfo(h).used/.total`     |
| util_gpu_pct  | `nvmlDeviceGetUtilizationRates(h).gpu`       |
| temp_c        | `nvmlDeviceGetTemperature(h, NVML_TEMPERATURE_GPU)` |
| power_w       | `nvmlDeviceGetPowerUsage(h) / 1000`          |
| power_limit_w | `nvmlDeviceGetEnforcedPowerLimit(h) / 1000`  |
| procs         | `nvmlDeviceGetComputeRunningProcesses` + `...Graphics...` (pid, имя, память) |

Любое поле может отсутствовать (карта не поддерживает) — остаётся `None`
и в UI показывает «нет данных»; ошибка чтения одной карты не рушит
остальные.

Форматирование значений в словари снапшота — `build_gpu_rows()` →
`_fmt_gpu()` (`metrics.py`): добавление `mem_pct`/`power_pct`,
человекочитаемый порядок полей и **`vendor`** (см. §3).

## 3. Идентификация вендора (v0.3.14)

`gpu_vendor(name)` в `metrics.py` — классификация по имени карты:

- подстроки `radeon` / `amd` → `"amd"`;
- подстроки `intel` / `arc` → `"intel"`;
- иначе → `"nvidia"` (дефолт, в т.ч. для пустого имени и текста
  «<НВМЛ ошибка: …>»).

Вендор попадает в снапшот как ключ `"vendor"` и используется шаблоном
`_gpus.html` для класса карточки: `gpu-card gpu-vendor-{vendor}`.
Цвет рамки в `app.css` (переменные в обеих темах):

| вендор | тёмные/светлые переменные          | цвет                     |
|--------|------------------------------------|--------------------------|
| nvidia | `--gpu-border` (базовая, зелёная)  | `#285241` / `#a4dcc9`    |
| amd    | `--gpu-border-amd` (красная)       | `#5c343d` / `#ddb0b7`    |
| intel  | `--gpu-border-intel` (синяя)       | `#2d4661` / `#a8c0dd`    |

Правила `.gpu-vendor-amd` / `.gpu-vendor-intel` красят рамку карточки,
подчёркивание шапки и pill загрузки; NVIDIA — базовое правило.

**Ограничение:** NVML видит ТОЛЬКО NVIDIA-карты. Если станция стоит на
гибридной конфигурации (например, NVIDIA + iGPU), имена AMD/Intel могут
прийти из другого источника — классификация сработает по имени; метрики
AMD/Intel панель сама НЕ собирает (нет интеграции с sysfs/radeontop).

## 4. Цепочка данных до UI

```
MetricsCollector.read_gpus()            # NVML → GpuMetrics
  → build_gpu_rows() / _fmt_gpu()       # словари + mem_pct/vendor (executor)
Collector._tick() (collector.py):
  → _gpu_attribution(gpu_rows, pid_owner)
      - procs[].owner  → id сервиса по pid (с учётом дочерних процессов)
      - g["services"]  → ФАКТИЧЕСКИЕ владельцы карты (работает и без привязки в yaml)
  → g["assigned"] = [s.id for s in defs if uuid in s.gpus]   # ПЛАНОВАЯ привязка из services.yaml
  → Snapshot(ts, services, gpus, host, ...)
routes/dashboard.py:
  → GET /          : state.render("_gpus.html", snapshot=…) внутри #gpus-init
  → GET /status    : SSE event «gpus» — те же фрагменты при каждом обновлении
```

## 5. UUID и привязки

- В `services.yaml` карты адресуются **полными UUID** (`cuda_addressing:
  uuid`); `uuid_name` в collector.py подставляет «человеческие» имена
  карт в карточки сервисов и матрицу GPU (как в `nvidia-smi`).
- Матрица (`/matrix`) сверяет план (gpus в yaml) и факт (processes
  NVML), конфликты RAM/портов/GPU показываются между таблицами.
- Проверить UUID с нуля: `nvidia-smi --query-gpu=index,name,uuid --format=csv`.
