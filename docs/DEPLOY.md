# llm-panel — deployment guide

> **A note from the author.** This project was developed by a Russian-speaking
> author; the code comments and the `*_RU.md` documents are in Russian — please
> treat them with understanding and forbearance. English documents are
> sense-faithful translations; a fully localized English variant may follow if
> the project attracts interest.

A universal installation on a server with systemd and an NVIDIA GPU (Ubuntu
22.04/24.04+ verified). All paths/user names are parameters; no values from
any specific server are hard-coded. For the history of the installation on
the LLM station see `docs/STATION-LLM.md`.

## 1. Requirements

- Linux + systemd; Python 3.11+ (3.12 recommended); an NVIDIA GPU + an NVML
  driver (the panel also works without a GPU — the GPU block will show "no
  data").
- A user (below: `paneluser`) with `sudo` rights for the installation steps.
  The panel itself does NOT run as root.
- LAN access for web access through Caddy (the panel listens only on
  `127.0.0.1`).

## 2. Server survey (read-only, before any changes)

```bash
git clone https://github.com/D-Stepenko/llm-panel.git
cd llm-panel
bash scripts/survey.sh > survey.out 2>&1      # changes nothing, only reads
bash scripts/survey2.sh                        # clarifications if needed
```

From the output take: the actual unit names (`systemctl list-units
--type=service`), ports and listening addresses, health endpoints, GPU UUIDs
(`nvidia-smi -L`).

## 3. Quick installation (install.sh)

```bash
sudo bash deploy/install.sh --user paneluser
```

Parameters:

| Parameter | Default | Purpose |
|---|---|---|
| `--user` | required | the user the panel runs as |
| `--dir` | `~paneluser/llm-panel` | code and `.venv` directory |
| `--config-dir` | `/etc/llm-panel` | configuration directory |
| `--data-dir` | `/var/lib/llm-panel` | data directory (DB), permissions 0700 |
| `--with-sudoers` | off | install the sudoers rule (see §4.6 — requires an explicit decision) |
| `--no-start` | off | do not start the panel right away |

The script: creates a venv in the user's home directory (not the system
Python), installs dependencies, creates directories, copies config templates
(**existing files are never overwritten**), installs the helper
(root:root 0755), initializes the DB, generates a systemd unit from the
template, `daemon-reload`, `enable --now`.

## 4. Manual installation (the same, step by step)

