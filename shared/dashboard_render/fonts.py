"""
Font loading with graceful fallback.

Raspberry Pi OS (and most Debian-based systems) ship DejaVu Sans via the
`fonts-dejavu-core` package, which is installed by pi_client/install.sh.
If it isn't found (e.g. running the preview server on a different OS),
we fall back to whatever PIL can find, and finally to its built-in
bitmap font so the code never crashes -- it just looks worse until a
real font is available.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

_CANDIDATES_REGULAR = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # last resort mix
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]
_CANDIDATES_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]

# Allow overriding via env var or a fonts/ directory dropped next to the
# render package (see README) without touching this file.
_LOCAL_FONT_DIR = Path(__file__).parent / "fonts"


def _first_existing(paths: list[str]) -> str | None:
    for p in paths:
        if Path(p).exists():
            return p
    return None


@lru_cache(maxsize=None)
def get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    local_regular = _LOCAL_FONT_DIR / "Regular.ttf"
    local_bold = _LOCAL_FONT_DIR / "Bold.ttf"

    if bold and local_bold.exists():
        return ImageFont.truetype(str(local_bold), size)
    if not bold and local_regular.exists():
        return ImageFont.truetype(str(local_regular), size)

    path = _first_existing(_CANDIDATES_BOLD if bold else _CANDIDATES_REGULAR)
    if path:
        return ImageFont.truetype(path, size)

    # Absolute last resort: PIL's built-in bitmap font (fixed size, ugly,
    # but never crashes).
    return ImageFont.load_default()
