# llm-panel — project description

> **A note from the author.** This project was developed by a Russian-speaking
> author; the code comments and the `*_RU.md` documents are in Russian — please
> treat them with understanding and forbearance. English documents are
> sense-faithful translations (meaning matters more than word-for-word
> accuracy). If the project attracts interest, a fully localized English
> variant — translated code comments and all — will be made.

A local web control panel for an LLM station: statuses and control of
systemd services (vLLM, llama.cpp, TTS/STT, auxiliary), a GPU/RAM/CPU/disk
summary, journals, an audit of actions. Universal: it installs on any server
with systemd and an NVIDIA GPU; the specific units/cards/disks are described
by configuration, not by code.

Implemented per the specification `llm-panel-tz-reviewed.md` (rev. 1.1).
Current version: **0.1.1**, **stage 1 (MVP)** of 4 implemented.

## 1. Features (stage 1)

- Password login (Argon2), lockout after 5 failures, sessions in SQLite,
  CSRF on all POSTs.
- Dashboard: service cards (systemd status, health, autostart, uptime, GPU
  binding), a single host block (RAM/swap/CPU/disks), GPU tiles (load,
  memory, temperature, power, processes).
- Control: start/stop/restart, the autostart toggle (enable/disable) —
  through a restricted root-owned helper (see §3).
- Unit journals: viewing and downloading with secret masking.
- Manual card ordering ("Order": drag & drop, persisted in the DB, survives
  a panel restart).
- SSE updates: one shared background collector feeds all clients (the
  interval is configurable, 3 s by default).
- Config Panel (`/config`): language (EN/RU), temperature unit (°C/°F),
  refresh interval, audit entry limit, disk summary paths. Everything
  applies on the fly, without a panel restart.
- Load bars: ≤60 % green, ≤80 % yellow, above that red; GPU names as in
  `nvidia-smi`; states are displayed honestly ("no data", not made-up
  values).

## 2. Stack and structure

Python 3.12, FastAPI, Uvicorn (a single process, loopback only), Jinja2 (no
npm/build step), SQLite, NVML (`nvidia-ml-py`) + psutil. CSP without inline
script/style.

```
llm-panel/
├── llm_panel/                 panel code
│   ├── cli.py                 init-db | set-password | lockout-reset | check-config | serve
│   ├── app.py                 FastAPI: Host/CSRF/authentication, SSE, panel settings
│   ├── config.py              loading/validation of config.yaml
│   ├── registry.py            loading/validation of services.yaml
│   ├── db.py       security.py   SQLite, sessions, login lockout, passwords
│   ├── systemd_units.py  health.py   read-only systemd statuses; health checks
│   ├── collector.py  sse.py    shared background collector; event bus
│   ├── metrics.py             NVML + psutil
│   ├── helper_client.py       helper call via sudo -n (argv, no shell)
│   ├── i18n.py                all UI strings EN/RU
│   ├── routes/{auth,dashboard,services,logs,config}.py
│   └── templates/  static/
├── deploy/
│   ├── install.sh                  universal installer (see docs/DEPLOY.md)
│   ├── llm-panel.service.template  panel systemd-unit template
│   ├── helper/llm-panel-systemd    root-owned helper (installed on the server)
│   ├── config.yaml.example         /etc/llm-panel/config.yaml (template)
│   ├── services.yaml.example       /etc/llm-panel/services.yaml (template)
│   ├── allowlist.json.example      /etc/llm-panel/allowlist.json (template)
│   ├── sudoers.llm-panel           sudoers suggestion (the helper line only)
│   └── examples/                   real filled-in examples (LLM station)
├── scripts/survey.sh, survey2.sh   read-only server survey
├── docs/                           PROJECT.md, DEPLOY.md, STATION-LLM.md,
│                                    gpu-detection.md (GPU detection, vendors)
├── tests/                          pytest
└── config.dev.yaml                 development-machine config (no systemd/NVML)
```

## 3. Security

- **Authentication**: an Argon2 password, sessions with an httpOnly+Secure
  cookie (Secure is disabled only in dev), login lockout after 5 failures
  (lifted with `llm-panel lockout-reset`).
- **CSRF**: the token lives in the session, checked on every POST;
  Host/Origin — only from `trusted_hosts`.
