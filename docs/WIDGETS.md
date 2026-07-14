# Widget reference

Each entry in `layout.yaml`'s `widgets:` list has a `type`, a grid
position (`x`, `y`, `w`, `h`), and an `id`. The `id` is the key a publisher
pushes data under (`POST /api/v1/widgets/<id>`). This page documents what
JSON shape each `type` expects in that push.

## Colors: solid vs. dithered blends

Most `color` fields below accept one of the panel's 4 real colors --
`"black"`, `"white"`, `"red"`, `"yellow"` -- drawn as a flat fill.

They also accept a **dithered blend**: `"orange"`, `"pink"`, `"gray"` (or
`"grey"`), `"light_gray"`, `"dark_gray"`. These aren't real panel colors --
there's no such thing as an orange pixel on 4-color e-paper -- they're a
Bayer-dispersed dot pattern of two real colors (orange = red+yellow, pink
= red+white, gray = black+white) at a pitch fine enough to read as a
blended color from normal viewing distance, the same trick halftone
printing uses. The dot pattern is deliberately diagonal/dispersed rather
than straight horizontal or vertical lines -- axis-aligned line patterns
are the textbook worst case for moire when an image gets resampled at a
non-integer scale (a browser shrinking a screenshot, a phone viewing a
photo of the panel, ...), which is exactly why real halftone printing uses
angled dot screens instead of parallel lines. The dot pattern's tile size
also auto-shrinks for short fills (a slim progress bar) so it still
completes a couple of full cycles instead of getting cut off mid-pattern
and collapsing into a solid band of one raw color.

Two things to know:

- **Fills only, not text.** Blends only make sense on shapes at least a
  few pixels wide/tall (bars, pie slices, banners, progress fill) --
  `bar_chart`, `pie_chart`, `alert_banner`, and `progress` all accept a
  blend anywhere they accept a `color`. Text and icons always use one of
  the 4 solid colors.
- **Up close it looks like a dot pattern; from a few feet away it reads
  as a color.** How convincing it is depends on viewing distance and
  physical size, same as any halftone -- at very small preview sizes
  (e.g. a tiny thumbnail) it can skew toward whichever of the two colors
  is more dominant. If you don't like the effect, just don't use blend
  names — nothing forces you to.

The `image` widget handles this differently: for photos it uses
Floyd-Steinberg error-diffusion dithering by default (more natural for
continuous-tone images than the ordered blends above), which you can
disable with `{"dither": false}` for a flat graphic where crisp edges
matter more.

## `header`

Full-width title bar, drawn once per row at the top of the layout.

```json
{"title": "Home Dashboard", "subtitle": "optional smaller line", "time": "3:45 PM"}
```

`time` is optional — both `pi_client` and `preview` fill it in
automatically with the current local time if you don't provide one.

## `metric`

One big number — a temperature, a percentage, a sensor reading.

```json
{"label": "Indoor", "value": 71, "unit": "°F", "color": "black"}
```

`color` is optional (defaults to black) — use `"red"` to flag something
out of range.

## `text_list`

Rows of label/value pairs — door/lock status, misc sensors, etc.

```json
{
  "items": [
    {"label": "Front Door", "value": "Locked"},
    {"label": "Garage", "value": "Open", "color": "red"}
  ]
}
```

## `weather`

Condition icon + current temperature, drawn from scratch (no external
icon files needed).

```json
{
  "condition": "rain",
  "temp": 58,
  "temp_unit": "°F",
  "high": 61,
  "low": 49,
  "humidity": 82
}
```

`condition` accepts: `sunny`, `cloudy`, `rain`, `snow`, `storm`, `fog`,
`clear_night` — plus common Home Assistant weather-entity state strings
(`clear`, `partlycloudy`, `pouring`, `snowy-rainy`, `lightning-rainy`,
etc.), which get mapped automatically. `high`/`low`/`humidity` are all
optional.

## `calendar`

A short agenda list.

```json
{
  "events": [
    {"time": "9:00 AM", "title": "Team standup"},
    {"time": "5:00 PM", "title": "Pick up kids"}
  ]
}
```

Note: nothing in this project pulls real events out of an HA `calendar.*`
entity automatically (that needs an extra service call) — see the comment
in `publisher_ha/entities.example.yaml` for where to extend it if you want
that.

## `alert_banner`

A full-width highlighted strip that's completely invisible when inactive
— use it for "garage left open," "freezer temp high," etc.

```json
{"active": true, "level": "warning", "text": "Garage door open 20+ min"}
```

`level` is `"warning"` (yellow) or `"critical"` (red) — or set `"color"`
explicitly (solid or a dithered blend, e.g. `"orange"`) to override the
level-based default entirely. When `active` is `false` (or the widget has
no data at all), nothing is drawn — the space stays blank/white.

## `progress`

A horizontal bar — battery levels, print job %, etc.

```json
{"label": "Vacuum Battery", "value": 62}
```

