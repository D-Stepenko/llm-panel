# Adding services: systemd units and the panel

> **A note from the author.** This project was developed by a Russian-speaking
> author; the code comments and the `*_RU.md` documents are in Russian — please
> treat them with understanding and forbearance. English documents are
> sense-faithful translations; a fully localized English variant may follow if
> the project attracts interest.

How to package a new LLM service as a systemd unit and show it in the panel —
for system units and user units. Uses the LLM station as an example
(`docs/STATION-LLM.md`); replace the user name (`dispot`), paths and units
with your own.

Three ways to show a service in the panel (in ascending order of detail):

| Method | What you get | When to use |
|---|---|---|
| "Search services" in the Config Panel | a card with state and actions | the unit has been loaded at least once |
| "Add unit by name" (same place) | the same, including units never started | quick addition without editing files |
| An entry in `services.yaml` | card + name, port, health, GPUs, notes | a primary service with a known configuration |

All three methods produce a card on the Status page with actions
(start/stop/restart/enable/disable): system units — through the root-owned
helper (see §3), user units — directly with `systemctl --user`. None of them
requires a panel restart (except editing `services.yaml` — it is re-read on
panel restart or with a `SIGHUP` drop-in, see below).

---

## 1. How to package a unit: system or user

**System unit** (`/etc/systemd/system/*.service`) — the default for LLM
services:

- survives user logout, starts at boot;
- resource limits (`OOMScoreAdjust`, `ManagedOOM*`) are set inside the unit
  itself;
- control from the panel — through the helper + allowlist (root, see §3);
- editing the unit — only via `sudo` (the panel does not edit units, spec
  §5.5).

**User unit** (`~/.config/systemd/user/*.service`) — for the panel user's
personal services (chat gateways, proxies, etc.):

- controlled with `systemctl --user` as the same user the panel runs as;
- the helper and allowlist are NOT needed (the panel runs systemctl
  directly);
- starting at boot requires linger: `loginctl enable-linger dispot`;
- not visible to the system manager: in `services.yaml` use `scope: user`,
  and the system-wide `systemctl ... show` has no such unit.

> A note on systemd-oomd: the user manager (`user@.service`) often has
> `ManagedOOMMemoryPressure=kill` — a service pinning almost all of RAM
> (Maya, for example) in a user unit risks being killed by oomd. A system
> unit has such properties off by default. One more reason to prefer a
> system unit.

### 1.1 System unit — template

```bash
sudo tee /etc/systemd/system/myservice.service >/dev/null <<'EOF'
[Unit]
Description=My LLM service
After=network-online.target gpu-power-limit.service
Wants=gpu-power-limit.service

[Service]
User=dispot
WorkingDirectory=/home/dispot/src/my-service
ExecStart=/home/dispot/src/my-service/.venv/bin/python serve.py --port 8100
Restart=on-failure
RestartSec=10
TimeoutStartSec=600      # if the service takes long to load a model
TimeoutStopSec=180       # how long to wait for a graceful stop
OOMScoreAdjust=-500      # on memory shortage the OOM killer takes it last

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
```

Check: `sudo systemctl start myservice.service`,
`systemctl status myservice.service`,
`journalctl -u myservice.service -f`. **Enable**
(`sudo systemctl enable ...`) — only if it is clear the service does not
contend for GPU/RAM with others at boot.

### 1.2 User unit — template

```bash
mkdir -p ~/.config/systemd/user
tee ~/.config/systemd/user/myservice.service >/dev/null <<'EOF'
[Unit]
Description=My service (user)

[Service]
WorkingDirectory=%h/src/my-service
ExecStart=%h/src/my-service/run.sh
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
loginctl enable-linger $USER   # so the unit lives without an open session
systemctl --user start myservice.service
```

### 1.3 Concept change: user unit → system unit (the Maya example)