- **CSP**: `default-src 'self'; script-src 'self'; style-src 'self';
  img-src 'self' data:; frame-ancestors 'none'; form-action 'self'` — no
  inline script/style, the bar widths are set by the `.p0…p100` classes.
- **Root operations**: only through the helper
  `/usr/local/sbin/llm-panel-systemd` (root:root 0755). The panel calls
  `sudo -n <helper> <action> <unit>`; the sudoers contains exactly one line
  for this path, NOPASSWD, nothing else. The helper itself checks the
  action, the exact unit name against a root-owned allowlist and the
  operation type; return codes: 0 — success, 2 — argument error, 3 —
  refusal, 4 — systemd failure. A unit outside the allowlist = refusal.
- **Secrets**: never rendered to the web layer; journals and downloads mask
  `hf_…`/JWT/Bearer/sk- keys (patterns configurable).
- **Audit**: all actions (login, service control, settings) are written to
  SQLite with IP, user, result and details; viewing — `/audit`.

## 4. Configuration

### 4.1 config.yaml (`/etc/llm-panel/config.yaml`)

| Key | Purpose | Default |
|---|---|---|
| `panel.listen_host/port` | panel listening address (loopback only) | 127.0.0.1:8443 |
| `panel.trusted_hosts` | allowed Host header values | — |
| `panel.cookie_secure` | Secure cookie (true on a server) | true |
| `panel.session_minutes`, `lockout_*` | sessions and lockout | 720 / 5 / 15 |
| `data.data_dir`, `db_path` | panel data directory and DB | /var/lib/llm-panel |
| `data.services_file` | path to services.yaml | /etc/llm-panel/services.yaml |
| `systemd.helper_path` | path to the root-owned helper | /usr/local/sbin/llm-panel-systemd |
| `systemd.log_max_lines`, `log_download_max_mb` | journal caps | 2000 / 2 |
| `metrics.interval`, `health_interval` | collection intervals (initial) | 3.0 / 5.0 s |
| `metrics.startup_timeout_default` | default start timeout | 600 s |
| `ports.min/max` | port range (stage 3 wizard) | 8100–8999 |
| `models.roots` | initial disk summary paths | — |
| `redact_patterns` | secret masking in journals | hf_/JWT/Bearer/sk- |

Check: `.venv/bin/python -m llm_panel --config <path> check-config`.

### 4.2 services.yaml (`/etc/llm-panel/services.yaml`)

The service descriptions are the single place listing the units of your
server. Fields: `id` (slug), `name`, `type` (vllm/llamacpp/script/generic),
`unit` (the exact `.service` name, case preserved), `managed`
(adopted/created), `edit_mode` (manage_only/dropin/panel), `port`,
`bind_host`, `health` (http/tcp), `gpus` (full UUIDs like `GPU-…`),
`cuda_addressing` (uuid/index), `startup_timeout`, `ram_exclusive`,
`stop_signal` (SIGTERM/SIGINT; SIGKILL forbidden), `stop_timeout`, `notes`.
Template — `deploy/services.yaml.example`, real examples —
`deploy/examples/`. Edited by the OS administrator; the panel reads the
file, never writes it.

### 4.3 allowlist.json (`/etc/llm-panel/allowlist.json`)

A root-owned list of units and allowed actions for the helper. A unit
outside the list — full refusal (safe default). The root:root owner check
is performed by the helper itself.

## 5. Config Panel (what is changed from the UI)

The `/config` section, rights of any logged-in user, every save goes to the
audit. Stored in the DB (settings), applies to the whole panel, applied
without a restart:

| Setting | Effect |
|---|---|
| Language (EN/RU) | interface language |
| Temperature unit (°C/°F) | GPU temperature display |
| Refresh interval (2/3/5/10 s) | metrics and SSE collection frequency |
| Audit entries shown (30/50/100/200) | the entry limit on the `/audit` page |
| Disks summary paths | paths for the disk summary on the main page (one absolute path per line, up to 8; non-existent ones show "no data") |

What is NOT editable from the UI (intentionally, spec §5.5/§9 and the
security model): config.yaml, services.yaml, allowlist.json, systemd units,
sudoers. The service configuration editor — stage 2.

## 6. Universality: what is detected automatically

- **GPUs**: count, names, UUIDs, memory, temperature, power, processes —
  NVML, automatically. No hard-coded cards.
