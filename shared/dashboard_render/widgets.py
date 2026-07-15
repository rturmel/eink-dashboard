"""
Widget drawing functions.

Every widget function has the signature:

    draw_<type>(draw: ImageDraw.ImageDraw, box: Box, data: dict, style: dict) -> None

`box` is the pixel rectangle (x, y, w, h) the widget owns, already computed
by render.py from the layout grid. `data` is whatever JSON payload the
broker is holding for that widget id (pushed by a publisher). `style` is
the widget's entry from layout.yaml (title, options, etc.) minus
position/size.

Widgets are intentionally forgiving about missing/malformed data (a
publisher being down shouldn't crash the renderer) -- they render a
placeholder instead of raising.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from PIL import ImageDraw

from . import palette
from .fonts import get_font

Box = tuple[int, int, int, int]  # x, y, w, h

PADDING = 14

# Static images (logos, etc.) that ship with the code rather than getting
# pushed through the broker on every update -- drop a file here and
# reference it from a widget as {"asset": "logo.png"}. See draw_image().
ASSETS_DIR = Path(__file__).parent / "assets"


@dataclass
class Ctx:
    draw: ImageDraw.ImageDraw
    box: Box
    data: dict[str, Any]
    style: dict[str, Any]


def _inset(box: Box, pad: int = PADDING) -> Box:
    x, y, w, h = box
    return (x + pad, y + pad, max(w - 2 * pad, 0), max(h - 2 * pad, 0))


def _fill_rect(ctx: Ctx, ltrb: tuple[float, float, float, float], color_name: str) -> None:
    """Fill a rectangle with a solid palette color, or -- if color_name is
    one of palette.BLENDS ("orange", "pink", "gray", ...) -- an ordered-
    dither pattern approximating it. Use this instead of
    draw.rectangle(fill=...) anywhere a widget's fill color is user-
    configurable, so blends "just work" without every widget needing its
    own dithering logic.

    `ltrb` uses the same convention as PIL's draw.rectangle([l,t,r,b]):
    `r`/`b` are the last INCLUDED pixel column/row, not an exclusive
    one-past-the-end bound. dithered_fill() takes a plain (x,y,w,h) size,
    so the +1 below converts inclusive-edge to pixel count -- dropping it
    was a real bug: the dithered path came up exactly 1px short at the
    bottom/right versus the solid-fill path, invisible on a flat color
    fill but a visible stray white line once the fill became a pattern."""
    l, t, r, b = ltrb
    if palette.is_blend(color_name):
        palette.dithered_fill(ctx.draw._image, (l, t, r - l + 1, b - t + 1), color_name)  # type: ignore[attr-defined]
    else:
        ctx.draw.rectangle([l, t, r, b], fill=palette.color(color_name))


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _fit_font(
    draw: ImageDraw.ImageDraw, text: str, max_w: int, max_h: int, bold: bool = True
) -> Any:
    """Binary-search the largest font size that fits text in the box."""
    lo, hi = 8, max(max_h, 8)
    best = get_font(lo, bold)
    while lo <= hi:
        mid = (lo + hi) // 2
        font = get_font(mid, bold)
        w, h = _text_size(draw, text, font)
        if w <= max_w and h <= max_h:
            best = font
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        w, _ = _text_size(draw, trial, font)
        if w <= max_w or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _placeholder(ctx: Ctx, message: str = "No data") -> None:
    x, y, w, h = _inset(ctx.box)
    font = get_font(16, bold=False)
    ctx.draw.text(
        (x + w / 2, y + h / 2),
        message,
        font=font,
        fill=palette.color("black"),
        anchor="mm",
    )


def _title_bar(ctx: Ctx, title: str) -> int:
    """Draws a small caps-style title at the top-left of the widget box.
    Returns the y-coordinate where content should start below it."""
    x, y, w, h = _inset(ctx.box)
    font = get_font(14, bold=True)
    ctx.draw.text((x, y), title.upper(), font=font, fill=palette.color("black"))
    return y + 22


# ---------------------------------------------------------------------------
# header: dashboard title bar + optional live clock
# ---------------------------------------------------------------------------
def draw_header(ctx: Ctx) -> None:
    x, y, w, h = ctx.box
    ix, iy, iw, ih = _inset(ctx.box, 10)

    title = ctx.data.get("title") or ctx.style.get("title") or ""
    subtitle = ctx.data.get("subtitle", "")
    time_str = ctx.data.get("time", "")

    title_font = get_font(30, bold=True)
    ctx.draw.text((ix, iy), title, font=title_font, fill=palette.color("black"))

    if subtitle:
        sub_font = get_font(15, bold=False)
        tw, th = _text_size(ctx.draw, title, title_font)
        ctx.draw.text(
            (ix, iy + th + 4), subtitle, font=sub_font, fill=palette.color("black")
        )

    if time_str:
        time_font = get_font(26, bold=True)
        tw, th = _text_size(ctx.draw, time_str, time_font)
        ctx.draw.text(
            (ix + iw - tw, iy + 2), time_str, font=time_font, fill=palette.color("black")
        )

    # bottom rule
    ctx.draw.line([(x, y + h - 1), (x + w, y + h - 1)], fill=palette.color("black"), width=2)


# ---------------------------------------------------------------------------
# metric: one big number (temperature, humidity, a sensor reading, etc.)
#
# The label's footprint is measured first and subtracted from the box
# height *before* fitting the value font, rather than using fixed
# fractions (old behavior: value centered at a fixed 42% down, label at a
# fixed 14px from the bottom) -- those fixed offsets assumed a roomy ~h2
# box and silently overlapped value/label text in anything shorter (a 1
# grid row box), since the value font could still fit-to-size taller than
# the actual gap above the label. Reserving the label's real height first
# makes the value font's available space (and therefore its size) shrink
# to whatever's actually left, so there's no overlap at any box height --
# it just looks smaller in a smaller box instead of breaking.
# ---------------------------------------------------------------------------
def draw_metric(ctx: Ctx) -> None:
    if not ctx.data:
        _placeholder(ctx)
        return

    x, y, w, h = _inset(ctx.box)
    label = ctx.data.get("label", ctx.style.get("title", ""))
    value = str(ctx.data.get("value", "--"))
    unit = str(ctx.data.get("unit", ""))
    accent = ctx.data.get("color", "black")

    label_font = get_font(15, bold=False)
    label_h = 0.0
    if label:
        _, lh = _text_size(ctx.draw, label.upper(), label_font)
        label_h = lh + 8  # padding above the label text

    value_area_h = max(h - label_h, 16)
    value_text = f"{value}{unit}"
    value_font = _fit_font(ctx.draw, value_text, w, int(value_area_h))
    ctx.draw.text(
        (x + w / 2, y + value_area_h / 2),
        value_text,
        font=value_font,
        fill=palette.color(accent),
        anchor="mm",
    )

    if label:
        ctx.draw.text(
            (x + w / 2, y + h - label_h / 2),
            label.upper(),
            font=label_font,
            fill=palette.color("black"),
            anchor="mm",
        )


# ---------------------------------------------------------------------------
# text_list: rows of label/value pairs (sensors, statuses, etc.)
# ---------------------------------------------------------------------------
def draw_text_list(ctx: Ctx) -> None:
    title = ctx.style.get("title", "")
    y0 = _title_bar(ctx, title) if title else _inset(ctx.box)[1]
    x, _, w, _ = _inset(ctx.box)
    _, top, _, h = ctx.box
    bottom = top + h - PADDING

    items = (ctx.data or {}).get("items", [])
    if not items:
        _placeholder(ctx)
        return

    row_h = max((bottom - y0) / max(len(items), 1), 20)
    label_font = get_font(16, bold=False)

    for i, item in enumerate(items):
        row_y = y0 + i * row_h
        if row_y > bottom:
            break
        label = str(item.get("label", ""))
        value = str(item.get("value", ""))
        color = palette.color(item.get("color", "black"))

        ctx.draw.text((x, row_y), label, font=label_font, fill=palette.color("black"))

        # Value font shrinks to whatever width is actually left after the
        # label, rather than a fixed 16px -- a long label + long value
        # (e.g. "Air Quality" + "70 (Moderate)") in a narrow widget would
        # otherwise overlap instead of just getting tighter.
        label_w, _ = _text_size(ctx.draw, label, label_font)
        max_value_w = max(w - label_w - 8, 20)
        value_font = _fit_font(ctx.draw, value, max_value_w, 20, bold=True)
        vw, _ = _text_size(ctx.draw, value, value_font)
        ctx.draw.text((x + w - vw, row_y), value, font=value_font, fill=color)


# ---------------------------------------------------------------------------
# table: a generic multi-column grid -- rows of N cells each, with an
# optional header row. Doesn't know or care what the columns mean (same
# spirit as text_list not knowing what its label/value pairs mean) --
# format each cell as a plain string before pushing it (e.g. "22.1°C",
# "87%"). First column is left-aligned (meant for a name), every other
# column is right-aligned (meant for numbers), matching text_list's
# label/value convention just extended to more columns.
# ---------------------------------------------------------------------------
def _table_row_cells(row: Any) -> list:
    return row.get("cells", []) if isinstance(row, dict) else list(row)


def _table_row_color(row: Any) -> str:
    return row.get("color", "black") if isinstance(row, dict) else "black"


def _draw_table_cell(
    ctx: Ctx, text: str, x0: float, x1: float, y: float, font, color, left: bool
) -> None:
    if not text:
        return
    if left:
        ctx.draw.text((x0, y), text, font=font, fill=color)
    else:
        tw, _ = _text_size(ctx.draw, text, font)
        ctx.draw.text((x1 - tw - 6, y), text, font=font, fill=color)


def draw_table(ctx: Ctx) -> None:
    """
    {
      "title": "Room Sensors",                            # optional
      "columns": ["Room", "Temp", "Humidity", "Batt"],    # optional header row
      "col_widths": [2, 1, 1, 1],                          # optional relative widths, default equal
      "rows": [
        ["Etage", "22.1°C", "45%", "87%"],
        {"cells": ["Gazebo", "15.0°C", "70%", "12%"], "color": "red"}  # optional per-row color override
      ]
    }
    """
    data = ctx.data or {}
    raw_rows = data.get("rows", [])
    if not raw_rows:
        _placeholder(ctx, "No data")
        return

    columns = data.get("columns") or []
    title = data.get("title") or ctx.style.get("title", "")
    y0 = _title_bar(ctx, title) if title else _inset(ctx.box)[1]
    x, _, w, _ = _inset(ctx.box)
    _, top, _, h = ctx.box
    bottom = top + h - PADDING

    n_cols = max(len(columns), max((len(_table_row_cells(r)) for r in raw_rows), default=1))
    weights = list(data.get("col_widths") or [1] * n_cols)
    weights = (weights + [1] * n_cols)[:n_cols]
    total_weight = sum(weights) or 1
    edges = [x]
    acc = 0.0
    for wgt in weights:
        acc += wgt
        edges.append(x + w * (acc / total_weight))

    header_font = get_font(13, bold=True)
    row_font = get_font(18, bold=False)
    row_y = y0

    if columns:
        for i in range(n_cols):
            label = str(columns[i]).upper() if i < len(columns) else ""
            _draw_table_cell(
                ctx, label, edges[i], edges[i + 1], row_y, header_font,
                palette.color("black"), left=(i == 0),
            )
        row_y += 20
        ctx.draw.line(
            [(x, row_y - 5), (x + w, row_y - 5)], fill=palette.color("black"), width=1
        )

    n_rows = len(raw_rows)
    row_h = max((bottom - row_y) / max(n_rows, 1), 20)

    for r, row in enumerate(raw_rows):
        ry = row_y + r * row_h
        if ry > bottom:
            break
        cells = _table_row_cells(row)
        color = palette.color(_table_row_color(row))
        for i in range(n_cols):
            text = str(cells[i]) if i < len(cells) else ""
            _draw_table_cell(
                ctx, text, edges[i], edges[i + 1], ry, row_font, color, left=(i == 0)
            )


# ---------------------------------------------------------------------------
# weather: condition icon + current temperature + optional hi/lo
# ---------------------------------------------------------------------------
_CONDITION_ALIASES = {
    # canonical names map to themselves so passing them directly always
    # works, not just their Home-Assistant-style aliases below
    "sunny": "sunny",
    "cloudy": "cloudy",
    "rain": "rain",
    "snow": "snow",
    "storm": "storm",
    "fog": "fog",
    "clear_night": "clear_night",
    # Home Assistant weather-entity state strings
    "clear": "sunny",
    "clear-night": "clear_night",
    "partlycloudy": "cloudy",
    "rainy": "rain",
    "pouring": "rain",
    "snowy": "snow",
    "snowy-rainy": "snow",
    "lightning": "storm",
    "lightning-rainy": "storm",
    "windy": "cloudy",
}


def _paste_silhouette(
    draw: ImageDraw.ImageDraw,
    add_ellipses: list[list[float]],
    subtract_ellipses: Optional[list[list[float]]] = None,
    fill: str = "white",
    outline: str = "black",
    outline_w: int = 2,
) -> None:
    """Paste the union of `add_ellipses` (minus any `subtract_ellipses`,
    for carving a crescent moon out of a circle) as one smooth silhouette:
    filled with `fill`, outlined in `outline`.

    Weather icons like the cloud are built from several overlapping
    ellipses -- outlining each ellipse individually would draw every seam
    where they overlap (three overlapping circles, not one cloud). Instead
    this rasterizes the union into a mask, eroding a copy of it to find
    just the *outer* boundary (mask minus its erosion = the outline ring),
    so overlapping component shapes always read as one clean silhouette
    regardless of how they overlap.
    """
    from PIL import Image as _Image
    from PIL import ImageChops as _ImageChops
    from PIL import ImageDraw as _ImageDraw
    from PIL import ImageFilter as _ImageFilter

    all_boxes = add_ellipses + (subtract_ellipses or [])
    xs = [c for box in all_boxes for c in (box[0], box[2])]
    ys = [c for box in all_boxes for c in (box[1], box[3])]
    pad = outline_w + 2
    x0, y0 = int(min(xs)) - pad, int(min(ys)) - pad
    x1, y1 = int(max(xs)) + pad, int(max(ys)) + pad
    w, h = max(x1 - x0, 1), max(y1 - y0, 1)

    mask = _Image.new("L", (w, h), 0)
    mdraw = _ImageDraw.Draw(mask)
    for box in add_ellipses:
        mdraw.ellipse([box[0] - x0, box[1] - y0, box[2] - x0, box[3] - y0], fill=255)
    for box in subtract_ellipses or []:
        mdraw.ellipse([box[0] - x0, box[1] - y0, box[2] - x0, box[3] - y0], fill=0)

    eroded = mask.filter(_ImageFilter.MinFilter(2 * outline_w + 1))
    ring = _ImageChops.subtract(mask, eroded)

    image = draw._image  # type: ignore[attr-defined]
    fill_layer = _Image.new("RGB", (w, h), palette.color(fill))
    image.paste(fill_layer, (x0, y0), mask)
    outline_layer = _Image.new("RGB", (w, h), palette.color(outline))
    image.paste(outline_layer, (x0, y0), ring)


def _icon_sunny(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int) -> None:
    # The rays extend past the disc on every side, so a sun drawn at the
    # same `r` as the other icons (cloud, etc.) ends up with noticeably
    # more total footprint than they have -- shrink the disc itself and
    # keep the rays short/close to it so the whole icon's visual size
    # roughly matches the others instead of dominating the widget.
    disc_r = int(r * 0.7)
    for i in range(8):
        a = i * math.pi / 4
        x1, y1 = cx + math.cos(a) * (disc_r + 3), cy + math.sin(a) * (disc_r + 3)
        x2, y2 = cx + math.cos(a) * (disc_r + 9), cy + math.sin(a) * (disc_r + 9)
        draw.line([(x1, y1), (x2, y2)], fill=palette.color("yellow"), width=3)
    # Outline drawn after the rays so the sun's disc reads as crisp/solid
    # rather than the rays visually poking through a soft edge.
    _paste_silhouette(
        draw,
        [[cx - disc_r, cy - disc_r, cx + disc_r, cy + disc_r]],
        fill="yellow",
        outline="black",
        outline_w=2,
    )


def _icon_cloud(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, color="black") -> None:
    """White-filled, black-outlined puffy cloud -- outline instead of a
    solid fill so it reads as a cloud pictogram rather than an ink blot,
    especially at the small sizes forecast_strip uses. `color` is kept as
    a parameter for backwards compatibility but now controls the outline,
    not a solid fill."""
    ellipses = [
        [cx - r, cy - r * 0.6, cx + r * 0.5, cy + r * 0.6],
        [cx - r * 0.5, cy - r, cx + r * 0.9, cy + r * 0.4],
        [cx - r * 1.3, cy - r * 0.3, cx + r * 0.2, cy + r * 0.7],
    ]
    outline_w = max(int(r * 0.14), 1)
    _paste_silhouette(draw, ellipses, fill="white", outline=color, outline_w=outline_w)


def _icon_rain(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int) -> None:
    _icon_cloud(draw, cx, cy - 8, r, color="black")
    for dx in (-r * 0.6, 0, r * 0.6):
        x = cx + dx
        draw.line([(x, cy + r * 0.5), (x - 6, cy + r * 0.9)], fill=palette.color("black"), width=4)


def _icon_snow(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int) -> None:
    _icon_cloud(draw, cx, cy - 8, r, color="black")
    for dx in (-r * 0.6, 0, r * 0.6):
        x, y = cx + dx, cy + r * 0.7
        draw.line([(x - 5, y), (x + 5, y)], fill=palette.color("black"), width=3)
        draw.line([(x, y - 5), (x, y + 5)], fill=palette.color("black"), width=3)


def _icon_storm(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int) -> None:
    _icon_cloud(draw, cx, cy - 8, r, color="black")
    pts = [(cx + 4, cy + r * 0.3), (cx - 8, cy + r * 0.9), (cx + 2, cy + r * 0.9), (cx - 6, cy + r * 1.4)]
    draw.line(pts, fill=palette.color("yellow"), width=5, joint="curve")


def _icon_fog(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int) -> None:
    for i, dy in enumerate((-10, 4, 18)):
        width = r * (1.4 - i * 0.15)
        draw.line(
            [(cx - width, cy + dy), (cx + width, cy + dy)],
            fill=palette.color("black"),
            width=4,
        )


def _icon_clear_night(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int) -> None:
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=palette.color("black"))
    draw.ellipse(
        [cx - r + r * 0.6, cy - r, cx + r + r * 0.6, cy + r], fill=palette.color("white")
    )


_ICON_FN: dict[str, Callable] = {
    "sunny": _icon_sunny,
    "cloudy": _icon_cloud,
    "rain": _icon_rain,
    "snow": _icon_snow,
    "storm": _icon_storm,
    "fog": _icon_fog,
    "clear_night": _icon_clear_night,
}


def draw_weather(ctx: Ctx) -> None:
    if not ctx.data:
        _placeholder(ctx)
        return

    x, y, w, h = _inset(ctx.box)

    # Optional -- a city/location name drawn as a small line above the icon,
    # pushed as data (not a layout.yaml style option) so it can change at
    # runtime without touching the layout (e.g. a configurable-city
    # publisher). Everything below shifts down to make room for it.
    location = str(ctx.data.get("location", ""))
    top_offset = 0
    if location:
        loc_font = get_font(13, bold=True)
        ctx.draw.text((x, y), location, font=loc_font, fill=palette.color("black"))
        top_offset = 18

    condition = _CONDITION_ALIASES.get(
        str(ctx.data.get("condition", "")).lower(), "cloudy"
    )
    icon_fn = _ICON_FN.get(condition, _icon_cloud)

    icon_r = int(min(w, h - top_offset) * 0.22)
    icon_cx = x + icon_r + 10
    icon_cy = y + top_offset + icon_r + 10
    icon_fn(ctx.draw, icon_cx, icon_cy, icon_r)

    temp = str(ctx.data.get("temp", "--"))
    unit = str(ctx.data.get("temp_unit", ""))
    temp_font = _fit_font(ctx.draw, f"{temp}{unit}", int(w * 0.55), int((h - top_offset) * 0.5))
    ctx.draw.text(
        (x + w - 4, y + top_offset + 6),
        f"{temp}{unit}",
        font=temp_font,
        fill=palette.color("black"),
        anchor="ra",
    )

    # Two separate lines (rather than one joined string) so a narrow box
    # (e.g. a 2-grid-column panel) shrinks the font to fit instead of the
    # text running off the left edge -- each line gets its own _fit_font
    # pass against the actual box width.
    detail_lines = []
    if ctx.data.get("high") is not None or ctx.data.get("low") is not None:
        hi = ctx.data.get("high", "--")
        lo = ctx.data.get("low", "--")
        detail_lines.append(f"H:{hi}{unit} L:{lo}{unit}")
    if ctx.data.get("humidity") is not None:
        detail_lines.append(f"{ctx.data['humidity']}% humidity")

    line_y = y + h - 18 * len(detail_lines)
    for line in detail_lines:
        line_font = _fit_font(ctx.draw, line, w, 16, bold=False)
        ctx.draw.text(
            (x + w - 4, line_y), line, font=line_font, fill=palette.color("black"), anchor="ra"
        )
        line_y += 18
    ctx.draw.text(
        (icon_cx, icon_cy + icon_r + 14),
        condition.replace("_", " ").title(),
        font=get_font(14, bold=False),
        fill=palette.color("black"),
        anchor="ma",
    )


# ---------------------------------------------------------------------------
# forecast_strip: a row of compact icon + hi/lo mini-forecasts (tomorrow,
# the day after, ...) -- the short-range companion to `weather` above,
# meant to sit in a single short row below it rather than needing its own
# full-size widget per day.
# ---------------------------------------------------------------------------
def draw_forecast_strip(ctx: Ctx) -> None:
    data = ctx.data or {}
    days = data.get("days", [])
    if not days:
        _placeholder(ctx, "No forecast")
        return

    x, y, w, h = _inset(ctx.box)
    unit = str(data.get("temp_unit", ""))
    n = len(days)
    col_w = w / n
    icon_r = max(int(min(col_w, h) * 0.16), 7)
    label_font = get_font(12, bold=True)
    temp_font = get_font(12, bold=False)

    for i, day in enumerate(days):
        cx = x + col_w * i + col_w / 2
        condition = _CONDITION_ALIASES.get(str(day.get("condition", "")).lower(), "cloudy")
        icon_fn = _ICON_FN.get(condition, _icon_cloud)
        icon_cy = y + icon_r + 2
        icon_fn(ctx.draw, cx, icon_cy, icon_r)

        label = str(day.get("label", ""))
        label_y = icon_cy + icon_r + 6
        ctx.draw.text((cx, label_y), label, font=label_font, fill=palette.color("black"), anchor="ma")

        hi, lo = day.get("high"), day.get("low")
        temp_text = f"{hi}{unit}/{lo}{unit}" if hi is not None and lo is not None else "--"
        ctx.draw.text(
            (cx, label_y + 15), temp_text, font=temp_font, fill=palette.color("black"), anchor="ma"
        )


# ---------------------------------------------------------------------------
# calendar / agenda: upcoming events
# ---------------------------------------------------------------------------
def draw_calendar(ctx: Ctx) -> None:
    title = ctx.style.get("title", "Agenda")
    y0 = _title_bar(ctx, title)
    x, _, w, _ = _inset(ctx.box)
    _, top, _, h = ctx.box
    bottom = top + h - PADDING

    events = (ctx.data or {}).get("events", [])
    if not events:
        _placeholder(ctx, "No upcoming events")
        return

    row_h = max((bottom - y0) / max(len(events), 1), 24)
    time_font = get_font(15, bold=True)
    title_font = get_font(15, bold=False)
    time_col_w = 100

    for i, event in enumerate(events):
        row_y = y0 + i * row_h
        if row_y > bottom:
            break
        ctx.draw.text(
            (x, row_y), str(event.get("time", "")), font=time_font, fill=palette.color("red")
        )
        lines = _wrap_text(
            ctx.draw, str(event.get("title", "")), title_font, w - time_col_w
        )
        for j, line in enumerate(lines[:1]):
            ctx.draw.text(
                (x + time_col_w, row_y), line, font=title_font, fill=palette.color("black")
            )


# ---------------------------------------------------------------------------
# alert_banner: full-width highlighted message, hidden when inactive
# ---------------------------------------------------------------------------
def draw_alert_banner(ctx: Ctx) -> None:
    data = ctx.data or {}
    if not data.get("active"):
        return  # widget simply disappears -- box stays blank/white

    x, y, w, h = ctx.box
    level = str(data.get("level", "warning")).lower()
    # `color` lets you override the level defaults entirely, including with
    # a dithered blend (e.g. "orange" for something between warning/critical).
    bg_name = data.get("color") or ("red" if level == "critical" else "yellow")
    fg = palette.color("white" if level == "critical" else "black")

    _fill_rect(ctx, (x, y, x + w, y + h), bg_name)
    text = str(data.get("text", ""))
    font = _fit_font(ctx.draw, text, w - 2 * PADDING, h - 2 * PADDING)
    ctx.draw.text((x + w / 2, y + h / 2), text, font=font, fill=fg, anchor="mm")


# ---------------------------------------------------------------------------
# progress: horizontal bar (e.g. vacuum battery, print job %)
#
# Bar thickness is capped at PROGRESS_MAX_BAR_H regardless of how tall the
# widget's box is -- letting it stretch to fill the box (the old behavior)
# made the fill look like a giant slab in anything taller than a couple of
# grid rows. A slim, consistently-proportioned bar reads as "a progress
# bar" at any box height; a bar that's 90px thick just reads as a filled
# rectangle. Any extra box height below the bar/value-label is left blank
# on purpose (e.g. a widget style you set roomier for a title/label above)
# rather than stretched into the bar.
# ---------------------------------------------------------------------------
PROGRESS_MAX_BAR_H = 28


def draw_progress(ctx: Ctx) -> None:
    data = ctx.data or {}
    x, y, w, h = _inset(ctx.box)
    label = data.get("label", ctx.style.get("title", ""))
    value = max(0, min(100, float(data.get("value", 0) or 0)))
    value_label = data.get("value_label", f"{int(value)}%")

    label_font = get_font(15, bold=False)
    ctx.draw.text((x, y), str(label), font=label_font, fill=palette.color("black"))

    bar_y = y + 24
    bar_h = min(max(h - 24 - 18, 10), PROGRESS_MAX_BAR_H)
    ctx.draw.rectangle(
        [x, bar_y, x + w, bar_y + bar_h], outline=palette.color("black"), width=2
    )
    fill_w = int(w * (value / 100))
    if fill_w > 2:
        color_name = data.get("color") or ("red" if value < 20 else "black")
        _fill_rect(
            ctx, (x + 2, bar_y + 2, x + max(fill_w - 2, 2), bar_y + bar_h - 2), color_name
        )

    value_font = get_font(14, bold=True)
    ctx.draw.text(
        (x + w, bar_y + bar_h + 4),
        str(value_label),
        font=value_font,
        fill=palette.color("black"),
        anchor="ra",
    )


# ---------------------------------------------------------------------------
# image: a logo or arbitrary picture (PNG/JPG/etc), quantized to the palette.
#
# Three ways to supply the picture, checked in this order:
#   1. {"asset": "logo.png"}       -- a file you dropped in
#      shared/dashboard_render/assets/ once. Best for a logo that never
#      changes: it never has to be pushed through the broker.
#   2. {"image_base64": "..."}     -- base64-encoded bytes of ANY format
#      Pillow can read (PNG, JPG/JPEG, GIF, BMP, ...). Format is
#      auto-detected from the bytes, not the field name.
#   3. {"png_base64": "..."}       -- kept as an alias of image_base64 for
#      backwards compatibility with earlier layouts.
# ---------------------------------------------------------------------------
def _load_image_from_data(data: dict[str, Any]):
    import base64
    import io

    from PIL import Image

    asset_name = data.get("asset")
    if asset_name:
        # Only allow a bare filename -- strips any directory components so
        # a widget payload can never read files outside ASSETS_DIR.
        safe_name = Path(asset_name).name
        path = ASSETS_DIR / safe_name
        if not path.exists():
            raise FileNotFoundError(f"asset not found: {safe_name} (looked in {ASSETS_DIR})")
        return Image.open(path)

    b64 = data.get("image_base64") or data.get("png_base64") or data.get("jpg_base64")
    if not b64:
        return None
    raw = base64.b64decode(b64)
    return Image.open(io.BytesIO(raw))


def _prepare_palette_image(img: Any, max_w: int, max_h: int, dither: bool):
    """Flatten transparency onto white, shrink to fit (max_w, max_h) keeping
    aspect ratio, and quantize to the panel's 4 colors. Shared by draw_image
    and draw_image_metric so both scale/quantize identically."""
    from PIL import Image

    # Flatten transparency onto white *before* converting to RGB -- a plain
    # .convert("RGB") on an RGBA image silently drops the alpha channel and
    # keeps whatever garbage color is underneath it, which is the classic
    # "transparent PNG logo shows up with a black box behind it" bug.
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, palette.color("white"))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")

    img = img.copy()
    img.thumbnail((max(max_w, 1), max(max_h, 1)))
    # Floyd-Steinberg dithering (the default) approximates far more detail
    # than 4 flat colors could otherwise show -- great for photos, but can
    # fuzz up crisp logo edges/text. Pass dither=False for a flat graphic
    # where sharp edges matter more than tonal range.
    return palette.quantize_to_palette(img, dither=dither)


def draw_image(ctx: Ctx) -> None:
    data = ctx.data or {}
    x, y, w, h = _inset(ctx.box)

    try:
        img = _load_image_from_data(data)
    except Exception:
        _placeholder(ctx, "Bad image data")
        return
    if img is None:
        _placeholder(ctx, "No image")
        return

    try:
        img = _prepare_palette_image(img, w, h, dither=bool(data.get("dither", True)))
    except Exception:
        _placeholder(ctx, "Bad image data")
        return

    paste_x = x + (w - img.width) // 2
    paste_y = y + (h - img.height) // 2
    ctx.draw._image.paste(img, (paste_x, paste_y))  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# image_metric: a static icon (from assets/, set once in layout.yaml) with a
# live value/unit reading drawn directly underneath it, in the same box --
# no separate image + metric widgets/rows needed, no double padding gap
# between them. Good for something like a periodic-table tile with a live
# sensor reading "attached" right under it.
#
#   layout.yaml (style -- the icon, set once, never pushed):
#     asset: "radon.jpg"
#     dither: false          # optional, default true
#
#   pushed data (from a publisher):
#     {"value": 61, "unit": " Bq/m³", "color": "black"}
# ---------------------------------------------------------------------------
def draw_image_metric(ctx: Ctx) -> None:
    x, y, w, h = _inset(ctx.box)
    data = ctx.data or {}

    icon_h = int(h * 0.68)
    icon_bottom = y  # falls back to the top of the box if there's no icon
    asset_name = ctx.style.get("asset")
    if asset_name:
        try:
            img = _load_image_from_data({"asset": asset_name})
            img = _prepare_palette_image(img, w, icon_h, dither=bool(ctx.style.get("dither", True)))
        except Exception:
            img = None
        if img is not None:
            paste_x = x + (w - img.width) // 2
            ctx.draw._image.paste(img, (paste_x, y))  # type: ignore[attr-defined]
            icon_bottom = y + img.height

    value = str(data.get("value", "--"))
    unit = str(data.get("unit", ""))
    color = data.get("color", "black")
    value_text = f"{value}{unit}"

    avail_top = icon_bottom
    avail_h = max((y + h) - avail_top, 16)
    font = _fit_font(ctx.draw, value_text, w, avail_h)
    ctx.draw.text(
        (x + w / 2, avail_top + avail_h / 2),
        value_text,
        font=font,
        fill=palette.color(color),
        anchor="mm",
    )


# ---------------------------------------------------------------------------
# bar_chart: simple vertical bar chart
# ---------------------------------------------------------------------------
_CHART_COLOR_CYCLE = ["black", "red", "yellow"]


def draw_bar_chart(ctx: Ctx) -> None:
    data = ctx.data or {}
    bars = data.get("bars", [])
    if not bars:
        _placeholder(ctx, "No data")
        return

    title = data.get("title") or ctx.style.get("title", "")
    y0 = _title_bar(ctx, title) if title else _inset(ctx.box)[1]
    x, _, w, _ = _inset(ctx.box)
    _, top, _, h = ctx.box
    bottom = top + h - PADDING

    unit = str(data.get("unit", ""))
    values = [float(b.get("value", 0) or 0) for b in bars]
    max_value = float(data.get("max") or max(values, default=1) or 1)

    label_font = get_font(13, bold=False)
    value_font = get_font(13, bold=True)
    value_h = 18  # headroom above bars for value labels
    axis_h = 16  # space below baseline for category labels

    plot_top = y0 + value_h
    plot_bottom = bottom - axis_h
    plot_h = max(plot_bottom - plot_top, 10)

    n = len(bars)
    gap = 8
    bar_w = max((w - gap * (n - 1)) / n, 4)

    ctx.draw.line(
        [(x, plot_bottom), (x + w, plot_bottom)], fill=palette.color("black"), width=2
    )

    for i, bar in enumerate(bars):
        value = values[i]
        frac = 0 if max_value <= 0 else max(0.0, min(1.0, value / max_value))
        bar_h = plot_h * frac
        bx = x + i * (bar_w + gap)
        by = plot_bottom - bar_h
        color_name = bar.get("color") or _CHART_COLOR_CYCLE[i % len(_CHART_COLOR_CYCLE)]
        if bar_h > 1:
            _fill_rect(ctx, (bx, by, bx + bar_w, plot_bottom), color_name)

        value_text = f"{value:g}{unit}"
        ctx.draw.text(
            (bx + bar_w / 2, by - 2), value_text, font=value_font, fill=palette.color("black"), anchor="mb"
        )
        label = str(bar.get("label", ""))
        ctx.draw.text(
            (bx + bar_w / 2, plot_bottom + 3), label, font=label_font, fill=palette.color("black"), anchor="ma"
        )


# ---------------------------------------------------------------------------
# pie_chart: pie/donut-style breakdown with a legend
#
# Hardware note: the panel only has 3 solid non-white colors (black/red/
# yellow) -- background white can't itself be a filled slice color. Beyond
# the first 3 segments, this cycles through dithered blends ("orange",
# "gray", "pink") so a 4th-6th segment still gets a genuinely distinct
# fill, not just an outline. Set an explicit `color` per segment (any name
# from palette.PALETTE or palette.BLENDS) to control this directly.
# ---------------------------------------------------------------------------
def _fill_pieslice(ctx: Ctx, bbox: list[float], start_angle: float, sweep: float, color_name: str) -> None:
    if not palette.is_blend(color_name):
        ctx.draw.pieslice(bbox, start_angle, start_angle + sweep, fill=palette.color(color_name))
        return

    from PIL import Image, ImageDraw as _ImageDraw

    x0, y0, x1, y1 = bbox
    w, h = int(round(x1 - x0)), int(round(y1 - y0))
    if w <= 0 or h <= 0:
        return
    mask = Image.new("L", (w, h), 0)
    _ImageDraw.Draw(mask).pieslice([0, 0, w, h], start_angle, start_angle + sweep, fill=255)
    pattern = Image.new("RGB", (w, h))
    palette.dithered_fill(pattern, (0, 0, w, h), color_name)
    ctx.draw._image.paste(pattern, (int(round(x0)), int(round(y0))), mask)  # type: ignore[attr-defined]


def draw_pie_chart(ctx: Ctx) -> None:
    """
    {"title": "Energy Split", "segments": [{"label": "HVAC", "value": 45}, ...]}

    Style (layout.yaml, not pushed data): `legend: "side"` (default) puts
    the legend to the right of the pie, sized to fill the box. `legend:
    "below"` instead centers the pie horizontally and lists the legend as
    centered, wrapped rows underneath it -- better for a wide/short box
    where a side legend would leave the pie small and off-center.
    """
    data = ctx.data or {}
    segments = data.get("segments", [])
    total = sum(float(s.get("value", 0) or 0) for s in segments)
    if not segments or total <= 0:
        _placeholder(ctx, "No data")
        return

    title = data.get("title") or ctx.style.get("title", "")
    legend_below = str(ctx.style.get("legend", "side")).lower() == "below"
    x, inset_y, w, _ = _inset(ctx.box)
    _, top, _, h = ctx.box
    bottom = top + h - PADDING

    if title and legend_below:
        # Centered, to sit visually above the now-centered pie+legend below
        # it, instead of _title_bar's normal left-aligned fieldset style.
        title_font = get_font(14, bold=True)
        ctx.draw.text(
            (x + w / 2, inset_y), title.upper(), font=title_font, fill=palette.color("black"), anchor="ma"
        )
        y0 = inset_y + 22
    elif title:
        y0 = _title_bar(ctx, title)
    else:
        y0 = inset_y

    # Figure out each slice's angle span + color, and each legend entry's
    # text, without drawing anything yet -- legend="below" needs to know
    # the legend's height (which depends on how many rows it wraps into)
    # *before* the pie's diameter can be sized, so drawing has to wait.
    default_cycle = _CHART_COLOR_CYCLE + ["orange", "gray", "pink"]
    angle = -90.0
    slice_defs: list[tuple[float, float, str]] = []  # (start, sweep, color_name)
    legend_entries: list[tuple[str, float, str]] = []  # (label, frac, color_name)
    for i, seg in enumerate(segments):
        value = float(seg.get("value", 0) or 0)
        frac = value / total
        sweep = max(frac * 360.0, 0.0)
        color_name = seg.get("color") or default_cycle[i % len(default_cycle)]
        if sweep > 0:
            slice_defs.append((angle, sweep, color_name))
        legend_entries.append((str(seg.get("label", "")), frac, color_name))
        angle += sweep

    swatch = 14
    legend_font = get_font(13, bold=False)

    if legend_below and legend_entries:
        chip_gap = 24
        # (text, color_name, chip_width, chip_height) per entry
        chips = []
        for label, frac, color_name in legend_entries:
            text = f"{label} ({frac * 100:.0f}%)"
            tw, th = _text_size(ctx.draw, text, legend_font)
            chips.append((text, color_name, swatch + 6 + tw, max(th, swatch)))

        # Greedily wrap chips into centered rows that fit the box width.
        rows: list[list[tuple[str, str, float, float]]] = []
        current: list[tuple[str, str, float, float]] = []
        current_w = 0.0
        for chip in chips:
            extra = chip[2] if not current else chip_gap + chip[2]
            if current and current_w + extra > w:
                rows.append(current)
                current, current_w = [chip], chip[2]
            else:
                current.append(chip)
                current_w += extra
        if current:
            rows.append(current)

        row_h = max((c[3] for c in chips), default=swatch) + 10
        legend_h = row_h * len(rows) + 6
        plot_h = max(bottom - y0 - legend_h, 10)
        diameter = max(int(min(plot_h, w)), 10)
        cx = x + w / 2
        cy = y0 + diameter / 2
    else:
        rows = None
        row_h = 0.0
        plot_h = bottom - y0
        diameter = max(int(min(plot_h, w * 0.5)), 10)
        cx = x + diameter / 2
        cy = y0 + plot_h / 2

    bbox = [cx - diameter / 2, cy - diameter / 2, cx + diameter / 2, cy + diameter / 2]

    for start, sweep, color_name in slice_defs:
        _fill_pieslice(ctx, bbox, start, sweep, color_name)
    # Crisp black separators/outline drawn on top, after all fills (including
    # dithered ones) so slice boundaries stay clean regardless of fill style.
    for start, sweep, _color_name in slice_defs:
        ctx.draw.pieslice(bbox, start, start + sweep, outline=palette.color("black"), width=2)
    ctx.draw.ellipse(bbox, outline=palette.color("black"), width=2)

    if legend_below and rows:
        ly = cy + diameter / 2 + 10
        for row in rows:
            row_w = sum(c[2] for c in row) + 24 * (len(row) - 1)
            lx = x + (w - row_w) / 2
            for text, color_name, chip_w, _chip_h in row:
                _fill_rect(ctx, (lx, ly + 2, lx + swatch, ly + 2 + swatch), color_name)
                ctx.draw.rectangle(
                    [lx, ly + 2, lx + swatch, ly + 2 + swatch], outline=palette.color("black")
                )
                ctx.draw.text(
                    (lx + swatch + 6, ly), text, font=legend_font, fill=palette.color("black")
                )
                lx += chip_w + 24
            ly += row_h
    elif legend_entries:
        legend_x = x + diameter + 20
        legend_w = w - diameter - 20
        if legend_w > 30:
            side_row_h = max(plot_h / len(legend_entries), 18)
            for i, (label, frac, color_name) in enumerate(legend_entries):
                ly = y0 + i * side_row_h
                _fill_rect(ctx, (legend_x, ly + 2, legend_x + swatch, ly + 2 + swatch), color_name)
                ctx.draw.rectangle(
                    [legend_x, ly + 2, legend_x + swatch, ly + 2 + swatch],
                    outline=palette.color("black"),
                )
                text = f"{label} ({frac * 100:.0f}%)"
                ctx.draw.text(
                    (legend_x + swatch + 6, ly), text, font=legend_font, fill=palette.color("black")
                )


# ---------------------------------------------------------------------------
# panel: a purely decorative border (+ optional label) around a cluster of
# other widgets, to visually group related ones -- e.g. a "UPS" box drawn
# around a battery-percent progress bar and a load metric sitting next to
# each other. It's the one widget type that doesn't need a publisher at
# all: it only reads `style` (its own entry in layout.yaml -- title/color/
# width), never `data`, so there's no widget id for anything to push to
# (though you can still give it one if you want).
#
# Give it the union rectangle of the widgets it's grouping (e.g. two
# 3-wide widgets side by side at x=6 and x=9 -> a panel at x=6, w=6) and
# list it anywhere relative to them in layout.yaml -- order doesn't matter
# here, since this only draws a border/label at the very edge of its own
# box while every other widget stays inset (PADDING) from its own cell
# edges, so there's no pixel overlap to worry about either way.
# ---------------------------------------------------------------------------
def draw_panel(ctx: Ctx) -> None:
    x, y, w, h = ctx.box
    color = palette.color(ctx.style.get("color", "black"))
    line_w = max(int(ctx.style.get("width", 2)), 1)
    title = ctx.style.get("title") or (ctx.data or {}).get("title")

    half = line_w / 2
    l, t, r, b = x + half, y + half, x + w - 1 - half, y + h - 1 - half
    ctx.draw.rectangle([l, t, r, b], outline=color, width=line_w)

    if title:
        font = get_font(13, bold=True)
        label = str(title).upper()
        tw, th = _text_size(ctx.draw, label, font)
        label_x = x + 16
        label_cy = t  # sits right on the top border line
        pad = 3
        # Punch a white gap in the border behind the label so it reads
        # like a fieldset legend instead of the line cutting through text.
        ctx.draw.rectangle(
            [label_x - pad, label_cy - th / 2 - pad, label_x + tw + pad, label_cy + th / 2 + pad],
            fill=palette.color("white"),
        )
        ctx.draw.text((label_x, label_cy), label, font=font, fill=color, anchor="lm")


WIDGET_REGISTRY: dict[str, Callable[[Ctx], None]] = {
    "header": draw_header,
    "metric": draw_metric,
    "text_list": draw_text_list,
    "weather": draw_weather,
    "forecast_strip": draw_forecast_strip,
    "calendar": draw_calendar,
    "alert_banner": draw_alert_banner,
    "progress": draw_progress,
    "image": draw_image,
    "image_metric": draw_image_metric,
    "bar_chart": draw_bar_chart,
    "pie_chart": draw_pie_chart,
    "panel": draw_panel,
    "table": draw_table,
}