```bash
# 1) stop and remove the user unit
systemctl --user stop maya-maya-m.service
rm ~/.config/systemd/user/maya-maya-m.service
systemctl --user daemon-reload

# 2) create the system unit (contents — per the §1.1 template; the full
#    Maya example — §5)
#    ...
# 3) re-read the systemd configuration
sudo systemctl daemon-reload
```

If the unit was already added to the panel as a user unit, remove it in the
Config Panel (uncheck it / remove it from `extra_units`) and add it again —
the scope will be determined automatically (system).

---

## 2. Show the unit in the panel

### 2.1 "Search services" (Config Panel)

`/config` → "Search services" → a list of loaded units of both managers
(system and user) → check the needed ones → "Save". Saved units are kept in
the panel settings (SQLite), no yaml edits required; cards appear on Status
with state/uptime and actions.

### 2.2 "Add unit by name"

The same block in the Config Panel, the field below the list. Works for
units missing from `list-units` too — ones never started, or stopped and
unloaded: the panel checks the name via `systemctl show` first in the user
manager, then the system one, determines the scope and saves the unit. The
name must be exact, with the suffix: `maya-maya-m.service`.

### 2.3 An entry in `services.yaml` (full details)

For primary services add a block to `/etc/llm-panel/services.yaml`
(template — `deploy/services.yaml.example`, real examples —
`deploy/examples/`):

```yaml
services:
  - id: my-service                    # latin letters/digits/dash, unique
    name: "My service (human-readable)"
    type: generic                     # vllm | llamacpp | generic
    unit: myservice.service           # EXACT unit name (case preserved)
    scope: system                     # for user units: user
    managed: adopted
    edit_mode: manage_only
    port: 8100
    bind_host: 127.0.0.1
    health:
      kind: http                      # or tcp (port)
      url: http://127.0.0.1:8100/health
      timeout_s: 3
      interval_s: 15
    startup_timeout: 600              # 30..3600, how long to wait for readiness
    stop_timeout: 60                  # 1..300, align with the unit's TimeoutStopSec
    gpus: []                          # planned binding by FULL UUIDs (nvidia-smi -L)
    cuda_addressing: uuid             # or index (how the service actually addresses cards)
    notes: >-
      ExecStart, env, specifics.
```

After editing — `sudo systemctl restart llm-panel` (or check the config:
`.venv/bin/python -m llm_panel --config /etc/llm-panel/config.yaml
check-config`). The GPU fields and the health endpoint — stage 2; a simple
card only needs `id/unit/scope`.

> The YAML block and units added through the UI (§2.1/§2.2) do not conflict
> with each other: the registry takes priority — a unit already described in
> `services.yaml` cannot be added again from the UI.

---

## 3. Allowlist for system units

Control of system units from the panel goes through the root-owned helper
`/usr/local/sbin/llm-panel-systemd`; without an allowlist entry for the unit,
the start/stop/restart buttons are declined with a hint "add the unit to the
allowlist" (status reading works without it — the panel reads systemctl
directly).

Adding an entry — from root only. It is convenient to merge it with an
existing entry:

```bash
sudo python3 - <<'EOF'
import json
p = "/etc/llm-panel/allowlist.json"
with open(p, encoding="utf-8") as f:
    d = json.load(f)
d["units"]["myservice.service"] = {
    "actions": ["start", "stop", "restart", "enable", "disable", "reset-failed",
                "status", "journal", "journal-follow"],
    # "view only": ["status", "journal"] + "note": "why"
}
with open(p, "w", encoding="utf-8") as f:
    json.dump(d, f, ensure_ascii=False, indent=2)
EOF
```

Check: `sudo -n /usr/local/sbin/llm-panel-systemd status myservice.service`
(exit code 0), then the buttons on the card. User units do not need to be
added to the allowlist.

Actions from the service settings and the "Unit config" page
(v0.3.18/0.3.19):

- `reset-failed` ("Reset failed status") — a per-unit action. To avoid
  listing it in every `actions`, permit it globally: the top-level
  allowlist key `"reset_failed": true` applies to ALL units listed in the
  allowlist (current and future ones).
