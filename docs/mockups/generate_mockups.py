"""
Generates sample dashboard mockups using the real rendering pipeline
(shared/dashboard_render) with representative sample data -- the same
code path pi_client and preview use, so these PNGs are an accurate
preview of what the physical panel will show, not hand-drawn mockups.

Run from anywhere:
    python3 docs/mockups/generate_mockups.py
Regenerates the three PNGs in this same directory.
"""

from __future__ import annotations

import base64
import io
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]  # docs/mockups/ -> repo root
OUT_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(REPO_DIR / "shared"))
from dashboard_render import render_dashboard, DEFAULT_WIDTH, DEFAULT_HEIGHT, palette  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402
from dashboard_render.fonts import get_font  # noqa: E402


# ---------------------------------------------------------------------------
# Mockup 1: realistic full home dashboard (the shipped layout.example.yaml)
# ---------------------------------------------------------------------------
def mockup_home_dashboard() -> None:
    layout = {
        "grid": {"cols": 12, "rows": 6},
        "widgets": [
            {"id": "header", "type": "header", "x": 0, "y": 0, "w": 12, "h": 1},
            {"id": "weather_outdoor", "type": "weather", "x": 0, "y": 1, "w": 4, "h": 2, "title": "Outdoor"},
            {"id": "metric_indoor_temp", "type": "metric", "x": 4, "y": 1, "w": 2, "h": 2, "title": "Indoor"},
            {"id": "metric_indoor_humidity", "type": "metric", "x": 6, "y": 1, "w": 2, "h": 2, "title": "Humidity"},
            {"id": "list_status", "type": "text_list", "x": 8, "y": 1, "w": 4, "h": 2, "title": "Status"},
            {"id": "agenda_today", "type": "calendar", "x": 0, "y": 3, "w": 6, "h": 2, "title": "Today"},
            {"id": "progress_vacuum", "type": "progress", "x": 6, "y": 3, "w": 3, "h": 2, "title": "Vacuum Battery"},
            {"id": "metric_extra", "type": "metric", "x": 9, "y": 3, "w": 3, "h": 2, "title": "Power Use"},
            {"id": "alert_banner", "type": "alert_banner", "x": 0, "y": 5, "w": 12, "h": 1},
        ],
    }
    state = {
        "header": {"title": "Home Dashboard", "subtitle": "Living Room Display", "time": "7:42 PM"},
        "weather_outdoor": {
            "condition": "cloudy", "temp": 64, "temp_unit": "°F",
            "high": 71, "low": 55, "humidity": 58,
        },
        "metric_indoor_temp": {"label": "Indoor", "value": 70, "unit": "°F"},
        "metric_indoor_humidity": {"label": "Humidity", "value": 44, "unit": "%"},
        "list_status": {"items": [
            {"label": "Front Door", "value": "Locked"},
            {"label": "Garage", "value": "Open", "color": "red"},
            {"label": "Alarm", "value": "Armed (Home)"},
            {"label": "Dishwasher", "value": "Running"},
        ]},
        "agenda_today": {"events": [
            {"time": "9:00 AM", "title": "Team standup"},
            {"time": "12:30 PM", "title": "Lunch with Sam"},
            {"time": "3:15 PM", "title": "Dentist appointment"},
            {"time": "5:00 PM", "title": "Pick up kids"},
        ]},
        "progress_vacuum": {"label": "Vacuum Battery", "value": 34},
        "metric_extra": {"label": "Power Use", "value": 2.1, "unit": "kW", "color": "red"},
        "alert_banner": {"active": True, "level": "warning", "text": "Garage door open 20+ minutes"},
    }
    img = render_dashboard(layout, state, DEFAULT_WIDTH, DEFAULT_HEIGHT)
    img.save(OUT_DIR / "mockup_1_home_dashboard.png")
    print("saved mockup_1_home_dashboard.png")