`value` is 0–100. Turns red automatically under 20 — or set `"color"` to
force a specific solid or dithered-blend fill.

## `image`

Pastes a logo, photo, QR code, or a chart rendered elsewhere — any format
Pillow can read (PNG, JPG/JPEG, GIF, BMP, ...), auto-detected from the
bytes. Transparent PNGs are flattened onto white before quantizing, so a
logo with a transparent background renders correctly instead of showing a
black box.

Three ways to supply the picture, checked in this order:

```json
{"asset": "logo.png"}
```
A file you've dropped once into `shared/dashboard_render/assets/` (see
the README there). Best for a logo that never changes — it's read from
disk at render time, never sent over the network.

```json
{"image_base64": "iVBORw0KGgoAAAANSUhEUgAA..."}
```
Base64-encoded image bytes pushed by a publisher — use this for anything
that changes (a generated chart, a snapshot, a QR code with live data).
`png_base64` / `jpg_base64` also work as aliases of the same field.

A publisher can build this field with, e.g.:

```python
import base64
with open("chart.png", "rb") as f:
    payload = {"image_base64": base64.b64encode(f.read()).decode("ascii")}
```

Keep pushed images reasonably small (roughly widget-size) — the broker
holds state in memory/JSON and broadcasts the full state on every change,
so a large image pushed frequently will slow things down. A one-off logo
belongs in `assets/`, not pushed as base64 on every update.

## `bar_chart`

A vertical bar chart. Values auto-scale to the largest bar unless you set
`max`.

```json
{
  "title": "Power Use (kWh)",
  "unit": "",
  "bars": [
    {"label": "Mon", "value": 12},
    {"label": "Tue", "value": 18},
    {"label": "Wed", "value": 9},
    {"label": "Thu", "value": 21, "color": "red"}
  ]
}
```

Bars cycle through black/red/yellow automatically; set `color` on a bar
to override it with any solid color or dithered blend (`"orange"`,
`"pink"`, `"gray"`, ...).

## `pie_chart`

A pie chart with a legend (label + percentage).

```json
{
  "title": "Energy Split",
  "segments": [
    {"label": "HVAC", "value": 45},
    {"label": "Kitchen", "value": 25},
    {"label": "Other", "value": 30}
  ]
}
```

Segments auto-cycle through colors: the first 3 get solid black/red/
yellow, the next 3 get dithered blends (orange/gray/pink), covering up to
6 visually distinct segments before repeating. Set `color` per segment
(any name from the colors section above) to control this directly — e.g.
group small categories into a gray "Other" slice.

## `table`

A generic multi-column grid -- a header row (optional) plus N data rows,
each with the same number of cells. It has no idea what the columns mean
-- format each cell as a plain string before pushing it (`"22.1°C"`,
`"87%"`) -- same spirit as `text_list` not knowing what its label/value
pairs mean, just extended to more than 2 columns.

```json
{
  "columns": ["Room", "Temp", "Humidity", "Batt"],
  "rows": [
    ["Etage", "22.1°C", "45%", "87%"],
    {"cells": ["Gazebo", "15.0°C", "70%", "12%"], "color": "red"}
  ]
}
```

The first column is left-aligned (meant for a name); every other column
is right-aligned (meant for numbers). A row can be a plain list of cell
strings, or `{"cells": [...], "color": "..."}` to tint that whole row
(solid or dithered blend) -- handy for flagging one row out of several,
e.g. a low battery.

`col_widths` (optional, e.g. `[2, 1, 1, 1]`) sets relative column
widths; omit it for equal-width columns.

## `panel` (decorative grouping box)

A border (+ optional label) drawn around a cluster of other widgets, to
visually group related ones -- e.g. a box around a UPS battery bar and a
load metric sitting next to each other. Give it the *union* rectangle of
the widgets it's grouping:

```yaml
  - id: ups_panel
    type: panel
    x: 6
    y: 3
    w: 6
    h: 2
    title: "UPS"        # optional -- drawn as a fieldset-style label cut into the top border
    color: "black"       # optional, default black -- solid colors only, no blends
    width: 2              # optional, default 2 -- border thickness in px
```

This is the one widget type with **no publisher involved at all** — it
only reads its own `layout.yaml` entry (`title`/`color`/`width`), never
pushed `data`, so there's nothing to `POST` for it. List it anywhere
relative to the widgets it's grouping (order doesn't matter here — it
only draws right at the edge of its own box, while every other widget
type stays inset from its own cell edges, so there's no overlap either
way).

## Adding a new widget type

Add a `draw_<type>` function to `shared/dashboard_render/widgets.py`
following the existing ones (signature `(ctx: Ctx) -> None`, `ctx.box` is
the pixel rectangle, `ctx.data` is the JSON payload, `ctx.style` is
whatever other keys you put on that widget in `layout.yaml`), then add it
to `WIDGET_REGISTRY` at the bottom of that file. Nothing else needs to
change — the broker and Pi client don't care what widget types exist.