- `daemon-reload` ("Daemon reload") — an operation over the ENTIRE manager
  (re-reads all unit files), it does NOT go into per-unit `actions`: it is
  permitted by a single key `"daemon_reload": true` — that covers all
  units at once. User units need none of this — `systemctl --user
  daemon-reload` is run by the panel directly.
- `unit-cat` / `unit-write` ("Unit config": view/edit the unit file
  /etc/systemd/system/<unit>) — strictly per-unit (editing a root file is
  more dangerous): add them to `actions` only for units that really need
  editing from the UI. Before every write the helper makes a backup in
  /etc/systemd/system/.panel-backups/; creating NEW units via the panel is
  forbidden.

---

## 4. Checklist for adding a new service

1. Create the unit (§1.1 or §1.2), `daemon-reload`, a test start, the
   journal.
2. Settle the GPU/memory question: does the unit overlap running ones
   (`ss -ltnp`, `nvidia-smi`, the RAM summary)? Do not `enable` before it is
   settled.
3. Add to the allowlist (§3) — for system units with buttons.
4. Add to the panel: "Search services" or "by name" (§2.1/§2.2); for
   permanent services — a block in `services.yaml` (§2.3).
5. Fill in ports/health/gpus according to the actual behavior; notes — for
   posterity.

---

## 5. Example: Maya (system unit, 2026-10-28)

Context: Maya-M (GLM-5.3-Flash) in `/home/dispot/src/project-maya`, config
`maya-maya-m.json`, web UI and API on one port 8010 (loopback), the engine
pins almost all free RAM (`MAYA_GLM_RAM_HEADROOM_GB=10`), start ~6.5 min,
graceful stop up to 150 s (QUIT to the engine, VRAM/RAM release).

### 5.1 The unit

```bash
sudo tee /etc/systemd/system/maya-maya-m.service >/dev/null <<'EOF'
[Unit]
Description=Project Maya (Maya-M) - LLM station service
After=network-online.target gpu-power-limit.service
Wants=gpu-power-limit.service

[Service]
User=dispot
WorkingDirectory=/home/dispot/src/project-maya
Environment=MAYA_GLM_RAM_HEADROOM_GB=10
ExecStart=/home/dispot/src/project-maya/.venv/bin/python maya.py --config maya-maya-m.json --port 8010
Restart=on-failure
RestartSec=10
TimeoutStartSec=900
TimeoutStopSec=180
OOMScoreAdjust=-500

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
```

### 5.2 Allowlist (system unit — actions through the helper)

```bash
sudo python3 - <<'EOF'
import json
p = "/etc/llm-panel/allowlist.json"
with open(p, encoding="utf-8") as f:
    d = json.load(f)
d["units"]["maya-maya-m.service"] = {
    "actions": ["start", "stop", "restart", "enable", "disable", "reset-failed",
                "status", "journal", "journal-follow"],
}
with open(p, "w", encoding="utf-8") as f:
    json.dump(d, f, ensure_ascii=False, indent=2)
EOF
```

### 5.3 A test start

```bash
sudo systemctl stop vllm.service          # vllm holds cards 0,1 — without this
                                          # Maya dies with "pack: out of memory"
sudo systemctl start maya-maya-m.service
journalctl -u maya-maya-m.service -f      # "ready: http://127.0.0.1:8010/v1"
```

`enable` is deferred until a concurrency-policy decision (stage 2): at
station boot Maya and vllm would contend for cards 0/1.

### 5.4 The panel

`/config` → "Search services" or "Add unit by name" → `maya-maya-m.service`.
A permanent registry block —
`deploy/examples/services.yaml.station-llm.yaml` (id `maya`: port 8010,
tcp-health, startup_timeout 900, stop_timeout 180, all three GPUs by UUID,
`cuda_addressing: index`, `ram_exclusive: true`).