- **CPU/RAM/swap**: psutil + `/proc/cpuinfo`, automatically.
- **Disks**: the list of paths is set in the Config Panel (initially —
  `models.roots`).
- **Service units**: `services.yaml` + `allowlist.json` for the specific
  server; the survey — `scripts/survey.sh` (read-only).
- **User/installation paths**: the `deploy/install.sh` parameters
  (`--user`, `--dir`, `--config-dir`, `--data-dir`); the systemd unit is
  generated from a template.

## 7. Specification stages

- **Stage 1 (MVP) — implemented** (versions 0.1.0–0.1.1): login/security,
  statuses, control through the helper, logs, GPU/RAM/CPU/disk summary,
  SSE, card ordering, i18n EN/RU, the Config Panel, universal installation.
- **Stage 2 — partially implemented** (version 0.2.0, refinements 0.2.1):
  the GPU matrix (`/matrix`: the services.yaml plan vs the NVML facts, index
  resolution, CVD/`gpu[]` binding snippets, GPU/RAM/port conflicts; in 0.2.1
  — the main table only for services with card bindings, the rest in the
  muted "Without GPU binding" section); the service configuration editor
  (`/config/files`: registering dispot-owned files, JSON validation,
  versions/diff/rollback in SQLite, audit). In 0.2.1: fixed the SSE log
  stream ("Stream error:" — a coroutine without await in events; now a
  meaningful reason or "connection closed"), the unit port in a card is a
  link in a new tab (panel host : port; for loopback-only bind_host no link
  is given). In 0.2.2: attribution of the GPU "facts" by the DESCENDANTS of
  MainPID — Maya (the maya.sh wrapper, the build/maya engine is a child) is
  now visible in the matrix as having taken the cards ("plan+facts"), not
  only the plan. Maya-specific items closed (2026-10-29): the service works,
  start/stop — manually and through the panel.