4.1. Code and venv:
```bash
sudo git clone https://github.com/D-Stepenko/llm-panel.git /home/paneluser/llm-panel
sudo chown -R paneluser:paneluser /home/paneluser/llm-panel
cd /home/paneluser/llm-panel
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

4.2. Directories:
```bash
sudo mkdir -p /etc/llm-panel /var/lib/llm-panel
sudo chown paneluser:paneluser /var/lib/llm-panel && chmod 700 /var/lib/llm-panel
```

4.3. Configuration (templates → working files, then edit):
```bash
sudo install -m 644 deploy/config.yaml.example    /etc/llm-panel/config.yaml
sudo install -m 644 deploy/services.yaml.example  /etc/llm-panel/services.yaml
sudo install -m 644 deploy/allowlist.json.example /etc/llm-panel/allowlist.json
```
- `config.yaml`: `trusted_hosts` — the address/name the panel is opened from
  the browser with; `models.roots` — disk-summary paths (changed from the UI
  later); `systemd.panel_unit` — the unit of the panel itself: an llm-panel
  card on Status alongside the services (empty string — disable; actions
  require an allowlist entry).
- `services.yaml`: the units of YOUR server (see §6). systemd **user units**
  (`~/.config/systemd/user/*.service`) are not visible to the system manager
  — for them set `scope: user`; reading/actions/journal are performed by the
  panel directly with `systemctl --user` / `journalctl --user` (no helper or
  allowlist needed; the unit must belong to the same user the panel runs
  as).
- `allowlist.json`: the same units + allowed actions (including
  `llm-panel.service`, if a card of the panel itself with actions is
  desired).

4.4. The helper:
```bash
sudo install -o root -g root -m 755 deploy/helper/llm-panel-systemd /usr/local/sbin/llm-panel-systemd
```
Check that no directory in the path is writable by `paneluser`.

4.5. DB and password (must use the same configuration as the panel):
```bash
.venv/bin/python -m llm_panel --config /etc/llm-panel/config.yaml init-db
.venv/bin/python -m llm_panel --config /etc/llm-panel/config.yaml set-password
```

Account management (v0.3.16): the interactive script
`llm_panel/users_panel_config_.sh` — create a user / delete / change a
password / show all (run from the project directory, sudo not needed;
passwords entered interactively via getpass; the user's session is
invalidated on creation/password change; admin cannot be deleted).

4.6. **sudoers — a separate decision of the owner (spec §12.5).** Without it,
service control from the panel is impossible (status reading works, the
buttons do not). The right is exactly one: running the root-owned helper at a
fixed path, NOPASSWD. The risk: a passwordless root operation at a fixed
path; the second line of checks is the helper itself (allowlist).
```bash
sudo sed "s/^dispot /paneluser /" deploy/sudoers.llm-panel | sudo tee /etc/sudoers.d/llm-panel >/dev/null
sudo chmod 0440 /etc/sudoers.d/llm-panel && sudo visudo -c
# check:      sudo -n /usr/local/sbin/llm-panel-systemd status <your-unit>.service
# roll back:  sudo rm /etc/sudoers.d/llm-panel && sudo visudo -c
```

4.7. The panel's systemd unit (generated from the template):
```bash
sed -e "s|@@PANEL_USER@@|paneluser|g" -e "s|@@PANEL_GROUP@@|paneluser|g" \
    -e "s|@@PANEL_DIR@@|/home/paneluser/llm-panel|g" \
    -e "s|@@CONFIG_FILE@@|/etc/llm-panel/config.yaml|g" \
    deploy/llm-panel.service.template | sudo tee /etc/systemd/system/llm-panel.service >/dev/null
sudo systemctl daemon-reload && sudo systemctl enable --now llm-panel
```
**Warning:** do not add `NoNewPrivileges=yes` — the flag makes sudo
impossible, service control will break with "helper exited with code 1" (a
real case 2026-10-28). `ProtectSystem=strict` requires an explicit
`ReadWritePaths=` for the data directory. If the panel must write somewhere
else too (e.g. a models directory for HF downloads), add
`ReadWritePaths=/home/dispot/models` there as well — otherwise the child
`hf download` will get `Errno 30` (EROFS), even though the directory is
writable for a regular user (real case 2026-10-29: the unit's sandbox, not
permissions/FS).

## 5. Publishing through Caddy (HTTPS)

A separate site block with its own port (a `/panel/*` sub-path is not used —
the panel's absolute paths `/login`, `/static/…` do not work under it):
```caddy
192.168.1.10:8444 {
    tls internal
    @lan remote_ip 192.168.0.0/16
    handle @lan {
        reverse_proxy 127.0.0.1:8443
    }
}
```
```bash
sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy
```
Do not open UFW for 8443 (the upstream); 8444 — only from LAN addresses.

## 6. Adjusting for your server (units, GPUs, disks)

1. `nvidia-smi -L` → full card UUIDs (NOT indices: indices change between
   boots).
2. Fill in `services.yaml`: one block per unit (an annotated template —
   `deploy/services.yaml.example`, a real example — `deploy/examples/`).
   Write unit names exactly as in systemctl (case preserved).
3. Fill `allowlist.json` with the same units; for "view only" units leave
   `status`+`journal`.
4. Summary disks — in the Config Panel (`/config`) or initially in
   `models.roots`.
5. GPU/CPU/RAM are detected automatically, no configuration needed.
6. Configuration check: `.venv/bin/python -m llm_panel --config
   /etc/llm-panel/config.yaml check-config`

Adding a new service as a systemd unit (system or user) and showing it in
the panel — step by step in `docs/UNITS.md`; a quick unit addition without
yaml editing — "Search services" / "Add unit by name" in the Config Panel.

## 6.1. CPU power in the output line (RAPL)

The panel shows CPU consumption in watts from the Intel RAPL counters
(`/sys/class/powercap/intel-rapl:*/energy_uj`). The counter files have
`0400 root` permissions, and the panel runs as an unprivileged user inside a
systemd-unit sandbox — by default the counter is unreadable, and the panel
simply does not show watts (this is the normal "trustworthiness over
completeness" mode).

To make the watts appear, give the panel process **read-only** access to
such files — the `CAP_DAC_READ_SEARCH` capability:

```bash
# 1. Check (confirms the diagnosis — should be Permission denied):
sudo -u PANEL_USER cat /sys/class/powercap/intel-rapl:0/energy_uj

