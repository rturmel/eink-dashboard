# Upgrade plan: multi-unit, multi-screen, hosted broker

**Status: design decisions only, nothing in this document is implemented yet.**
This records what was decided and why, so a future implementation pass (or
a future me) doesn't have to re-derive the reasoning. Current actual
behavior is still single-dashboard, single-screen, LAN-only, as described
in `README.md` and `docs/SETUP.md`.

## Goals

- One broker serving multiple physical e-ink units, each independently
  identified (`unit_id`).
- Each unit cycles through multiple "screens" (independent widget layouts)
  on its own schedule, rather than showing one fixed layout forever.
- Support units with different physical panels (resolution, color count,
  full-refresh-only vs. partial-refresh-capable) without a future rework.
- Move the broker off the home LAN to a small always-on host, reachable
  over the internet by units at other physical locations (a friend's
  store, other people's houses), with TLS and per-unit auth since it's no
  longer a single-trust-domain LAN.

## 1. Multi-tenant broker: units and screens

**Identifiers.** `unit_id` and `screen_id` are slug strings (e.g. `"home"`,
`"lilia-store"`, `"main"`, `"docker-status"`), not numeric IDs — no
functional benefit to a numeric mapping layer at this scale; revisit only
if there's a real reason to obscure unit names later.

**Data model.** The broker's state goes from one flat `{widget_id: data}`
to:

```
{
  unit_id: {
    display_name: "...",
    panel_profile_id: "waveshare_10in85_g",   # see section 3
    token: "...",                              # see section 4
    rotation: [ ... ],                         # see section 2 -- client-owned, broker just stores/relays it
    screens: {
      screen_id: {
        layout: { ... },   # same shape as today's layout.yaml
        state: { widget_id: {...}, ... },
      },
      ...
    },
  },
  ...
}
```

**Tying a screen name to a specific layout file.** The `layout` in the
data model above is loaded from disk at broker startup, not pushed by a
publisher — same as today's hand-edited `layout.yaml`, just organized for
many screens across many units instead of one file. Convention: one
directory per unit, one file per screen, **filename is the identifier**:

```
shared/dashboard_render/layouts/
  home/
    unit.yaml           # display_name, panel_profile_id, rotation (section 2), token ref
    main.yaml            # screen_id "main"   -- today's layout.yaml, moved here unchanged
    docker-status.yaml    # screen_id "docker-status"
  lilia-store/
    unit.yaml
    main.yaml
    weekly-special.yaml
  ...
```

- The broker discovers units by scanning `layouts/*/unit.yaml`, and each
  unit's screens by scanning every other `*.yaml` file in that unit's
  directory — the filename (minus `.yaml`) *is* the `screen_id`, no
  separate id field inside the file. One source of truth: rename the
  file, the screen's id changes; there's nothing else to keep in sync.
- `unit.yaml` deliberately does **not** enumerate its screens in a list —
  that would just be a second place that could drift out of sync with
  what files actually exist. The directory listing is the list.
- Under this convention, today's single dashboard becomes exactly
  `layouts/home/main.yaml` — matching the `unit=home, screen=main`
  default from the backward-compatibility note below. Migration is
  "move the file," not "rewrite it."
- `layout.example.yaml` (the "Load demo data" file) stays exactly where
  it is, outside this tree — it isn't a real unit/screen, just demo
  content, unrelated to this convention.
- **Validation worth adding:** at load time, confirm every screen name
  listed in a unit's `rotation` (section 2) actually has a matching
  layout file in that unit's directory — catches a typo'd screen name in
  the rotation schedule before it ever reaches a physical panel.

**API.** Widget pushes become unit+screen scoped:

- `POST /api/v1/units/<unit_id>/screens/<screen_id>/widgets/<widget_id>`
- `POST /api/v1/units/<unit_id>/screens/<screen_id>/widgets/bulk`
- `GET /api/v1/units` — lists units + their screens, for the preview
  dropdown (see below). New endpoint, doesn't exist today.
- WebSocket: `ws://broker/ws?token=...&unit_id=<unit_id>` — a connection
  is scoped to one unit and receives that unit's `full_state` (**all** of
  that unit's screens' data in one push, not just the currently-displayed
  one — see section 2 for why).

