# E-Ink Dashboard — project context

Handoff document. Everything needed to work on this project without prior conversation history.

---

## What this is

A dashboard on a Waveshare 10.85" e-Paper panel driven by a Raspberry Pi. Data sources push to a central broker; the Pi renders whatever the broker tells it to. The Pi knows nothing about any specific data source, so it can be reprovisioned anywhere with Wi-Fi.

```
 publishers ──HTTP POST (bearer token)──> broker ──WebSocket push──> pi_client ──> e-Paper
                                            │
                                            └────────────────────> preview (browser)
```

---

## Repo layout

```
broker/                  FastAPI hub. Holds widget state + layout. Port 9090.
preview/                 Browser preview, renders identically to the panel. Port 9091.
pi_client/               Runs on the Pi. Connects to broker, renders, drives the panel.
shared/dashboard_render/ Pillow-based rendering + widget library. Used by pi_client AND preview.
publisher_ha/            Home Assistant WebSocket -> broker
publisher_ups/           apcaccess -> broker (cron)
publisher_rooms/         HA REST API room sensors -> broker (cron)
publisher_zabbix/        Zabbix JSON-RPC -> broker (cron)
publisher_weather/       Open-Meteo / Environment Canada GEM -> broker (cron)
publisher_pi_temp/       Pi's own temp sensor -> broker (cron, runs ON the Pi)
docs/SETUP.md            Full setup walkthrough
docs/WIDGETS.md          Widget types and the data each expects
UPGRADE.md               Planned multi-unit / multi-screen architecture (design only)
PANEL_ISSUE_NOTES.md     Hardware investigation notes — read before debugging the panel
```

`docker-compose.yml` runs broker + preview together. Publishers and pi_client run outside it.

---

## Hardware

| | |
|---|---|
| Panel | Waveshare 10.85" e-Paper HAT+ **(G)** — 1360×480, 4-colour (black/white/red/yellow), dual-IC |
| Host | Raspberry Pi 3 Model B |
| OS | **Raspberry Pi OS Bookworm (Debian 12), 32-bit** |
| Power | 5.1V regulated supply (verify `vcgencmd get_throttled` returns `0x0`) |

### Critical constraints

**The panel does not work on Raspberry Pi OS Trixie.** `Init()` hangs forever waiting on BUSY after command `0x04` (POWER_ON). Bookworm works with no other changes. This cost weeks to find — see `PANEL_ISSUE_NOTES.md`. If the panel hangs at init, check the OS version before anything else.

**The client must run as root.** The vendored `epdconfig.py` loads a precompiled `DEV_Config_*.so` built against the bcm2835 library, which maps `/dev/mem` directly. As a normal user it doesn't fail cleanly — it dies with SIGSEGV before any Python traceback.

**Vendor the driver from Waveshare's official zip, not GitHub.** `install.sh` downloads `10.85inch_e-Paper_G.zip` from the product wiki. The GitHub repo is a different revision and its Python driver has not been verified working here.

**No partial refresh.** The (G) is 4-colour; only some black-and-white panels support partial updates. Every change is a full ~19–21 second redraw with heavy flickering (normal — it's clearing residual charge).

**Refresh rate limits** (Waveshare guidance, already implemented in `client.py`):
- `min_refresh_interval_seconds: 180` — coalesce rapid changes
- `force_refresh_seconds: 21600` — redraw at least every 6h so it's never stale past 24h

**Never leave the panel energised.** If a refresh is interrupted, run `pi_client/reset_panel.py` — it resets, sends POWER_OFF + DEEP_SLEEP, and drops PWR, with no busy waits so it can't itself hang. The systemd unit calls it via `ExecStopPost`.

---

## Auth and networking

- Single shared bearer token, `DASHBOARD_TOKEN` in `.env` (gitignored). Same token for every publisher and client.
- WebSocket auth is a **query parameter**, not a header: `ws://host:9090/ws?token=<token>`
- Health check: `GET /api/v1/health`
- Broker is exposed publicly via Cloudflare Tunnel at `broker.static-free.net` → `http://localhost:9090`. No special tunnel config is needed for WebSockets; `cloudflared` proxies the upgrade automatically. External clients use `wss://`.

**Outstanding:** the token has been shared in plaintext during debugging and should be rotated before production. Generate with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`, then update `.env` plus every publisher and `pi_client/config.yaml`.

---

## Running it

**Broker + preview:**
```bash
cp .env.example .env      # set DASHBOARD_TOKEN
docker compose up -d --build
curl http://localhost:9090/api/v1/health
```
Preview at `http://localhost:9091` — "Load demo data" renders `layout.example.yaml`.

**Pi client:**
```bash
cd pi_client
chmod +x install.sh && ./install.sh    # warns if not on Bookworm
nano config.yaml                        # broker URL, token, dry_run: false
sudo ./venv/bin/python client.py --config config.yaml
```

Service: `sudo systemctl {start,status,restart} eink-dashboard`, logs via `journalctl -u eink-dashboard -f`.

**Dry run** (`dry_run: true` in config.yaml) renders to `preview_frame.png` instead of touching hardware — use this for anything not specifically testing the panel.

---

## Layouts

`shared/dashboard_render/layout.yaml` is the real dashboard; `layout.example.yaml` is the demo target and reference. Widgets are placed on a grid — check "Show grid" in the preview for a numbered `x,y` overlay. Changing what's displayed needs no Python.

See `docs/WIDGETS.md` for widget types and their expected data shapes.

---

## Planned work

`UPGRADE.md` contains a full design for multi-unit / multi-screen support: named units, multiple screens per unit with client-owned rotation, panel profiles for different resolutions and colour depths, per-unit auth tokens, and broker state persistence. Nothing in it is implemented. It also covers Let's Encrypt / Caddy for `panels.richardturmel.net`, which the Cloudflare tunnel has since made redundant for the broker specifically.

Note the multi-unit plan assumed Pi Zeros per site. This panel is only verified on a 3B; the Zero was never successfully tested (all Zero testing predated the Bookworm discovery, so it may well work — untested).

---

## Debug artefacts

In `pi_client/`, kept from the hardware investigation. None are imported by the working client:

- `epd_trace.c` — instrumented init with per-stage BUSY sampling and bounded waits
- `epd_matrix.c` — statistical harness; runs a variant N times, reports success rates
- `epd_prime.c` — deterministic POWER_OFF/DEEP_SLEEP leaving PWR asserted
- `epd10in85g_fixed.py` — Python driver with C-matching CS handling and reset timing
- `epd10in85g_clib.py` — ctypes wrapper around Waveshare's C driver

`WAVESHARE_SUPPORT_REPORT.md` was written when the panel appeared faulty. The root cause turned out to be the OS, so it's obsolete except as a record.

---

## Conventions

- Comments explain *why*, not *what* — particularly around driver quirks, where the reasoning is non-obvious and expensive to rediscover
- `v1.0` is tagged at the first fully working state; tags are immutable, cut a new one rather than moving it
- Panel-affecting changes get tested with `dry_run: true` first