# 2. Read permission for the panel process:
sudo systemctl edit llm-panel
```

In the editor that opens, append:

```ini
[Service]
AmbientCapabilities=CAP_DAC_READ_SEARCH
```

**Do NOT add a `CapabilityBoundingSet=…` line** (station lesson 2026-10-31):
it REPLACES the entire capability set of the unit, after which `sudo` inside
the panel cannot switch to the root-gid and service control fails with
"helper exited with code 1: sudo: unable to change to root gid: Operation
not permitted · sudo: error initializing audit plugin sudoers_audit".
`AmbientCapabilities=` itself adds the capability to the
effective/permitted sets and creates no restrictions for sudo.

```bash
# 3. Apply:
sudo systemctl daemon-reload && sudo systemctl restart llm-panel
```

Notes:

- the capability grants **read-only** access, writing is not allowed — the
  sandbox (`ProtectSystem=strict`, `ReadWritePaths=…`) is not weakened;
- if the drop-in already contains
  `CapabilityBoundingSet=CAP_DAC_READ_SEARCH` — remove it
  (`sudo systemctl revert llm-panel` will revert the whole drop-in, then
  repeat steps 2–3 without that line);
- the watts appear from the second metrics poll (power is computed from the
  counter delta over an interval) — wait ~30–60 seconds after the restart;
- if there are still no watts: `systemctl show llm-panel -p
  NoNewPrivileges` (should be `no`) and `journalctl -u llm-panel -n 20`;
- on CPUs without RAPL (not Intel or an old kernel) no watts are shown at
  all — this is normal.

## 6.2. Server shutdown/reboot from the panel (v0.3.12)

In the Config Panel at the bottom there are "Reboot server" / "Shut down
server" buttons: a confirmation, a 5-second countdown with cancellation (a
"Cancel" button, pressing the same button again or Esc), then
POST /ui/power. The panel performs the operation through the same
root-owned helper: `sudo -n /usr/local/sbin/llm-panel-systemd power
<poweroff|reboot>` → `systemctl <poweroff|reboot>` as root. NO NEW
sudoers RULE IS NEEDED (the helper path is already allowed by sudoers).

For the buttons to work, the station needs the following (root files are
edited by the owner). The station update order — manually copying the ENTIRE
project directory (WinSCP/robocopy), without `git pull`:

```bash
# 0. Copy the fresh project directory to the station (from Windows):
#    robocopy "C:\...\llm-panel" \\llm\llm-panel /MIR
#    (or WinSCP: "Keep remote directory up to date"). Do NOT replace .venv!
#    Lesson 2026-11-01: without a fresh copy the install puts an OLD helper —
#    the buttons fail ("ERROR: unknown action", in the panel "helper
#    argument error"), because the power operation only appeared in 0.3.12.
#    A frequent cause of "cannot stat 'deploy/helper/llm-panel-systemd': No
#    such file or directory" — the install command was run NOT from the
#    project directory (prompt ~/ instead of ~/llm-panel).

