# The LLM station: survey facts and installation history

> **A note from the author.** This project was developed by a Russian-speaking
> author; the code comments and the `*_RU.md` documents are in Russian — please
> treat them with understanding and forbearance. English documents are
> sense-faithful translations; a fully localized English variant may follow if
> the project attracts interest.

The specifics of a real server (dispot@llm, 10.10.100.44, Ubuntu 24.04.5) on
which the panel was developed and first deployed. For a universal installation
— see docs/DEPLOY.md.

## 1. Hardware and environment

- Ubuntu 24.04.5, Python 3.12.3, user `dispot` (sudo (ALL:ALL) ALL with a
  password).
- GPUs (UUID→indices at the time of the 2026-10-26 survey):
  0 → GPU-9f764cdf-08bd-48a9-c219-a8660a71c1d6 (32 GB),
  1 → GPU-ca13b019-b898-2c2b-8c0a-2b92f64e6476 (32 GB),
  2 → GPU-cfec683a-1069-2f9c-0425-2e26192519e6 (16 GB).
- Caddy 2.6.2 (`tls internal`), upstreams: panel 127.0.0.1:8443 (port 8444
  externally), tts-proxy 8004 (/tts/*), open-webui 8080.
- `systemd-oomd` — inactive; `MemoryMax=infinity` in units behaves
  predictably, but there is no pressure protection (oomd).
- Ports `127.0.0.1:8888` and Node `127.0.0.1:18789` (via Caddy `18769`)
  belong to dispot, purpose not identified; not added to the panel (not
  systemd units).

## 2. Units (round 1 + round 2, 2026-10-26/27)

- `vllm.service`: enabled/running, `User=dispot`,
  `ExecStart=/home/dispot/bin/start-vllm.sh` (conda env 1cat-vllm-sm70_v1.5.0,
  CUDA 12.8, tensor-parallel-size 2, max-model-len 262144, api-key from
  ~/.vllm_api_key — the secret stays in the file, the panel does not read it),
  0.0.0.0:8000, `/health` 200 (`/v1/models` — 401), Restart=on-failure/30,
  Requires=gpu-power-limit.service. GPUs 0,1 by indices
  (CUDA_VISIBLE_DEVICES=0,1).
- `llama-qwen38_gguf.service`: enabled/running, `User=dispot`, llama.cpp
  0.4.1-dev (build 11141), binary /opt/llama.cpp/build/bin/llama-server, model
  Qwen3.8-27B-GSQ-RCO-IQ3_S.gguf, 16 GB card by UUID, context 82144,
  0.0.0.0:8002, `/health` 200. Conflicts=Qwen3-TTS.service.
- `llama-Qwen3.8_GGUF.service`: nearly a duplicate of the main one (alias
  qwen38-GGUF, context 62144), disabled. A typo in the Description
  ("Flash"). Conflicts=Qwen3-TTS.service.
- `llama-qwen38-flash.service`: disabled, 2 shards, `--lazy-mode on`, context
  262144, all three GPUs by UUID, Conflicts=vllm.service (running together
  with vLLM blocks systemd). Port 8002 is shared by all llama units.
- `speaches.service`: enabled/running, `User=dispot`, uvicorn 127.0.0.1:8001,
  `CUDA_VISIBLE_DEVICES=` (empty — CPU).
- `open-webui.service`: enabled/running, `User=dispot`, 0.0.0.0:8080
  (DISCREPANCY: 127.0.0.1 was expected — direct LAN access bypassing Caddy is
  not closed off), `/health` 200.
- `Qwen3-TTS.service`: inactive/disabled, WITHOUT `User=` → runs as root;
  drop-in editing via the panel is forbidden (spec §5.5), control/logs only.
  16 GB card by UUID.
- `tts-proxy.service`: inactive/disabled, `User=dispot`, 127.0.0.1:8004.
- `gpu-power-limit.service`: oneshot, `nvidia-smi -pm 1 -pl 150`, without CVD;
  view-only (allowlist: status+journal).
- **Maya**: v1.1.23, engine /home/dispot/src/project-maya/maya.sh (maya.py,
  build/maya, config maya-maya-m.json), automatic layer split [0,1,2], first
  load takes "minutes", stopped with SIGINT. Model: sharded
  ~/models/maya/GLM-5.3-Flash-Maya-M-IQ2_S-*3. Port and launch flags NOT
  confirmed (`./maya.sh --help` output needed before stage 2/3).

**CUDA addressing convention (owner's):** vLLM — by indices, llama.cpp/TTS —
by full UUIDs. In services.yaml this is the `cuda_addressing: index|uuid`
field; planned bindings in the panel are always by UUID, indices for
index-services are re-derived from NVML at start time.

**Discrepancies with the specification (owner's decision):**
1. Open WebUI and llama-server listen on 0.0.0.0 (8080, 8002) — the owner is
   closing this themselves.
2. `Qwen3-TTS.service` without `User=` (root) — editing the unit through the
   panel is declined permanently.

## 3. Installation history (2026-10-27) and incidents

- Installation per spec §12: survey → code/venv in /home/dispot/llm-panel →
  directories /etc/llm-panel, /var/lib/llm-panel (0700) → allowlist/helper →
  config/services → init-db/set-password → panel unit → Caddy 8444.
- **2026-10-28, incident "helper exited with code 1"**: the panel unit on the
  station had been deployed with sandbox options (`NoNewPrivileges=yes`,
  `ProtectSystem=strict`, `ProtectHome=read-only`, `PrivateTmp=yes`).
  `NoNewPrivileges` blocks sudo (setuid) → `sudo -n helper` failed with code 1
  on every action, while everything worked from an interactive shell.
  Diagnosis via the auth log (there are no sudo refusals there — sudo is
  rejected before authentication) and `systemctl show`. Fix: remove
  `NoNewPrivileges=yes`, `daemon-reload`, restart. The lesson is baked into
  `deploy/llm-panel.service.template`.
- **2026-10-29, incident "HF download: Errno 30 EROFS"**: /home/dispot/models
  is owned by dispot and writable, but the panel process (and its child
  `hf download`) lives inside the unit's sandbox (`ProtectSystem=strict`,
  `ProtectHome=read-only`) → all of /home is read-only outside of
  `ReadWritePaths`. Fix: add `ReadWritePaths=/home/dispot/models` to the
  [Service] section of llm-panel.service → `daemon-reload` → restart. The
  panel catches this in advance with a trial write and shows instructions
  (v0.3.3).
- sudoers `/etc/sudoers.d/llm-panel` (dispot → helper, NOPASSWD) installed
  2026-10-28 with the owner's confirmation, `visudo -c` — OK.
- Version 0.1.1: universalization (install.sh, deploy templates, disks in the
  Config Panel, i18n fixes), docs/.
- **2026-11-11, lesson "deploy commands only from the PROJECT ROOT"**: while
  installing the updated helper (`install: cannot stat
  'deploy/helper/llm-panel-systemd'`), the cause was the shell NOT being in
  the project root: first an attempt to `cd ~/llm-panel/llm-panel` (the
  nested folder must NOT exist — the 2026-11-01 WinSCP drag&drop lesson),
  then entering the package directory `cd ~/llm_panel/` (`llm_panel/` is the
  CODE PACKAGE, not the project root) — and the relative path
  `deploy/helper/...` stopped existing. Rule: sudo install / install.sh /
  check-config commands run only from the `~/llm-panel$` prompt (a check
  before the command: `ls deploy/helper/llm-panel-systemd` — the file must
  exist). Keep the `~/llm-panel/артефакты` folder (downloaded wheel/tar.gz)
  OUTSIDE the project directory — the next full directory copy (/MIR) will
  wipe it.
- **2026-11-11, "reset-failed not allowed (allowlist)" after the helper
  update — EXPECTED**: the new helper is installed, but the allowlist
  (`/etc/llm-panel/allowlist.json`) was not updated: the new per-unit action
  `reset-failed` must be ADDED to the `actions` of the needed units
  (root editing, as in docs/UNITS.md §3), and for `daemon-reload` on system
  units — the top-level key `"daemon_reload": true`. A helper update
  permits NOTHING by itself — action rights are always checked against the
  current allowlist (the safe default: refusal). After editing — a dry-run
  WITHOUT sudo (`/usr/local/sbin/llm-panel-systemd --dry-run reset-failed
  <unit>`) and `sudo systemctl restart llm-panel`. THE OWNER'S SOLUTION
  (v0.3.19, 2026-11-11): to avoid editing the allowlist for every new unit,
  the global keys `"reset_failed": true` and `"daemon_reload": true` are
  set — ONCE per manager, they cover all current and future units;
  unit-file editing (unit-cat/unit-write) remains strictly per-unit.

## 4. Updating the station from the repository

git push is unavailable on the server without a PAT — files are copied
manually from the repository (on the developer's PC) into
`/home/dispot/llm-panel` (do NOT replace `.venv`), then
`sudo systemctl restart llm-panel` and Ctrl+F5 in the browser. After the
`git pull`-based updates are set up — see docs/DEPLOY.md §7. All subsequent
installation commands (helper, install.sh, check-config) run ONLY from the
project root `~/llm-panel`; inside `llm_panel/` (the code package) the
relative `deploy/...` paths do not work (the 2026-11-11 lesson).
