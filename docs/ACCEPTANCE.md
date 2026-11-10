# llm-panel acceptance tests (browser, station)

> **A note from the author.** This project was developed by a Russian-speaking
> author; the code comments and the `*_RU.md` documents are in Russian — please
> treat them with understanding and forbearance. English documents are
> sense-faithful translations; a fully localized English variant may follow if
> the project attracts interest.

A manual browser-based acceptance scenario. Environment: station 10.10.100.44,
panel v0.3.16+, access via browser over an SSH tunnel
`http://127.0.0.1:8443/` (trusted_hosts contains 127.0.0.1). Account: a
dedicated test user (created via `llm_panel/users_panel_config_.sh`).

Accepted by the owner (2026-11-01): server SHUTDOWN/REBOOT are NOT tested
(dry-run in the helper tests already covers the validation). Dangerous
actions (start/stop/restart of real services, user deletion) — only after
the owner's explicit approval.

## 1. Login and security

| # | Step | Expected |
|---|------|----------|
| 1.1 | Open `/login` (log out first) | Form without lockout explanations (the text was removed in 0.3.17); no error |
| 1.2 | Login with a WRONG password (once) | "Wrong username or password." + "Attempts left: 4." |
| 1.3 | Login with the correct password | Redirect to `/`; topbar, card header |
| 1.4 | "Log out" | Redirect to `/login`; going back to `/` is not possible (302) |
| 1.5 | Check the cookie | `llm_panel_session` — HttpOnly; CSP header in devtools, no errors |

## 2. Main page (/): service cards

| # | Step | Expected |
|---|------|----------|
| 2.1 | Full window width (≥1200px) | Service cards in 3 columns; GPU tiles in 3 equal-width columns; the 3rd and 4th cards are NOT stretched |
| 2.2 | Field rows on a card | "Port 0.0.0.0:8000   Uptime —" #1; "Restarts 0   Systemd inactive/dead" #2; columns vertically aligned, no borders; the labels "loopback only…", "detected automatically", "· finished (code 0)" — absent |
| 2.3 | GPU strip of the card | GPU label aligned with column 1 (one vertical with "Port"); card chips whole (the name "Tesla V100-SXM2-32GB" does not wrap); 5px between chips |
| 2.4 | Narrow the window to ~1000px | Service cards in 2 columns, GPU tiles in 2 columns |
| 2.5 | Narrow to ~760px | Everything in 1 column; the GPU strip keeps 10+px from the border |
| 2.6 | Check the pill | Neutral border; theme-aware shading |
| 2.7 | Bindings in a GPU tile | "Services:"/"planned binding" as separate rows UNDER the grid (gpu-binds), not in the gauge columns |
| 2.8 | Service settings (v0.3.18) | "Reset failed status" and "Daemon reload" buttons present; system unit without allowlist entry for the action → clear refusal, no errors in the console |

## 3. GPU tiles (main page)

| # | Step | Expected |
|---|------|----------|
| 3.1 | "Clean line" gauges | Height 10px, 1px border; aligned to the longest label; percentages with theme-aware shading |
| 3.2 | VRAM/Temp | Compact rows; when squeezed, the gauge compresses and does not escape the border |
| 3.3 | Vendor borders | NVIDIA — green (base); AMD — red; Intel — blue; in both themes |
| 3.4 | UTC clock / refresh | SSE: values/time update without a reload (observe 10–15 s) |

## 4. Themes and language

| # | Step | Expected |
|---|------|----------|
| 4.1 | Config Panel → light theme | All cards/gauges/power buttons correct in the light theme too |
| 4.2 | Back to dark | No artifacts |
| 4.3 | Switch language to EN | Field/GPU/button rows are translated; the power countdown in EN |
| 4.4 | Back to RU | No artifacts |

## 5. Matrix (/matrix)

| # | Step | Expected |
|---|------|----------|
| 5.1 | Open | The main table — only services with cards; the rest under "Without GPU binding"; conflicts/"no conflicts" — between the tables |
| 5.2 | Binding snippets | UUID snippets of CVD/`gpu[]`, a copy button |
| 5.3 | Card names | Full NVML names (not UUID tails) |

## 6. Models (/models)

| # | Step | Expected |
|---|------|----------|
| 6.1 | Catalog | GGUF shards grouped; HF directories with quant/dtype; one entry per directory (dedup by realpath) |
| 6.2 | Ordering + sort | Manual drag&drop order: enable the mode, drag, "Save order" (changes the saved display order — SAFE) |
| 6.3 | HF downloads | hf availability from Config Panel; do NOT start a download (dangerous); the stop buttons are visible/translated |

## 7. Wizard (/wizard) — up to the last step only

| # | Step | Expected |
|---|------|----------|
| 7.1 | Step 1 | A model is selectable from the catalog |
| 7.2 | Step 2 | The engine is preselected by kind (GGUF→llamacpp, safetensors→vllm); cards labeled "planned/busy" |
| 7.3 | Step 3 | A free port with an occupancy check; do NOT press the final generation twice (safe: it only writes to the audit, the YAML is displayed) |
| 7.4 | Exit without saving | The "Back" button/closing the page — no changes |

## 8. Logs (card → "Journal")

| # | Step | Expected |
|---|------|----------|
| 8.1 | Open the journal of a stopped unit | A readable reason/empty; the red "no event" flow does not hang the page |
| 8.2 | Line limits | The "lines" field works |
| 8.3 | Close the journal tab | No dangling connections in devtools |

## 9. Config Panel (/config)

| # | Step | Expected |
|---|------|----------|
| 9.1 | Language/interval/audit limit | Saving returns a "Saved" notice; changes behavior |
| 9.2 | Disk summary paths | Changed without writing root files (own storage) |
| 9.3 | Path to hf | Validation: a non-existent path → err redirect; a working one — ok |
| 9.4 | Adding a unit by name | Returns a permissions block with a copyable command and, for the admin user, instructions; does NOT duplicate the start |
| 9.5 | Power buttons (DO NOT EXECUTE) | The countdown "… in 5 s": the tick is visible; do NOT wait for the countdown itself to fire — press "Cancel", then a repeat press and Esc; the systemctl commands are NOT sent (test the cancellation only) |

## 10. Config files (/config/files) — read-only

| # | Step | Expected |
|---|------|----------|
| 10.1 | Open | List of dispot-owned files; the editor does NOT open root files (services.yaml is unavailable) |
| 10.2 | diff/versions | Viewing versions WITHOUT rollback (safe) |

## 11. Audit

| # | Step | Expected |
|---|------|----------|
| 11.1 | Open the audit | Recent actions login/logout visible in the entries; the password is NOT recorded |

## 12. Users (`llm_panel/users_panel_config_.sh`) — on a separate request

| # | Step | Expected |
|---|------|----------|
| 12.1 | §4 as a list | The `check` account (the owner looks at it themselves) |
| 12.2 | Create/delete — FORBIDDEN without approval | Tested with a test version (offline), NOT on the live database |

## 13. Console errors

| # | Step | Expected |
|---|------|----------|
| 13.1 | Walk through all pages, devtools console | No JS errors (except expected SSE reconnects during a restart) |