**Backward compatibility during migration.** Every publisher gets two new
env vars, both defaulted: `UNIT_ID` (default `"home"`) and `SCREEN_ID`
(default `"main"`). Existing publisher cron lines keep working unchanged
unless explicitly pointed at a different unit/screen.

**Preview.** Gains a unit/screen dropdown (backed by `GET /api/v1/units`)
for debugging — picks a specific unit+screen directly, bypassing rotation
entirely (rotation is a physical-panel-cadence concern, irrelevant to a
browser debug view). Preview authenticates with a separate, elevated
token that can see all units, distinct from any single unit's token.

## 2. Client-owned timing: rotation + partial refresh

**Decision: `client.py` owns all refresh/rotation timing, not the
broker.** Reasoning: this cadence has to be coupled to the physical
refresh-rate-limiting logic that already lives client-side
(`min_refresh_interval_seconds` / `force_refresh_seconds`) to protect the
hardware — splitting that across broker and client would be fighting the
existing design for no benefit. The broker's only job stays "hold the
latest state per unit/screen and push it out" exactly like today.

**Rotation (full refresh, screen-to-screen).** Each screen gets its own
dwell duration, not a uniform interval:

```yaml
rotation:
  - screen: main
    duration_seconds: 300      # 5 min
  - screen: docker-status
    duration_seconds: 600      # 10 min
  - screen: main
    duration_seconds: 300
```

Client advances to the next entry when the current one's `duration_seconds`
elapses, does a full refresh, resets the dwell clock. A flat list like this
trivially expresses both "alternate evenly" and "weighted repeats"
(`main` can appear more than once) — no separate weighting field needed.

