"""
Top-level renderer: takes a layout definition + current dashboard state and
produces a single PIL Image sized to the panel (1360x480 by default).

Used identically by pi_client (renders, then hands the image to the EPD
driver) and preview (renders, then serves the PNG over HTTP) -- this is
the one place that defines what the dashboard looks like.
"""

from __future__ import annotations

from typing import Any

from PIL import Image, ImageDraw

from . import palette
from .widgets import WIDGET_REGISTRY, Ctx

DEFAULT_WIDTH = 1360
DEFAULT_HEIGHT = 480


def render_dashboard(
    layout: dict[str, Any],
    state: dict[str, Any],
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
) -> Image.Image:
    """
    layout: parsed layout.yaml, shape:
        {
          "grid": {"cols": 12, "rows": 4},
          "widgets": [
            {"id": "hdr", "type": "header", "x":0,"y":0,"w":12,"h":1, "title": "..."},
            {"id": "temp", "type": "metric", "x":0,"y":1,"w":3,"h":2, "title": "Indoor"},
            ...
          ]
        }
    state: {"hdr": {...}, "temp": {...}, ...}  keyed by widget id
    """
    image = Image.new("RGB", (width, height), palette.color("white"))
    draw = ImageDraw.Draw(image)
    draw._image = image  # small back-reference used by the image widget to paste

    grid = layout.get("grid", {"cols": 12, "rows": 4})
    cols = max(int(grid.get("cols", 12)), 1)
    rows = max(int(grid.get("rows", 4)), 1)
    cell_w = width / cols
    cell_h = height / rows

    for widget in layout.get("widgets", []):
        wtype = widget.get("type")
        fn = WIDGET_REGISTRY.get(wtype)
        if fn is None:
            continue  # unknown widget type in layout.yaml -- skip, don't crash

        gx, gy = int(widget.get("x", 0)), int(widget.get("y", 0))
        gw, gh = int(widget.get("w", 1)), int(widget.get("h", 1))
        box = (
            round(gx * cell_w),
            round(gy * cell_h),
            round(gw * cell_w),
            round(gh * cell_h),
        )

        widget_id = widget.get("id")
        data = state.get(widget_id, {}) if widget_id else {}
        style = {k: v for k, v in widget.items() if k not in ("x", "y", "w", "h", "id", "type")}

        ctx = Ctx(draw=draw, box=box, data=data, style=style)
        try:
            fn(ctx)
        except Exception as exc:  # noqa: BLE001 - a broken widget must not kill the frame
            _draw_error(draw, box, str(exc))

        if layout.get("debug_grid"):
            x, y, w, h = box
            draw.rectangle([x, y, x + w, y + h], outline=palette.color("red"), width=1)

    # Anti-aliased text edges (the panel has no anti-aliasing of its own --
    # see palette.quantize_exact()'s docstring) are the only source of
    # non-palette colors at this point; snap the whole frame down to the 4
    # real colors so what's returned here is byte-for-byte what the panel
    # will show, and what the preview shows matches exactly.
    return palette.quantize_exact(image)


def _draw_error(draw: ImageDraw.ImageDraw, box, message: str) -> None:
    from .fonts import get_font

    x, y, w, h = box
    draw.rectangle([x, y, x + w, y + h], outline=palette.color("red"), width=2)
    font = get_font(12, bold=False)
    draw.text((x + 6, y + 6), f"render error: {message[:40]}", font=font, fill=palette.color("red"))
