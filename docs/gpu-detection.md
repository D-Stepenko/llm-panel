# GPU detection and identification (v0.3.14)

> **A note from the author.** This project was developed by a Russian-speaking
> author; the code comments and the `*_RU.md` documents are in Russian — please
> treat them with understanding and forbearance. English documents are
> sense-faithful translations; a fully localized English variant may follow if
> the project attracts interest.

How llm-panel finds the station's video cards and determines their vendor
(NVIDIA / AMD Radeon / Intel Arc) — the data source, the chain up to the UI,
limitations.

## 1. Data source: NVML

The single source of GPU data is **NVML** (NVIDIA Management Library) via the
`nvidia-ml-py` package (`import pynvml`), see `llm_panel/metrics.py`.

Initialization (`MetricsCollector._init_nvml`):

- `pynvml` not installed → NVML is considered unavailable,
  `nvml_error =` "the nvidia-ml-py module is not installed in this
  environment";
- `pynvml.nvmlInit()` fails (no driver / not Linux) → `nvml_error`
  "NVML unavailable (<exception type>)".

If NVML is unavailable, the GPU column on the main page shows a warning and
no card tiles are drawn — the panel does NOT degrade (`read_gpus()` returns
`None`).

## 2. Card list and metrics

`MetricsCollector.read_gpus()` (`metrics.py`):

1. `nvmlDeviceGetCount()` — the number of cards;
2. for each one: `nvmlDeviceGetHandleByIndex(i)`, then:

| field         | NVML call                                    |
|---------------|----------------------------------------------|
| uuid          | `nvmlDeviceGetUUID(h)` — FULL UUID           |
| name          | `nvmlDeviceGetName(h)` — as in `nvidia-smi`  |
| mem_used/total| `nvmlDeviceGetMemoryInfo(h).used/.total`     |
| util_gpu_pct  | `nvmlDeviceGetUtilizationRates(h).gpu`       |
| temp_c        | `nvmlDeviceGetTemperature(h, NVML_TEMPERATURE_GPU)` |
| power_w       | `nvmlDeviceGetPowerUsage(h) / 1000`          |
| power_limit_w | `nvmlDeviceGetEnforcedPowerLimit(h) / 1000`  |
| procs         | `nvmlDeviceGetComputeRunningProcesses` + `...Graphics...` (pid, name, memory) |

Any field may be missing (the card does not support it) — it stays `None`
and the UI shows "no data"; a read error on one card does not break the
others.

Formatting of the values into snapshot dictionaries — `build_gpu_rows()` →
`_fmt_gpu()` (`metrics.py`): adds `mem_pct`/`power_pct`, a human-readable
field order and **`vendor`** (see §3).

## 3. Vendor identification (v0.3.14)

`gpu_vendor(name)` in `metrics.py` — classification by card name:

- substrings `radeon` / `amd` → `"amd"`;
- substrings `intel` / `arc` → `"intel"`;
- otherwise → `"nvidia"` (the default, including for an empty name and the
  text "<NVML error: …>").

The vendor goes into the snapshot as the `"vendor"` key and is used by the
`_gpus.html` template for the card class: `gpu-card gpu-vendor-{vendor}`.
Border colors in `app.css` (variables in both themes):

| vendor | dark/light variables                | color                    |
|--------|-------------------------------------|--------------------------|
| nvidia | `--gpu-border` (base, green)        | `#285241` / `#a4dcc9`    |
| amd    | `--gpu-border-amd` (red)            | `#5c343d` / `#ddb0b7`    |
| intel  | `--gpu-border-intel` (blue)         | `#2d4661` / `#a8c0dd`    |

The `.gpu-vendor-amd` / `.gpu-vendor-intel` rules color the card border, the
header underline and the load pill; NVIDIA is the base rule.

**Limitation:** NVML sees ONLY NVIDIA cards. If the station runs a hybrid
configuration (e.g. NVIDIA + an iGPU), AMD/Intel names may come from another
source — the classification will still work by name; the panel does NOT
collect AMD/Intel metrics itself (no sysfs/radeontop integration).

## 4. Data chain up to the UI

```
MetricsCollector.read_gpus()            # NVML → GpuMetrics
  → build_gpu_rows() / _fmt_gpu()       # dicts + mem_pct/vendor (executor)
Collector._tick() (collector.py):
  → _gpu_attribution(gpu_rows, pid_owner)
      - procs[].owner  → service id by pid (including child processes)
      - g["services"]  → ACTUAL owners of the card (works even without a yaml binding)
  → g["assigned"] = [s.id for s in defs if uuid in s.gpus]   # PLANNED binding from services.yaml
  → Snapshot(ts, services, gpus, host, ...)
routes/dashboard.py:
  → GET /          : state.render("_gpus.html", snapshot=…) inside #gpus-init
  → GET /status    : SSE event "gpus" — the same fragments on every refresh
```

## 5. UUID and bindings

- In `services.yaml` cards are addressed by **full UUIDs** (`cuda_addressing:
  uuid`); `uuid_name` in collector.py substitutes human-readable card names
  into service cards and the GPU matrix (as in `nvidia-smi`).
- The matrix (`/matrix`) cross-checks the plan (gpus in yaml) against the
  facts (NVML processes); RAM/port/GPU conflicts are shown between the
  tables.
- To check UUIDs from scratch: `nvidia-smi --query-gpu=index,name,uuid --format=csv`.