- **Stage 3**: model catalog, HF downloads, the "model → service" wizard.
  Partially implemented (0.3.0): the model catalog `/models` — a read-only
  scan of model_roots (Config Panel → "Disks summary paths"): GGUF (shards
  are grouped), HF safetensors directories
  (model_type/torch_dtype/quant from config.json), quantization and family
  from the name, size/files, free space of the roots, depth/entry limits, a
  scan cache with TTL and a "Rescan" button.
  HF downloads (0.3.1): a check for hf/huggingface-cli (if missing — a
  notice with a copyable install command); running
  `hf download <repo> --local-dir` into a chosen root (an optional
  subdirectory) as a background job: status, output tail (HTMX polling,
  2 s), stop (SIGTERM, no SIGKILL — spec §4.2), cleanup; a limit of 3
  concurrent; dest only inside model_roots; audited.
  Conda environment support (0.3.2): hf outside the panel unit's PATH (a
  conda env) — the Config Panel gained a "Path to hf" field
  (settings.hf_tool_path, the full path to the env binary, e.g.
  …/miniconda3/envs/<env>/bin/hf — run without activation; validation
  isabs+isfile+X_OK, otherwise a redirect err=hfpath). A banner without the
  tool: three copyable install variants (pipx — recommended, pip inside the
  conda env, pip --user --break-system-packages) + the hint
  "conda activate <env> && which hf" and instructions to set the path in the
  Config Panel.
  Fixes (0.3.3): (1) a write-protected destination directory (Errno 30) is
  caught in advance with a trial write (check_writable) → a clear notice
  with instructions instead of a downloader crash/500 on Subdirectory. The
  cause on the station is NOT a read-only FS but the panel unit's sandbox
  (ProtectSystem=strict + ProtectHome=read-only): the panel process (and its
  hf) may write only into ReadWritePaths; the station fix — add
  ReadWritePaths=/home/dispot/models to llm-panel.service; (2) the
  Stop/Remove job buttons — hx-post (HTMX), without navigating to a bare
  fragment; (3) the model catalog: dedup of directories by realpath (the
  maya symlink → Maya-M and nested roots are no longer duplicated) and one
  entry per directory with several different GGUFs (talker/tokenizer/vocab/
  mmproj are no longer separate "models").
  Continuation (0.3.4): after /home/dispot/models was opened, large-file
  downloads still broke — hf/xet write service data into
  ~/.cache/huggingface (refs/main, xet logs/chunks), closed by
  ProtectHome=read-only. The in-code solution: hf_env(state) redirects the
  HF_HOME of the hf process to <data_dir>/hf-home (the panel data directory
  is already in ReadWritePaths; created/verified with a trial write; on
  failure — a quiet rollback to the original environment). No need to widen
  the sandbox to ~/.cache anymore.
  The "model → service" wizard (0.3.5): a three-step /wizard page — a model
  from the catalog → an engine (llamacpp for GGUF / vllm for safetensors,
  preselected by kind) + cards (full UUIDs from NVML with "plan/busy"
  labels from the conflict matrix) + a free port (busy ones from the
  registry + a bind probe, the ports.min/max range) → a valid services.yaml
  YAML block (validated by the registry), a systemd unit template for the
  engine and installation steps. The panel writes NOTHING: the root-owned
  services.yaml and units are edited by the owner (spec §5.5). Generation —
  to the audit. Stage 3 CLOSED in full (catalog, HF downloads, wizard).
  CPU temperature/power and "Clean line" gauges (0.3.6): the CPU row gained
  a temperature ("Package id 0" from coretemp/k10temp/cpu_thermal, otherwise
  the maximum across sensors) and a watt consumption from RAPL (intel-rapl
  energy_uj, the delta over an interval; unreadable/overflow/unrealistic
  > 10 kW → the field is simply not shown). The load percentage was removed
  from the CPU text. ALL gauges (RAM/swap/CPU/disk + GPU memory/power) were
  reworked per the user's mock-up: height 10 px, a 1 px tone-colored
  border, a fill with a 2 px inset, a gradient, the percentage centered
  (.bar-pct), unified thresholds: green ≤50 / yellow 51–75 / red ≥76.
  Gauge polish (0.3.7, per the owner's screenshots): all gauges of a block
  are aligned into columns (host-card/gpu-body — grid, .host-row —
  contents: labels/values/gauges by columns, a shared left edge after the
  longest label and a shared right one); the gauge border — neutral
  var(--line-soft) in both themes (it used to be white rgba — invisible in
  the light theme), the tone colors ONLY the fill; the track's inner shadow
  removed — the track background is simply var(--track-bg) (half a tone off
  the block background); the percentage color = the block text color
  (var(--val-fg)) in each theme.
  Polish per the owner's screenshots (0.3.8): the gauges are aligned ALONG
  THE LONGEST LABEL (the values column max-content, the gauges of one width
  minmax(240px,320px) in the host block; in GPU tiles the gauge stretches
  to the right edge); GPU labels shortened (en: VRAM/Temp), memory/power
  values wrapped in .nb (white-space: nowrap) — when squeezed, wrapping
  only at " / ", at least "15.8 GB" per line instead of a letter-by-letter
  column; the "planned for: …" warnings removed from the GPU matrix (a
  duplicate of the table, several planned bindings per card is a normal
  scheme); /models: row numbering and a sort-toggle button (by name ↔ by
  size) at the top of the list.
  Polish per the owner's remarks (0.3.9): "Models: sort" — a manual
  drag&drop order, like the services (app.js: a generalized setupSort for
  .service-card/.model-row; the model order stored in settings.models_order
  by full paths, new models appended; POST /ui/models-order, audited);
  "Services: sort" moved UNDER the service cards (a 5 px gap); the gauge
  percentages got back a light shading for readability over the fill; the
  GPU pill (98%) — neutral: the border like the card's (--gpu-border), the
  text like the block's (--val-fg), the load color indication disabled
  (loaded cards are the norm).
  Documented enabling of CPU power: docs/DEPLOY.md §6.1 — the drop-in
  AmbientCapabilities=CAP_DAC_READ_SEARCH (RAPL read-only).
  Fixes per the owner's remarks (0.3.10): the model sort was broken — the
  .models-list container had no id="models-list", the JS got null and
  showed "Service cards not found" (the id added, a dedicated empty-list
  toast: order_no_models "Model rows not found"); on /matrix the "No
  GPU/RAM/port conflicts" block (and conflicts) moved UNDER the matrix
  table (a 14 px top gap), tests: the notice position and the presence of
  the drag&drop container — 210/210.
  Edits per the owner's remarks (0.3.11): "Services: sort" returned BEFORE
  the service cards (the 26 px top gap returned, Models has its own compact
  .models-head of 5 px); the model numbering moved OUTSIDE the card borders
  (a .model-item wrapper, the number outside, data-model and dragging on
  the wrapper); on /matrix the conflicts/"no conflicts" block — under the
  FIRST table, above "Without GPU binding"; theme-aware percentage shadows
  (--bar-shadow: dark in the dark theme, light in the light one — dark
  text does not drown); documented the cause of the "sudo: unable to change
  to root gid" error after enabling RAPL: a CapabilityBoundingSet line in
  the drop-in replaced the whole capability set (sudo loses CAP_SETGID) —
  removed from the DEPLOY.md §6.1 instructions, the warning also added to
  deploy/llm-panel.service.template.
  New (0.3.12): server shutdown/reboot from the panel — Config Panel, the
  bottom of the page: "Reboot server" / "Shut down server" buttons with a
  confirmation and a 5-second countdown with cancellation (a "Cancel"
  button, a repeat click, Esc; app.js without inline scripts per CSP).
  POST /ui/power (CSRF, audit power:poweroff/power:reboot) → the root
  helper `power <poweroff|reboot>` → systemctl as root; the permission — an
  optional "power" section of allowlist.json (false/absence — refusal, the
  safe default), no sudoers rules needed (the helper path is already
  allowed). The enablement instructions — docs/DEPLOY.md §6.2; a section
  example — deploy/allowlist.json.example. Tests: the helper (plan/refusal/
  usage) and the route (buttons, CSRF/400, audit, a helper error) —
  214/214.
  Edits (0.3.13): the power buttons brought to the project's common style —
  "Reboot server" amber (--pill-warn-*), "Shut down server" red
  (--pill-err-*, the --red icon); fixed by a test.
  Edits (0.3.14): per the owner's remarks from station screenshots — GPU
  tiles: the binding rows (Services: / "planned binding") moved out of the
  .gpu-body grid into a separate .gpu-binds block outside the grid (they
  used to break the label/gauge columns: the "planned binding" slid into
  the temperature gauge column, breaking the card); the paddings compacted
  (head 11/16/9, body 11/16/10); the last card's span-2 removed — all cards
  are ALWAYS of equal width (3 → 2 → 1 columns); when squeezed the gauge
  column compresses to 64px (the track does not escape the card border).
  Services: 3 cards per row above 1200px wide (2 at ≤1200px, 1 at ≤820px),
  compact paddings of .service-main/.service-fields/.service-bottom. GPU
  vendor borders (v0.3.14): gpu_vendor() in metrics.py
  (radeon/amd→amd, intel/arc→intel, otherwise nvidia) → gpu-vendor-* on
  .gpu-card: NVIDIA green (base), AMD red, Intel blue (the
  --gpu-border-amd/-intel variables in both themes); the GPU detection
  documentation — docs/gpu-detection.md. DEPLOY.md §6.2 rewritten for
  manually copying the ENTIRE project directory (the station is NOT updated
  by git): a grep POWER_ACTIONS check before sudo install, the cause of
  "ERROR: unknown action" — an old helper from v0.3.11. Tests: 217/217.
  Edits (0.3.15, per the owner's screenshots): service card fields —
  "label + value" on one line (10px between), the extra labels by the port
  and systemd removed (the important parts in tooltips); the GPU section of
  a card — a separate full-width strip (10px from the border), the card
  chips whole (white-space: nowrap), 5px between chips.
  Edits (0.3.16): the fields — a grid of 2 max-content columns (a
  borderless table: Port/Restarts in one vertical, Uptime/Systemd in the
  second), the GPU strip aligned with column 1 (16px). Added
  llm_panel/users_panel_config_.sh — an interactive menu for managing
  panel accounts (create / delete / password change / list; Argon2 via
  llm_panel.security.set_password, getpass, sessions are invalidated,
  deleting admin is forbidden) + tests running the embedded python block.
  The cause of long-lived "stale" copies on the station — the nested
  llm-panel/llm-panel folder when copying (WinSCP drag&drop), confirmed by
  the owner. Tests: 221/221.
- **Stage 4**: Open WebUI integration — SKIPPED by the owner's decision
  (2026-11-01): Open WebUI is visible in the panel as a regular service
  (start/stop/logs, health via /health) — that is enough; a deep
  integration of models/settings is not required. Effectively the
  specification stages are COMPLETE (1, 2, 3 implemented; 4 — removed from
  the plan by the owner).
- **0.3.17 — security audit (an external audit 2026-11-08, 12 findings)**.
  🔴1 Dependencies: fastapi 0.115.12→0.162.2, the starlette pin
  `>=1.3.1,<2` (all 2026 advisories — CVE-2025-54121/GHSA-2c2j-9gv5-cj73
  and related — are closed only in the 1.x branch; TemplateResponse moved
  to the `(request, name, ctx)` signature), python-multipart→0.0.31,
  pytest 9.0.3/pytest-asyncio 1.4.1; `pip_audit -r requirements.txt` —
  0 vulnerabilities; the CI gained `.github/workflows/security-audit.yml`
  (pip-audit --strict, weekly) and `.github/dependabot.yml`.
  🔴2 `_write_atomic` (config_files) preserves the source st_mode
  (chmod on tmp before os.replace), a repeated `_validate_new_path` on
  every save/rollback; refusal on an owner change (0600 no longer
  "degrades" to 0644 — mode tests skipif win32).
  🟠3 Lockout: blocking by the (username, ip) pair + a global
  per-account threshold (failures×3 within minutes×3); a success clears
  the failures of its own IP only (per-IP isolation + global threshold
  tests).
  🟠4 db.cleanup() runs at startup and every 6 h (the cleanup_task
  background job, cancelled in shutdown); a 90-day retention for
  login_attempts and audit; on_event migrated to lifespan.
  🟠5 Timing: for a non-existent user a dummy verify_password runs on a
  lazily generated real argon2id hash (the same response time).
  🟠6 An empty Host header → 400 (in dev_mode — skipped for debugging).
  🟠7 Open redirect: next rejects `//`, `/\` (scheme-relative) and `\\`
  inside — a fallback to `/`.
  🟡8 helper: the allowlist is checked for group/other-write bits
  (0644/0640 root), the /etc/llm-panel directory — root-owned without
  group/other write.
  🟡9 deploy/llm-panel.service.template: a comment block added with
  sudo-compatible sandbox options (ProtectSystem=strict + ReadWritePaths,
  PrivateTmp, ProtectKernel*, RestrictNamespaces, LockPersonality) — to be
  enabled one by one with a check after each.
  🟡10 CSRF: `csrf_matches()` via secrets.compare_digest — a single helper
  instead of 17 copies of `form.get("csrf") != request.state.csrf`.
  🟡11 Password policy (cli set-password and users_panel_config_.sh):
  ≥10 characters AND ≥3 of 4 character classes (lower/UPPER/digits/other).
  🟡12 Pickle-RCE: a warning in the UI (Models → "Download from Hugging
  Face", the hf_pickle_warning notice.warn) + the risk recorded in §8
  "Known limitations".
  UI fixes of the same build: the hint line on /login removed
  (login_note), the CSP fix of htmx indicators in static/app.js
  (includeIndicatorStyles=false). docs/ACCEPTANCE.md — the acceptance plan.
  Tests: 233/233 + 2 skip (win32).
- **0.3.18 — new service actions: reset-failed and daemon-reload**
  (2026-11-10). At the owner's request — two buttons on the service settings
  page (`/services/<id>/settings`, POST + CSRF, like the card buttons):
  "Reset failed status" (`systemctl reset-failed <unit>` — per-unit, to use
  after the failure has been dealt with) and "Daemon reload"
  (`systemctl daemon-reload` — an operation over the WHOLE MANAGER, the unit
  is not involved: it re-reads ALL unit files; use after manually editing
  unit files outside the panel). Scope handling: system units — through the
  helper (reset-failed — a regular per-unit allowlist action; daemon-reload
  — a NEW permission level: the optional allowlist key
  `"daemon_reload": true`, absent/false = refusal, a safe default by the
  example of the "power" section); user units — directly with
  `systemctl --user` (no helper needed, like the other user actions).
  Helper: "reset-failed" added to ACTIONS (takes no extra arguments),
  a daemon-reload branch without a unit argument (any argument = code 2),
  dry-run plan `["systemctl","daemon-reload","--no-ask-password"]`. Audit:
  `reset-failed[:user]` / `daemon-reload[:user]` with the actual unit state
  in the result. i18n: buttons/titles/messages EN+RU. Templates: buttons in
  service_settings.html; allowlist examples (universal and station) —
  reset-failed in the actions list + the daemon_reload section;
  llm_panel/helper_client.py — call_daemon_reload; systemd_units.py —
  daemon_reload_user, reset-failed in run_user_action.
  Tests: 249/249 + 2 skip (win32) — +12 (helper: plan/refusal/extra
  arguments; routes: system via call_helper/call_daemon_reload mocks, user
  via run_user_action/daemon_reload_user mocks, CSRF 403, button render,
  audit entries). Live check on the station — after copying.
- **0.3.19 — "Unit config": view/edit the unit file + UI fixes**
  (2026-11-11). At the owner's request:
  (1) the settings-page buttons are on ONE line with 20px gaps. The cause
  of the stacking: `<form>` inside `<p class="settings-links">` is invalid
  HTML — the parser closes the `<p>` before the form, so the buttons fell
  out of the flex container and became blocks. The container is now a
  `<div>`, CSS: `.settings-links` gap 20px +
  `.settings-links form { display: contents }`.
  (2) "Daemon reload" is NO LONGER pale: the btn-muted class removed
  (opacity .5 — was set by mistake in 0.3.18).
  (3) A new "Unit config" button → the /services/<id>/unit-config page:
  the full unit file in a textarea (like nano
  /etc/systemd/system/<unit>.service), with "Save" (POST unit-config/save)
  and "Daemon reload" buttons on the same page. System units — through the
  helper: NEW allowlist actions unit-cat (read) and unit-write (edit:
  content passed on STDIN, ONLY existing files — creating new units via
  the panel is forbidden; UTF-8/NUL/[Unit]/[Service]/[Install] validation;
  a backup in .panel-backups before every write; atomic tmp+os.replace,
  root:root 0644; on an error the edits are returned to the form);
  user units — directly ~/.config/systemd/user (unit_cat_user/
  unit_write_user, .panel-backups backup). Audit: unit-write[:user].
  (4) Global allowlist keys for "default actions" (the owner's decision —
  the actions are permitted to ALL current and FUTURE units):
  "reset_failed": true — reset-failed for any allowlisted unit without a
  per-unit entry; "daemon_reload": true — manager-wide (already covered
  every unit). unit-cat/unit-write remain strictly per-unit (editing a
  root file is more dangerous).
  Tests: 272/272 + 2 skip (win32) — +23 (helper: the global key,
  unit-cat/unit-write plans, a real roundtrip with backup/refusals;
  routes: render, load errors, save ok/refusal keeping the edits, CSRF,
  empty input, buttons/layout; user units: read/write, a file roundtrip).
  Live check on the station — after copying.

## 8. Known limitations

- The /config/files editor edits dispot-owned files only; /etc/llm-panel
  and systemd units are not edited from the UI (spec §5.5). The conflict
  matrix is advisory only (does not block actions).
- Maya: closed (2026-10-29). The maya-maya-m.service unit works, start/stop
  — manually and through the panel; the GPU "facts" are visible in the
  matrix (v0.2.2, attribution via MainPID descendants). The
  conflicts/idle_unload_s policy was deferred as unnecessary.
- The health check `readiness_timeout` does not kill the process (per the
  spec).
- The "unhealthy" history is kept in the panel's memory and is lost on
  restart.
- Tests on the development machine verify the logic, but not systemd/NVML
  (it is not there) — acceptance only on the target server.
- Sandbox options of the panel's systemd unit: `NoNewPrivileges` is
  forbidden (it breaks sudo to the helper), the others — enable one by one
  with a check (the station lesson 2026-10-28, see
  `deploy/llm-panel.service.template` and docs/STATION-LLM.md).
- Pickle-RCE via HF downloads (the 2026-11-08 audit, #12): the panel
  downloads models from ANY specified Hugging Face repositories. Some
  repositories contain pickle files (.bin / .pt from transformers) whose
  code executes when the model is loaded by the serving service (vLLM and
  the like) — the panel's own files are safe, but the consumer may run
  someone else's code. Mitigation: a warning in the UI (Models → "Download
  from Hugging Face"); download only from trusted sources, prefer
  safetensors, run untrusted models under a dedicated unprivileged user
  (not dispot/root). An automated safetensors inspection of downloads — in
  the stage 3+ TODO.