# ---------------------------------------------------------------------------
# Mockup 2: widget gallery -- one of every widget type, deliberately using
# all four colors so the palette range is obvious at a glance.
# ---------------------------------------------------------------------------
def _make_logo_base64() -> str:
    """A tiny synthetic monogram logo, generated in-memory (not saved to
    assets/) just so the image widget has something real to render."""
    img = Image.new("RGBA", (240, 240), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([10, 10, 230, 230], fill=(20, 20, 20, 255))
    font = get_font(96, bold=True)
    d.text((120, 120), "EP", font=font, fill=(255, 255, 255, 255), anchor="mm")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def mockup_widget_gallery() -> None:
    layout = {
        "grid": {"cols": 12, "rows": 6},
        "widgets": [
            {"id": "hdr", "type": "header", "x": 0, "y": 0, "w": 12, "h": 1},
            {"id": "wx", "type": "weather", "x": 0, "y": 1, "w": 3, "h": 2, "title": "Weather"},
            {"id": "m1", "type": "metric", "x": 3, "y": 1, "w": 2, "h": 2, "title": "CPU Temp"},
            {"id": "m2", "type": "metric", "x": 5, "y": 1, "w": 2, "h": 2, "title": "Battery"},
            {"id": "list1", "type": "text_list", "x": 7, "y": 1, "w": 5, "h": 2, "title": "Status"},
            {"id": "bar", "type": "bar_chart", "x": 0, "y": 3, "w": 4, "h": 2, "title": "Power (kWh)"},
            {"id": "pie", "type": "pie_chart", "x": 4, "y": 3, "w": 4, "h": 2, "title": "Energy Split"},
            {"id": "logo", "type": "image", "x": 8, "y": 3, "w": 2, "h": 2, "title": "Logo"},
            {"id": "prog", "type": "progress", "x": 10, "y": 3, "w": 2, "h": 2, "title": "Print Job"},
            {"id": "alert", "type": "alert_banner", "x": 0, "y": 5, "w": 12, "h": 1},
        ],
    }
    state = {
        "hdr": {"title": "Widget Gallery", "subtitle": "One of every widget type", "time": "11:05 AM"},
        "wx": {"condition": "storm", "temp": 51, "temp_unit": "°F", "high": 55, "low": 44, "humidity": 88},
        "m1": {"label": "CPU Temp", "value": 74, "unit": "°C", "color": "red"},
        "m2": {"label": "Battery", "value": 92, "unit": "%"},
        "list1": {"items": [
            {"label": "Router", "value": "Online"},
            {"label": "Freezer", "value": "Alert", "color": "red"},
            {"label": "Mail", "value": "3 New"},
        ]},
        "bar": {"bars": [
            {"label": "Mon", "value": 12}, {"label": "Tue", "value": 18},
            {"label": "Wed", "value": 9}, {"label": "Thu", "value": 21},
            {"label": "Fri", "value": 15},
        ]},
        "pie": {"segments": [
            {"label": "HVAC", "value": 45}, {"label": "Kitchen", "value": 25}, {"label": "Other", "value": 30},
        ]},
        # dither: false -- this is a flat graphic (solid shapes + text), not
        # a photo, so crisp edges beat Floyd-Steinberg's tonal dithering here.
        "logo": {"image_base64": _make_logo_base64(), "dither": False},
        "prog": {"label": "Print Job", "value": 78},
        "alert": {"active": True, "level": "critical", "text": "Freezer temperature above threshold"},
    }
    img = render_dashboard(layout, state, DEFAULT_WIDTH, DEFAULT_HEIGHT)
    img.save(OUT_DIR / "mockup_2_widget_gallery.png")
    print("saved mockup_2_widget_gallery.png")


# ---------------------------------------------------------------------------
# Mockup 3: the 4-color palette reference
# ---------------------------------------------------------------------------
def mockup_color_palette() -> None:
    img = Image.new("RGB", (DEFAULT_WIDTH, DEFAULT_HEIGHT), palette.color("white"))
    draw = ImageDraw.Draw(img)

    title_font = get_font(30, bold=True)
    sub_font = get_font(16, bold=False)
    label_font = get_font(22, bold=True)

    draw.text((20, 16), "Panel Color Palette", font=title_font, fill=palette.color("black"))
    draw.text(
        (20, 56),
        "The 10.85\" HAT+ (G) can only show these four colors -- every widget draws with one of them.",
        font=sub_font,
        fill=palette.color("black"),
    )
    draw.line([(0, 90), (DEFAULT_WIDTH, 90)], fill=palette.color("black"), width=2)

    swatches = [
        ("black", "Black"),
        ("white", "White"),
        ("red", "Red"),
        ("yellow", "Yellow"),
    ]
    margin = 30
    gap = 24
    top = 120
    bottom = DEFAULT_HEIGHT - 40
    sw_w = (DEFAULT_WIDTH - 2 * margin - gap * (len(swatches) - 1)) / len(swatches)

    for i, (name, label) in enumerate(swatches):
        x0 = margin + i * (sw_w + gap)
        x1 = x0 + sw_w
        draw.rectangle([x0, top, x1, bottom - 50], fill=palette.color(name), outline=palette.color("black"), width=2)
        rgb = palette.color(name)
        draw.text(((x0 + x1) / 2, bottom - 30), label, font=label_font, fill=palette.color("black"), anchor="mm")
        draw.text(
            ((x0 + x1) / 2, bottom - 6),
            f"RGB {rgb}",
            font=sub_font,
            fill=palette.color("black"),
            anchor="mm",
        )

    img.save(OUT_DIR / "mockup_3_color_palette.png")
    print("saved mockup_3_color_palette.png")


# ---------------------------------------------------------------------------
# Mockup 4: dithered blends (orange/pink/gray) -- colors the panel can't
# show directly, approximated by ordered-dithering two real colors together.
# See docs/WIDGETS.md's "Colors: solid vs. dithered blends" section.
# ---------------------------------------------------------------------------
def mockup_dithered_blends() -> None:
    layout = {
        "grid": {"cols": 12, "rows": 6},
        "widgets": [
            {"id": "hdr", "type": "header", "x": 0, "y": 0, "w": 12, "h": 1},
            {"id": "bar", "type": "bar_chart", "x": 0, "y": 1, "w": 6, "h": 3, "title": "Solid + Blended Bars"},
            {"id": "pie", "type": "pie_chart", "x": 6, "y": 1, "w": 6, "h": 3, "title": "6-Segment Pie (only 3 real colors)"},
            {"id": "prog", "type": "progress", "x": 0, "y": 4, "w": 4, "h": 1, "title": "Progress (blended fill)"},
            {"id": "alert", "type": "alert_banner", "x": 4, "y": 4, "w": 8, "h": 1},
            {"id": "note", "type": "text_list", "x": 0, "y": 5, "w": 12, "h": 1},
        ],
    }
    state = {
        "hdr": {
            "title": "Dithered Color Blends",
            "subtitle": "Orange = red+yellow dot pattern, pink = red+white, gray = black+white -- the panel only has 4 real colors",
            "time": "2:15 PM",
        },
        "bar": {"bars": [
            {"label": "Black", "value": 20, "color": "black"},
            {"label": "Red", "value": 24, "color": "red"},
            {"label": "Yellow", "value": 14, "color": "yellow"},
            {"label": "Orange", "value": 22, "color": "orange"},
            {"label": "Pink", "value": 16, "color": "pink"},
            {"label": "Gray", "value": 19, "color": "gray"},
        ]},
        "pie": {"segments": [
            {"label": "A", "value": 20}, {"label": "B", "value": 18}, {"label": "C", "value": 15},
            {"label": "D (orange)", "value": 17}, {"label": "E (gray)", "value": 15}, {"label": "F (pink)", "value": 15},
        ]},
        "prog": {"label": "Disk Usage", "value": 71, "color": "orange"},
        "alert": {"active": True, "level": "warning", "color": "pink", "text": "Custom-colored banner (pink blend)"},
        "note": {"items": [
            {"label": "Zoom in", "value": "and you'll see a dot pattern -- from a few feet away it reads as a solid color, like halftone printing."},
        ]},
    }
    img = render_dashboard(layout, state, DEFAULT_WIDTH, DEFAULT_HEIGHT)
    img.save(OUT_DIR / "mockup_4_dithered_blends.png")
    print("saved mockup_4_dithered_blends.png")


if __name__ == "__main__":
    mockup_home_dashboard()
    mockup_widget_gallery()
    mockup_color_palette()
    mockup_dithered_blends()
