# Full setup guide

Assumes you've already flashed Raspberry Pi OS (Bookworm recommended) onto
the Pi Zero WH and it's on your Wi-Fi with SSH enabled.

## 1. Broker + preview

These two run together and are the two pieces meant for a server (your
Ubuntu box, a NAS, a cloud VM), not the Pi itself.

### Docker Compose (recommended)

This repo ships its own standalone `docker-compose.yml` with an
`eink-broker` and `eink-preview` service (the `eink-` prefix keeps them
from colliding with other stacks/versions you might run on the same
host). If you're running it on its own:

```bash
cp .env.example .env
nano .env               # set DASHBOARD_TOKEN -- see the comment in the file for how to generate one
docker compose up -d --build
curl http://localhost:9090/api/v1/health
```

Host ports default to `9090` (broker) and `9091` (preview) but are
configurable via `BROKER_PORT`/`PREVIEW_PORT` in `.env` if those are
already taken.

**Adding it to an existing Compose setup instead:** if you already run
other containers on this host, fold `eink-broker` and `eink-preview` in
as two more services rather than running a second, separate Compose
project. From this repo's root:

1. Copy (or merge) the two service blocks from `docker-compose.yml` into
   your existing compose file — `eink-broker` and `eink-preview`,
   unchanged, plus the `eink-broker-data:` named volume. Keep their
   `build.context` pointed at this repo's path (e.g.
   `context: ./eink_dashboard`, adjusting `dockerfile:` accordingly),
   since the build needs `shared/`, `broker/`, and `preview/` from here.
   If you ever need a second version of this stack running alongside it
   (e.g. for testing a change), just pick a different prefix, like
   `eink-broker-v2`, for that copy.
2. Add `DASHBOARD_TOKEN` to whatever `.env` file your existing compose
   setup already loads (or keep this repo's `.env.example` → `.env` as a
   separate file and reference it with `env_file:` on both services) —
   see the comment in `.env.example` for how to generate one.
3. Check the ports: `eink-broker` defaults to host port `9090` (container
   `8080`), `eink-preview` defaults to host port `9091` (container
   `9090`), both overridable via `BROKER_PORT`/`PREVIEW_PORT` in `.env`
   if either is already taken on this host — no file edits needed, just
   set those two vars.
4. If your existing setup already has a shared network your other
   containers use, put `eink-broker` and `eink-preview` on it too
   (`networks:` on each service) so they're reachable the same way as
   everything else you run; otherwise Compose's default network is
   enough for them to reach each other via `http://eink-broker:8080`
   (already set as `eink-preview`'s `BROKER_URL` — this is the internal
   container port, unaffected by `BROKER_PORT`).
5. Bring up just the new services without touching what's already
   running:

```bash
docker compose up -d --build eink-broker eink-preview
curl http://localhost:9090/api/v1/health
```

Useful commands going forward:

```bash
docker compose logs -f eink-broker eink-preview
docker compose up -d --build eink-broker eink-preview   # rebuild + restart after pulling code changes
```

Open `http://<this-machine's-LAN-IP>:9091` (or whatever `PREVIEW_PORT`
you set) from any browser on your network to see the preview page.

**Other machines need the LAN IP, not `localhost`.** The Pi client and
the Home Assistant/UPS publishers (none of which run in this compose file
— see sections 2, 3, and 4) connect to the broker from *outside* Docker's
internal network, so their `broker_url` needs this server's real address
and whatever `BROKER_PORT` you set, e.g. `http://192.168.1.50:9090`
(find the IP with `hostname -I` on this machine). Make sure your
firewall allows it:

```bash
sudo ufw allow 9090/tcp   # broker (or your BROKER_PORT)
sudo ufw allow 9091/tcp   # preview (or your PREVIEW_PORT), if you'll view it from another device
```

**Moving to a cloud VM later:** this is the same reason it's worth doing
now, locally, first — once you're happy with your layout and widgets,
copying this repo to a cloud VM and running the identical
`docker compose up -d --build` just works. The only things that change
are: put it behind HTTPS (a reverse proxy like Caddy makes this close to
zero-effort) rather than exposing plain HTTP with a bearer token to the
open internet, and open the cloud provider's firewall/security group for
whatever port ends up public instead of (or in addition to) `ufw`.

### Alternative: without Docker

```bash
# Broker
cd broker
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml   # generate + set a token, see the comment in the file
./venv/bin/python app.py
# or, for production: ./venv/bin/uvicorn app:app --host 0.0.0.0 --port 8080

# Preview (separate terminal/machine)
cd preview
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml   # same token, broker_url pointing at the broker
./venv/bin/python server.py
```

Keep either running with systemd:

```ini
# /etc/systemd/system/eink-broker.service  (same pattern for eink-preview)
[Unit]
Description=E-Ink Dashboard Broker
After=network-online.target

[Service]
WorkingDirectory=/path/to/eink_dashboard/broker
ExecStart=/path/to/eink_dashboard/broker/venv/bin/python app.py
Environment=DASHBOARD_TOKEN=your-generated-token
Restart=on-failure
User=youruser

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now eink-broker
```

## 2. Home Assistant publisher

Get a long-lived access token: in Home Assistant, click your profile
(bottom-left) → **Security** tab → **Long-Lived Access Tokens** → **Create
Token**.