**Partial refresh (within a screen, while it's up).** New optional
per-widget layout flag: `partial_refresh: true` (same style as
`pie_chart`'s existing `legend: "below"` option). While a screen is the
one currently displayed, if the client's live WebSocket state updates a
widget flagged this way, it diffs just that widget's bounding box and does
a small partial panel write — rate-limited by the panel profile's
`min_partial_interval_seconds`, batching multiple hot widgets that changed
within the same window into one combined-bbox write rather than firing
several in a row. Widgets without the flag just wait for the next full
rotation, like everything does today.

**Push cadence is unaffected.** Publishers push whenever they push; the
broker doesn't know or care about rotation/partial-refresh timing for any
unit. The client was always free to look at fresher data than what's on
the panel — partial refresh just gives it a legitimate, hardware-safe
reason to act on that between rotations.

**Three independent client-side timers**, all in `client.py`:
1. Rotation dwell (per screen) — decides *which screen* is showing.
2. Partial-refresh interval (per currently-displayed screen's hot
   widgets) — small in-place updates, same screen stays up.
3. Ghosting-prevention forced full refresh
   (`max_partial_refreshes_before_full`, from the panel profile) — a full
   redraw of *whatever screen is currently up*, not a rotation event.

## 3. Panel profiles (multi-resolution / multi-color support)

Designed in now specifically so a second panel model later needs zero
code change, only a new registry entry.

**Shared registry**, e.g. `shared/dashboard_render/panel_profiles.yaml`,
loaded identically by broker, `pi_client`, and preview (same pattern
already used for layouts):

```yaml
waveshare_10in85_g:
  width: 1360
  height: 480
  colors: [black, white, red, yellow]
  refresh_mode: full_only
  min_refresh_interval_seconds: 180
  force_refresh_seconds: 21600

waveshare_4in2_bw:
  width: 400
  height: 300
  colors: [black, white]
  refresh_mode: partial_capable
  min_partial_interval_seconds: 5
  max_partial_refreshes_before_full: 20
  force_full_refresh_seconds: 3600
```

- `panel_profile_id` is a property of the **unit** (the physical
  hardware), not of a screen — every screen a unit owns inherits its one
  panel profile.
- `render_dashboard()` takes canvas size + palette as parameters resolved
  from the unit's profile, instead of the current hardcoded 1360×480
  4-color constants. Parametrization, not a rewrite.
- The `colors` list (rather than a boolean "is color") also covers true
  grayscale panels later (`[black, white, gray25, gray50, ...]`) for
  free — not needed now, just confirms the shape holds up.
- **Not solved by this alone:** a screen's `layout.yaml` still has to be
  authored with its target panel's aspect ratio/grid size in mind.
  Parametrizing the renderer doesn't make one layout automatically look
  right on two different resolutions.
- **Validation worth adding:** when loading a screen against its unit's
  panel profile, check every widget's `color`/`fill` value against that
  profile's declared `colors` list — catches "used red on a B/W unit" at
  config-load time instead of it rendering wrong on real hardware.
- **Driver abstraction** (`pi_client/epd_display.py`): needs a small
  interface — `full_refresh(image)` always, `partial_refresh(image, bbox)`
  only for `partial_capable` profiles — with the model-specific
  implementation selected per unit. `install.sh` already vendors driver
  code per-model, so this is "pick the right vendored module," not new
  infrastructure.
- **Deferred on purpose:** actually building the driver abstraction +
  partial-refresh/ghosting logic waits until real B/W hardware is in
  hand — can't validate ghosting behavior against a panel that doesn't
  exist yet. Everything else in this section (registry, parametrized
  renderer, validation) should land as part of the initial data-model
  work regardless, since retrofitting it later means touching the same
  render code twice.

## 4. Auth & isolation

Today's broker has one shared bearer token for everything — fine on a
trusted home LAN, not fine once units belong to other people at other
locations over the internet. Required before onboarding any external
unit:

- **Per-unit tokens.** A unit's publishers and its `pi_client` only work
  against that unit's own token. A leaked/compromised token for one
  friend's unit can't read or write anyone else's screens.
- **Separate elevated token for preview/admin use** — the only credential
  that can see across all units.

## 5. Broker persistence

Currently in-memory only: a restart just means blank widgets until the
next publisher cron cycle, an acceptable tradeoff for a single home
hobby display. Not acceptable once a unit belongs to, e.g., a store that
pushes its "weekly special" screen by hand once a week — a broker
restart could leave that screen blank for days with nobody noticing.
**Add a persistence layer (periodic JSON/SQLite snapshot, reload on
boot) before onboarding any unit with infrequent, business-relevant
updates** — this should land before step 7 in the rollout below, not
after.

## 6. Hosting: AWS

**Chosen: Amazon Lightsail, $5/mo nano instance** (Ubuntu VM blueprint —
not the separate "Lightsail Containers" product), `ca-central-1`
(Montreal — closest region, lowest latency). 512MB RAM, 2 vCPU, 20GB SSD,
1TB transfer, public IPv4, all bundled into one flat price. Deployment is
identical to today: SSH in, `git clone`, `docker compose up -d`.

**Why not the alternatives:**
- **EC2 t4g.nano** looks cheaper on paper (~$3/mo compute) but isn't
  once billed realistically: AWS has charged for every public IPv4
  address since Feb 2024 (~$3.65/mo) plus separate EBS storage billing —
  real total lands around $8-9/mo, worse than Lightsail's bundled $5, for
  the same specs and more line items to track.
- **ECS Fargate / App Runner** are priced for bursty/scale-to-zero
  workloads. This workload is always-on with no scaling need (WebSocket
  connections must persist, traffic is flat and tiny), so there's no
  scale-to-zero benefit to capture — just a higher effective cost than a
  tiny reserved VM.
- **Lambda + API Gateway WebSocket** would need real architectural rework
  (external state store for what's currently in-memory, a different
  connection-management model) to fit a stateless execution model — not
  worth it for this workload's size.
- **AWS ALB / Lightsail Load Balancer with ACM cert** would auto-renew
  TLS certs with zero maintenance, but at ~$18-20/mo minimum it roughly
  quadruples the entire hosting cost just to avoid running one more small
  container (Caddy, section 7) that does the same job for free.
- **EC2 Spot** (~$1-2/mo) was considered and set aside, not rejected —
  `pi_client.py` already has reconnect-with-backoff, so it would probably
  tolerate Spot interruptions fine for a home-scale project. Marginal
  savings over Lightsail's flat $5 don't currently justify the added
  operational complexity (interruption handling, no fixed IP without also
  paying for an Elastic IP). Worth revisiting if cost ever becomes the
  overriding priority.

## 7. DNS & TLS

Domain: `panels.richardturmel.net`, user's own DNS, A record pointed at
the Lightsail instance.

**Chosen: Caddy** as reverse proxy, running as one more service in
`docker-compose.yml`. Reasoning: automatic Let's Encrypt issuance *and*
renewal *and* HTTP→HTTPS redirect, all with near-zero config, plus
transparent WebSocket proxying (no manual `Upgrade`/`Connection` header
forwarding required, unlike nginx). This is what actually makes it safe
to hand a `wss://` URL + token to a unit at someone else's house.

**Routing: single hostname, path-based**, not subdomains — keeps
certificate issuance on the simple HTTP-01 challenge (just needs port 80
reachable) instead of DNS-01 (which would need API credentials for
whatever registrar holds the DNS, only worth it for a subdomain
aesthetic):

```
panels.richardturmel.net/          -> preview   (human browser UI)
panels.richardturmel.net/ws        -> broker    (WebSocket)
panels.richardturmel.net/api/*     -> broker    (REST: publisher pushes, /api/v1/units)
```

**Operational notes:**
- Caddy's cert-storage volume (`caddy_data` or similar) **must persist**
  across container recreates — Let's Encrypt rate-limits issuance per
  domain (a handful per week); losing that volume repeatedly can lock out
  fresh certs for days.
- Once Caddy is the front door, **stop publishing 9090/9091 directly to
  the host** (today's `docker-compose.yml` maps them straight through) —
  otherwise the broker/preview stay reachable unencrypted, bypassing
  Caddy entirely.
- **Migration checklist item:** every existing publisher's `BROKER_URL`
  (currently the LAN IP, `http://192.168.11.75:9090`) needs to become
  `https://panels.richardturmel.net` once the broker actually moves — a
  one-line env var change, but five separate cron jobs to touch (rooms,
  ups, zabbix, weather, pi_temp).

## Rollout order

1. Broker: units/screens data model + API + panel-profile registry +
   `render.py`/`palette.py` parametrization. Default to `unit=home,
   screen=main` so nothing breaks mid-transition.
2. `pi_client`: add `unit_id` to config, add duration-based rotation list
   (a single-screen list == today's exact behavior, zero visible change
   until a second screen is actually configured).
3. Broker state persistence (snapshot + reload on boot).
4. Per-unit tokens + Caddy/Let's Encrypt TLS.
5. Preview: `GET /api/v1/units` + debug dropdown, elevated token.
6. First real multi-screen test, entirely at home (add the
   docker/serval-status screen) — validates rotation end-to-end in a
   trusted environment before anything external touches it.
7. Onboard Lilia's store as the first external unit — new hardware, its
   own token, its own screens. Validates isolation for real.
8. Friends' units after that, same pattern.
9. (Opportunistic, only once real B/W hardware is in hand) driver
   abstraction + partial refresh + ghosting logic in `client.py`.

## Explicitly deferred / open questions

- **Different panel resolutions per unit:** architecture (panel profiles)
  is designed for this now; the actual second hardware profile isn't
  needed until real hardware exists to test against.
- **Numeric unit IDs:** considered, rejected as unnecessary complexity at
  current scale (a handful of units). Revisit only if there's a real
  reason to obscure unit names.
- **Grayscale (not just black/white) panels:** the `colors` list
  abstraction already covers this for free if it comes up — no design
  work needed now.