cd /home/dispot/llm-panel

# 0.1. Check that the helper on the filesystem contains the power operation:
grep -c "POWER_ACTIONS" deploy/helper/llm-panel-systemd
#    expected: 1 or more; if 0 — the project copy is stale, re-copy.
```

```bash
# 1. The updated helper (power support):
sudo install -o root -g root -m 0755 deploy/helper/llm-panel-systemd \
     /usr/local/sbin/llm-panel-systemd

# 2. Allow power operations in the allowlist (root:root, safe default — refuse):
sudo python3 - <<'EOF'
import json
p = "/etc/llm-panel/allowlist.json"
with open(p, encoding="utf-8") as f:
    d = json.load(f)
d["power"] = {"reboot": True, "poweroff": True}
with open(p, "w", encoding="utf-8") as f:
    json.dump(d, f, ensure_ascii=False, indent=2)
EOF

# 3. Check the REAL allowlist without running the operation (dry-run as NON-root):
/usr/local/sbin/llm-panel-systemd --dry-run power reboot
#   expected: {"action": "power", "allowed": true, "argv": [...], "unit": "reboot"}
#   "REFUSED …" — the section did not apply; re-check step 2 and the file owner.
#   "ERROR: unknown action" — an OLD helper remains at /usr/local/sbin:
#   repeat step 1, always from the ~/llm-panel directory.
```

After updating the panel code — `sudo systemctl restart llm-panel` + Ctrl+F5.

Notes:

- disabling the operations: `"power": {"reboot": false, "poweroff": false}`
  or removing the section — the helper will refuse again;
- every attempt is written to the audit (`power:reboot` /
  `power:poweroff`);
- power operations also shut down the panel — after execution the page will
  stop responding, which is expected; access again after the host is up;
- protection: only an explicit allowlist section permits the operation, the
  check is duplicated in the helper (not only in FastAPI), the HTTP call
  requires a CSRF token and a session.

## 7. Version updates

```bash
cd /home/paneluser/llm-panel
git pull                                    # configs in /etc and the DB in /var/lib are not touched
.venv/bin/pip install -r requirements.txt   # if requirements changed
.venv/bin/python -m llm_panel --config /etc/llm-panel/config.yaml check-config
sudo systemctl restart llm-panel
```
If there is no git access from the server — copy the changed files from the
repository manually (do not replace `.venv`), then `restart`.

## 8. Emergency procedures

- Stop the panel without touching the LLM services:
  `sudo systemctl disable --now llm-panel` (the panel does not "own" the
  services — their units will not stop).
- Restoring access (login lockout):
  `.venv/bin/llm-panel --config /etc/llm-panel/config.yaml lockout-reset`
- Password reset: `llm-panel set-password` (interactive; all sessions are
  invalidated).
- Backup: `panel.db` (WAL) + `/etc/llm-panel/{config,services}.yaml`,
  `allowlist.json`. Restore: stop the panel, copy the files with the same
  permissions.
- Helper/sudoers rollback: delete the files, `visudo -c`, `daemon-reload`.

## 9. Post-installation checks

```bash
systemctl status llm-panel --no-pager          # the panel started as paneluser
ss -ltnp | grep 8443                           # listens ONLY on 127.0.0.1
curl -sk https://<IP>:8444/ -o /dev/null -w '%{http_code}\n'   # via Caddy — 302/200
curl -m 3 http://<IP>:8443/                    # expected: refusal/timeout (unreachable from outside)
sudo -n /usr/local/sbin/llm-panel-systemd status <your-unit>.service    # success
sudo -n /usr/local/sbin/llm-panel-systemd start stranger.service       # expected: REFUSED (rc=3)
```
In the UI: login, statuses, Start/Stop on an allowed unit, `/audit` contains
entries, `/config` applies settings without a restart.
