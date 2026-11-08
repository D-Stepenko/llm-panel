# llm-panel — local web control panel for an LLM server

> **A note from the author.** This project was developed by a Russian-speaking
> author. The code comments and the `*_RU.md` documents are in Russian — please
> treat them with understanding and forbearance. The English documents you are
> reading are sense-faithful translations (meaning matters more than
> word-for-word accuracy). If the project attracts interest, a fully localized
> English variant — translated code comments and all — will be made.

A web panel for a server running LLM services: status and control of systemd
units (vLLM, llama.cpp, TTS/STT, auxiliary services), a GPU/RAM/CPU/disk
summary, journals with secret masking, and an audit log of actions.
**Universal**: the specific units, GPUs, disks and user are defined by
configuration and installer parameters rather than by code — the project
installs on any server with systemd (an NVIDIA GPU is optional).

![llm-panel dashboard: service cards, host and GPU summary](docs/screenshot-dashboard.png)

**Status: stage 3 (of 4) CLOSED, version 0.3.6.** Done: stage 1 (MVP) in full,
stage 2 partially (GPU matrix, configuration editor with versions/diff/rollback),
and stage 3 in full — model catalog, HF downloads (conda path to `hf`, running
inside a systemd-unit sandbox) and the "model → service" wizard (`/wizard`:
engine + GPU + free port → a ready `services.yaml` snippet + a unit template).
Next: stage 4 — Open WebUI integration. For the current status see
[docs/PROJECT.md](docs/PROJECT.md) (Russian original: `docs/PROJECT_RU.md`).

## Documentation

| Document | Contents |
|---|---|
| [docs/PROJECT.md](docs/PROJECT.md) | project description: features, architecture, security, configuration, Config Panel, stages |
| [docs/DEPLOY.md](docs/DEPLOY.md) | deployment on any server: install.sh / manual setup, unit/GPU/disk configuration, Caddy, updates, emergency procedures |
| [docs/STATION-LLM.md](docs/STATION-LLM.md) | survey facts and the history of the first station (a specific server) |
| [docs/UNITS.md](docs/UNITS.md) | adding systemd units to the panel (system and user units) |

Russian originals of all documents: `docs/*_RU.md` and `README_RU.md`.

## Stage 1 features

- Password login (Argon2), lockout after 5 failures, CSRF, strict CSP
  (no inline script/style).
- Dashboard: service cards (systemd status, health, autostart, uptime, GPU
  binding), a host block (RAM/swap/CPU/disks), GPU cards (load, memory,
  temperature, power, processes).
- start/stop/restart control and autostart — only through a root-owned helper
  with an allowlist (sudoers grants the right to exactly one path; a unit
  outside the allowlist = refusal).
- Unit journals: view/download with masking of `hf_…`/JWT/Bearer/sk- keys;
  live stream (SSE) with a per-line filter.
- Manual card ordering (drag & drop, persisted in the database).
- SSE updates from a single shared collector (interval 2–10 s, configurable
  in the UI).
- Config Panel `/config`: language EN/RU, °C/°F, refresh interval, audit limit,
  disk summary paths — everything applies on the fly, without a restart.
- Adding units from the UI (system and user units, "Search services"),
  hiding cards.
- Ground-truth data without yaml: GPUs by processes (NVML attribution),
  listening ports, automatic TCP health — for units without declared bindings
  (v0.1.12).
- GPU/CPU/RAM are detected automatically (NVML + psutil); "no data" instead of
  made-up values.

## Stage 2 features (partially implemented)

- **GPU matrix** (`/matrix`, view-only): planned card bindings from
  services.yaml versus actual occupancy (NVML processes); binding snippets in
  the owner's convention (`CUDA_VISIBLE_DEVICES` by indices/UUIDs,
  `"gpu": [...]` for Maya); services without binding in a separate muted
  section.
- **Conflicts** (advisory): a card planned for several services; actual sharing
  of a card; ≥2 services with `ram_exclusive` running at the same time; one
  port used by running services.
- **Configuration editor** (`/config/files`): editing dispot-owned engine
  configs (e.g. `maya-maya-m.json`) right from the panel — JSON validation
  before writing, atomic writes, version history in SQLite, diff against the
  current version, rollback (also as a new version). Root-owned files
  (`/etc/llm-panel`, systemd units) are not editable — per the specification.
  All actions are audited.
- Unit port in the card is a link opening in a new tab (panel host : unit
  port); for loopback-only bindings no link is given.

## Structure

```
llm-panel/
├── llm_panel/                  panel code (FastAPI, Jinja2, SQLite, NVML+psutil)
├── deploy/
│   ├── install.sh              universal installer (--user/--dir/--config-dir/...)
│   ├── llm-panel.service.template  panel systemd-unit template
│   ├── helper/llm-panel-systemd    root-owned helper
│   ├── *.example               config/services/allowlist templates
│   ├── sudoers.llm-panel       sudoers suggestion (one line per helper)
│   └── examples/               real filled-in examples (LLM station)
├── scripts/survey*.sh          read-only server survey
├── docs/                       PROJECT.md, DEPLOY.md, STATION-LLM.md, UNITS.md
├── tests/                      pytest
└── config.dev.yaml             development-machine config
```

## Local development (Windows/Linux without systemd)

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt          # Linux: .venv/bin/pip
.venv/Scripts/python -m llm_panel --config config.dev.yaml init-db
.venv/Scripts/python -m llm_panel --config config.dev.yaml set-password
.venv/Scripts/python -m llm_panel --config config.dev.yaml serve
# open http://localhost:8000  (SSE/systemd statuses will show "no data")
```

Tests: `.venv/Scripts/python -m pytest tests -q` (208 tests).

## Server installation

```bash
sudo bash deploy/install.sh --user <panel-user>
```

Full instructions — [docs/DEPLOY.md](docs/DEPLOY.md): server survey, filling in
`services.yaml`/`allowlist.json` for your units, sudoers (a separate
confirmation step), Caddy, updates, emergency procedures.
