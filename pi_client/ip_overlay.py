"""
Footer bar showing this Pi's own IP address.

Entirely local to pi_client. The IP is read from this machine's own routing
table -- the broker is never consulted, never contacted, and has no idea the
bar exists. That matters because the address is most useful precisely when
the broker is unreachable and you need to get at the Pi.

Not part of shared/dashboard_render for the same reason: the browser preview
would show the preview server's address, which is meaningless.

Composited over the bottom of the finished frame, so no layout change is
needed and existing widgets keep their full grid area.

EDGE CRISPNESS
--------------
The bar must have hard edges -- no anti-aliased or dithered transition
between the white frame and the black bar. Three things guarantee that:

  * the rectangle is drawn on exact integer pixel bounds, so no partial
    coverage of any pixel;
  * the fill and text use exact palette colors, so quantization is a no-op
    for them;
  * the final pass is palette.quantize_exact(), which is nearest-color with
    dithering explicitly OFF. A dithering pass would scatter red/yellow
    speckle along the boundary.

Text anti-aliasing is the one source of off-palette pixels, and it is
confined to the glyphs, snapped to black or white by the same pass.
"""

from __future__ import annotations

import logging
import socket
import sys
from pathlib import Path

from PIL import Image, ImageDraw

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR.parent / "shared") not in sys.path:
    sys.path.insert(0, str(BASE_DIR.parent / "shared"))

from dashboard_render import palette  # noqa: E402
from dashboard_render.fonts import get_font  # noqa: E402

log = logging.getLogger("ip_overlay")

DEFAULT_FONT_SIZE = 14

# Above and below the text; bar height = text height + 2 * this.
PADDING_PX = 2

# Gap between the text and the right edge of the panel.
RIGHT_MARGIN_PX = 6

# Raise the bar this many pixels off the bottom edge. Mounted panels are
# usually held in a frame or case whose lip covers the outermost rows, so
# a bar flush with the bottom edge can be physically hidden even though it
# is present in the frame. Raising it moves it into the visible area.
DEFAULT_OFFSET_PX = 0


def get_local_ip() -> str:
    """This Pi's address on whichever interface carries its default route.

    Opens a UDP socket toward a routable address and asks the kernel which
    local address it would send from. connect() on UDP only sets a default
    destination -- no packets leave the machine, nothing is contacted, and it
    works with no internet connection as long as a default route exists.

    The probe address is arbitrary and never receives traffic; it exists only
    to select a route.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(0.5)
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except OSError:
        pass
    finally:
        sock.close()

    try:
        # Fallback. Often 127.0.0.1 on Debian, hence being second.
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "no network"


def draw_ip_bar(
    image: Image.Image,
    text: str | None = None,
    font_size: int = DEFAULT_FONT_SIZE,
    offset_px: int = DEFAULT_OFFSET_PX,
) -> Image.Image:
    """Composite a full-width footer bar, white text on black, right-aligned.

    `offset_px` raises the bar off the bottom edge, for panels whose mounting
    frame covers the outermost rows. Whatever the dashboard drew below the
    bar stays visible (or stays hidden behind the frame lip, which is the
    point).

    Returns the image, modified in place and re-quantized to the panel's
    four colors.
    """
    if text is None:
        text = get_local_ip()

    width, height = image.size
    draw = ImageDraw.Draw(image)
    font = get_font(font_size, bold=False)

    # Measure the glyphs actually being drawn rather than the font's nominal
    # size -- ascent/descent vary by face, and the padding should be 2px
    # around the visible text, not around the em box.
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    text_w = right - left
    text_h = bottom - top

    bar_h = text_h + PADDING_PX * 2

    # Clamp so a large offset can't push the bar off the top of the frame.
    offset_px = max(0, min(int(offset_px), height - bar_h))
    bar_bottom = height - 1 - offset_px
    bar_top = bar_bottom - bar_h + 1

    # Integer bounds, inclusive of the last row/column: no pixel is partially
    # covered, so the edge cannot be soft.
    draw.rectangle(
        [0, bar_top, width - 1, bar_bottom],
        fill=palette.color("black"),
    )

    # textbbox offsets are subtracted so the glyphs land where intended --
    # `top` is usually non-zero, and ignoring it shifts the text down and
    # breaks the 2px padding.
    text_x = width - RIGHT_MARGIN_PX - text_w - left
    text_y = bar_top + PADDING_PX - top


    draw.text((text_x, text_y), text, font=font, fill=palette.color("white"))

    # Nearest-color, dithering off -- see the module docstring.
    return palette.quantize_exact(image)