```bash
cd publisher_ha
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml       # ha_url, ha_token, broker_url, broker_token
cp entities.example.yaml entities.yaml   # map YOUR entity ids to widgets
./venv/bin/python publish.py
```

It connects to HA's WebSocket API, grabs the current state of everything
once, pushes it to the broker, then keeps watching for changes and
pushes updates as they happen (debounced ~2s to coalesce bursts). Run this
as a systemd service too (same pattern as the broker above) or in
whatever you already use to run long-lived scripts near your HA instance.

Check `docs/WIDGETS.md` for what each widget type in `entities.yaml`
expects, and edit `shared/dashboard_render/layout.example.yaml` (or make
your own layout file and point the broker's `layout_file` config at it)
to change what's actually on screen.

## 3. UPS publisher (apcaccess, cron)

If you run `apcupsd` for a UPS (APC or compatible), `publisher_ups/publish_ups.py`
pushes battery charge, load %, and an on-battery alert to the broker. Unlike
the Home Assistant publisher, this one is a **one-shot script meant for
cron**, not a long-running daemon -- apcupsd already polls the UPS
continuously, this just asks it for the latest snapshot and pushes it.
Pure standard library, no venv or pip install needed -- just `python3` and
the `apcaccess` binary on PATH (from `apt install apcupsd`).

```bash
cd publisher_ups
python3 publish_ups.py --dry-run   # parses apcaccess + prints the payload, doesn't push
```

Wire it into cron once that looks right:

```bash
crontab -e
```

```cron
*/2 * * * * BROKER_URL=http://localhost:9090 DASHBOARD_TOKEN=your-token /usr/bin/python3 /path/to/eink_dashboard/publisher_ups/publish_ups.py >> /var/log/ups_publish.log 2>&1
```

(swap `localhost:9090` for wherever the broker actually lives, and the real
token -- see `docs/WIDGETS.md`/section 1 above for both). If apcupsd runs on
a different host than this script, set `APCACCESS_HOST=ip:3551` too.

It pushes three widgets -- `ups_battery` (progress bar), `ups_load`
(metric), `ups_alert` (alert_banner, invisible unless the UPS isn't
`ONLINE`) -- so your `layout.yaml` needs matching entries, e.g.:

```yaml
  # optional -- draws a border + "UPS" label around the two widgets below;
  # see docs/WIDGETS.md#panel-decorative-grouping-box
  - id: ups_panel
    type: panel
    x: 0
    y: 3
    w: 6
    h: 2
    title: "UPS"

  - id: ups_battery
    type: progress
    x: 0
    y: 3
    w: 3
    h: 2
    title: "UPS Battery"

  - id: ups_load
    type: metric
    x: 3
    y: 3
    w: 3
    h: 2
    title: "UPS Load"

  - id: ups_alert
    type: alert_banner
    x: 0
    y: 5
    w: 12
    h: 1
```

Adjust `x`/`y`/`w`/`h` to fit wherever's free in your actual layout, and set
`UPS_WIDGET_PREFIX` (default `ups_`) if you'd rather use different ids --
just keep the layout's `id`s matching whatever the script pushes.

## 4. Pi client (the physical display)

### Hardware assembly

- **Case:** the [EPaper Dashboard Waveshare 10.85" case on
  MakerWorld](https://makerworld.com/en/models/2322517-epaper-dashboard-waveshare-10-85)
  is sized for the same 1360×480 panel, so the physical fit should be the
  same. Its bundled dashboard *software*, however, is written for the
  plain black/white HAT+ (which supports fast partial refresh) — don't use
  that code with the (G) 4-color panel; use this repo's `pi_client`
  instead, which is built around the (G) variant's full-refresh-only,
  refresh-rate-limited behavior.
- Follow Waveshare's own manual for connecting the HAT+ to the Pi Zero WH
  and the ribbon cable to the panel — handle the panel/cable gently, no
  force.
- Enable SPI (the install script below does this for you, or manually via
  `sudo raspi-config` → Interface Options → SPI → Enable).

### Software

```bash
git clone <this-repo-url>
cd eink_dashboard/pi_client
chmod +x install.sh
./install.sh
```

This enables SPI, installs system + Python dependencies, vendors the
official Waveshare driver (`waveshare_epd/`, cloned fresh from
`github.com/waveshare/e-Paper`), sets up `config.yaml` from the example,
and installs + enables a systemd service so the dashboard starts on boot
and restarts if it crashes.

Edit `config.yaml`:

```yaml
broker_url: "http://YOUR-BROKER-HOST:9090"   # or your BROKER_PORT / native port, if different
token: "same token as the broker"
```

Then:

```bash
sudo systemctl start eink-dashboard
journalctl -u eink-dashboard -f      # watch it connect + render
```

### Testing without the physical panel

Set `dry_run: true` in `pi_client/config.yaml` (or run
`DASHBOARD_TOKEN=... BROKER_URL=... ./venv/bin/python client.py` on any
machine, not just the Pi) — it writes each rendered frame to
`preview_frame.png` instead of touching SPI, so you can validate the
whole pipeline (broker connection, debouncing, rendering) before ever
touching hardware. The preview web app from section 1 is a nicer version
of the same idea, meant to be left running continuously — it updates live
as the broker's state changes (no debouncing — that only applies to the
physical panel), so it's the fastest way to iterate on layout changes.
