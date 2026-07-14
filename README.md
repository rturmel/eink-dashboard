# E-Ink Home Dashboard

Drives a Waveshare 10.85" e-Paper HAT+ (G) — 1360×480, 4-color (black /
white / red / yellow) — from a Raspberry Pi Zero WH, showing information
pulled from Home Assistant (or anything else). The Pi knows nothing about
Home Assistant; it just renders whatever a small central service tells it
to, which is what lets it be reprovisioned anywhere with Wi-Fi.

**Sample mockups** (rendered with the real pipeline, not hand-drawn) are in
[`docs/mockups/`](docs/mockups/) — a realistic full dashboard, a gallery of
every widget type (metrics, weather, bar/pie charts, a logo image, alerts,
etc.), a reference of the 4 available colors, and a demo of dithered color
blends (orange/pink/gray, approximated from the 4 real colors — see
[`docs/WIDGETS.md`](docs/WIDGETS.md#colors-solid-vs-dithered-blends)).
Regenerate them anytime with `python3 docs/mockups/generate_mockups.py`.

## Architecture

```
 Home Assistant  ─┐
                   │  (or a cron job, a phone shortcut, anything)
 other data source ┤
                   │  HTTP POST (bearer token)
                   ▼
            ┌───────────────┐   WebSocket push    ┌──────────────┐
            │    Broker      │ ───────────────────▶│  Pi client    │──▶ e-Paper
            │ (FastAPI)      │                      │ (client.py)   │    HAT+ (G)
            │ - holds state  │ ───────────────────▶│  Preview web  │──▶ browser
            │ - holds layout │                      │  (server.py)  │
            └───────────────┘                      └──────────────┘
```

- **broker/** — the single source of truth. Publishers `POST` widget data
  to it; any number of clients (the Pi, the local preview, a second Pi)
  connect over WebSocket and get pushed every update. Host this wherever
  is convenient — on your home server, in a container, on a small AWS
  instance. It has no Home-Assistant-specific code at all.
- **publisher_ha/** — connects to Home Assistant's WebSocket API, watches
  the entities you list in `entities.yaml`, and pushes just those fields
  to the broker. This is the only piece that needs your HA URL/token.
- **publisher_ups/** — a one-shot cron script (not a daemon) that reads
  `apcaccess status` and pushes battery/load/on-battery-alert to the
  broker. Any number of publishers like this can coexist — they just need
  to write to different widget ids.
- **publisher_rooms/** — another one-shot cron script; fetches Bluetooth
  thermometer/hygrometer readings (temp/humidity/battery per room) from
  Home Assistant's REST API and pushes a table to the broker.
- **pi_client/** — runs on the Raspberry Pi. Connects to the broker,
  renders the dashboard, and pushes it to the physical panel — rate
  limited to protect the hardware (see "About this display" below).
  Generic: point it at any broker URL and it works, no code changes.
- **preview/** — a small local web app that connects to the broker the
  same way the Pi does, but renders to a browser page instead of SPI, so
  you can see what the panel will show without waiting on it.
- **shared/dashboard_render/** — the actual drawing code (Pillow-based),
  used identically by pi_client and preview so what you see in the
  browser matches what shows up on the panel. `layout.example.yaml` here
  is a demo/reference file (what "Load demo data" targets); your real
  dashboard belongs in its own `layout.yaml`, with the broker pointed at
  it — see `docs/SETUP.md`. Edit whichever one to change what's on screen
  without touching any Python.

## About this display

The 10.85" HAT+ **(G)** variant is a true 4-color panel (not grayscale)
and, unlike the plain black/white HAT+, it does **not** support partial
refresh — every update is a full-panel redraw, about 21 seconds. Waveshare
recommends refreshing multi-color panels like this no more than roughly
once every 3 minutes, and at least once every 24 hours (to avoid ghosting
or burn-in from a static image sitting too long). `pi_client` builds this
in by default:

- `min_refresh_interval_seconds: 180` — rapid data changes get coalesced
  into a single refresh instead of hammering the panel.
- `force_refresh_seconds: 21600` (6h) — the panel redraws periodically
  even with no new data, so it's never left stale for 24h+.

The broker and preview are **not** rate-limited — only the physical panel
push is. So the preview may show updates a few minutes before the real
display catches up; that's intentional.

## Quick start: broker + preview (Docker Compose)

```bash
cp .env.example .env          # then edit .env and set a real token
docker compose up -d --build
curl http://localhost:9090/api/v1/health
```

Open `http://localhost:9091`, click **Load demo data**, and you should see
a fully rendered dashboard with sample values. The same `docker-compose.yml`
works unchanged on a cloud VM later — copy the repo over and run the same
command. See `docs/SETUP.md` for the full walkthrough, including how the
Pi client and Home Assistant publisher (which run outside this compose
file — see below) find the broker.

Don't want Docker? `docs/SETUP.md` also has the plain `venv` + `systemd`
path for the broker and preview.

From here:

- Edit `shared/dashboard_render/layout.example.yaml` to change what
  widgets exist and where — reload the preview to see it. Check **Show
  grid** in the preview page for a numbered `x,y` overlay matching
  `layout.yaml`'s coordinate system, to help plan where new widgets go.
- See `docs/WIDGETS.md` for what data each widget type expects.
- See `docs/SETUP.md` for wiring up the real Home Assistant publisher and
  the physical Pi client (including the systemd auto-start service) —
  neither of those run in Docker: the Pi client needs direct SPI/GPIO
  hardware access, and the publisher is meant to run wherever's
  convenient, often right next to Home Assistant itself.

## Repo layout

```
broker/                  central hub service (FastAPI)
publisher_ha/             Home Assistant -> broker bridge
publisher_ups/             apcupsd -> broker cron script
publisher_rooms/           HA REST API room sensors -> broker cron script
pi_client/                 runs on the Pi, renders + pushes to hardware
preview/                   local web preview
shared/dashboard_render/   rendering + widget library used by both clients
docs/                      setup + widget reference
docker-compose.yml         runs broker + preview together (see Quick start)
.env.example                template for the shared auth token Compose needs
```

## Security note

The broker's only auth is a shared bearer token (`DASHBOARD_TOKEN`, set in
`.env` for the Docker Compose path, or `config.yaml`/env var for the
native path). That's fine on a trusted home LAN. If you host the broker
somewhere reachable from the open internet (e.g. a cloud VM), put it
behind HTTPS (a reverse proxy like Caddy/nginx, or a cloud load balancer
with a TLS cert) so the token isn't sent in the clear, and use a long
random token (see the comment in `.env.example` for how to generate one).
`.env` is gitignored — don't commit it.
