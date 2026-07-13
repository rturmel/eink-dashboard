"""
Color palette for the Waveshare 10.85" e-Paper HAT+ (G).

The (G) variant is a genuine 4-color panel (Black / White / Red / Yellow) -
it is NOT grayscale and does NOT support arbitrary RGB. Widgets should draw
using ONLY these four colors so what you see in the local preview matches
what the physical panel will show (the vendor driver will nearest-match any
other color, which can look muddy on a 4-color panel).

The exact RGB values below are close approximations of the panel's real
appearance (used for the on-screen preview). If your physical prints look
off after your first real refresh, tweak these three constants -- nothing
else needs to change since every widget references these names, not raw
RGB tuples.
"""

from __future__ import annotations

# Approximate on-screen preview colors. Adjust after your first real
# hardware test if the physical panel's red/yellow look different.
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
RED = (196, 30, 40)
YELLOW = (255, 205, 0)

PALETTE = {
    "black": BLACK,
    "white": WHITE,
    "red": RED,
    "yellow": YELLOW,
}

# Flat list in the order most Waveshare 4-color drivers expect when you
# build a PIL "P" mode palette image (used only for the preview's
# nearest-color quantization pass -- the real panel driver does its own
# quantization from a plain RGB image).
_FLAT_PALETTE = []
for _rgb in (BLACK, WHITE, RED, YELLOW):
    _FLAT_PALETTE.extend(_rgb)
# PIL requires exactly 256 colors (768 values) in a palette image; pad by
# repeating the last color.
_FLAT_PALETTE += list(WHITE) * (256 - len(PALETTE))


def color(name: str) -> tuple[int, int, int]:
    """Look up a palette color by name ('black' | 'white' | 'red' | 'yellow')."""
    try:
        return PALETTE[name.lower()]
    except KeyError as exc:
        raise ValueError(
            f"Unknown palette color {name!r}. Valid solid colors: {list(PALETTE)}. "
            f"Valid dithered blends: {list(BLENDS)}."
        ) from exc


def quantize_to_palette(image, dither: bool = True):
    """
    Snap an arbitrary RGB PIL image down to the panel's 4 real colors.

    For photos/gradients, `dither=True` (the default, matching Waveshare's
    own recommendation for their color panels) uses Floyd-Steinberg
    error-diffusion dithering, which approximates far more colors/shades
    than the panel can literally display by scattering red/yellow/black/
    white dots -- similar to a newsprint halftone. For flat graphics with
    sharp edges (a logo, a QR code) pass `dither=False` for crisp,
    un-dithered edges instead.
    """
    from PIL import Image

    pal_img = Image.new("P", (1, 1))
    pal_img.putpalette(_FLAT_PALETTE)
    rgb = image.convert("RGB")
    mode = Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE
    quantized = rgb.quantize(palette=pal_img, dither=mode)
    return quantized.convert("RGB")


# ---------------------------------------------------------------------------
# Dithered blends: approximate colors the panel can't show directly (orange,
# pink, gray, ...) by dithering two of the 4 real colors together as fine
# full-height vertical stripes (see dithered_fill()'s docstring for why
# stripes rather than a 2D dot/checkerboard pattern). This only makes sense
# for FILLED SHAPES (bars, pie slices, banners, progress fills) -- not for
# text or thin lines, which just look broken up. Use dithered_fill() below,
# or widgets.py's _fill_rect() helper, which already knows to route blend
# names here automatically.
# ---------------------------------------------------------------------------
BLENDS: dict[str, tuple[str, str, float]] = {
    # name: (color_a, color_b, fraction that is color_a)
    "orange": ("red", "yellow", 0.55),
    "pink": ("red", "white", 0.35),
    "gray": ("black", "white", 0.5),
    "grey": ("black", "white", 0.5),
    "light_gray": ("black", "white", 0.25),
    "light_grey": ("black", "white", 0.25),
    "dark_gray": ("black", "white", 0.75),
    "dark_grey": ("black", "white", 0.75),
}

def is_blend(name: str) -> bool:
    return name.lower() in BLENDS


def _bayer_matrix(n: int) -> list[list[int]]:
    """Classic recursive Bayer ordered-dither matrix (n must be a power of
    2). Deliberately NOT a pure horizontal/vertical stripe pattern -- axis-
    aligned line patterns are the textbook worst case for moire when an
    image gets resampled at any non-integer scale factor (a browser
    shrinking a screenshot, a chat preview thumbnail, ...), which is
    exactly why real halftone printing uses angled/dispersed dot screens
    instead of parallel lines. A Bayer matrix disperses its dots diagonally
    and holds up far better under arbitrary scaling."""
    if n == 1:
        return [[0]]
    half = _bayer_matrix(n // 2)
    size = n // 2
    result = [[0] * n for _ in range(n)]
    for y in range(size):
        for x in range(size):
            v = half[y][x]
            result[y][x] = 4 * v
            result[y][x + size] = 4 * v + 2
            result[y + size][x] = 4 * v + 3
            result[y + size][x + size] = 4 * v + 1
    return result


_BAYER_4 = _bayer_matrix(4)
_BAYER_2 = _bayer_matrix(2)


def _tile_for_box(w: int, h: int) -> tuple[list[list[int]], int]:
    """
    Pick a Bayer tile small enough to complete at least 2 full periods in
    the shorter dimension of the box, so a thin fill (a slim progress bar,
    a short banner) doesn't get cut off mid-pattern and collapse into a
    solid band of one raw color instead of reading as a blend -- a real bug
    this once caused. Falls back to the finer 4x4 tile whenever there's
    room for it (pie slices, bar-chart bars, anything reasonably sized).
    """
    short_side = max(min(w, h), 1)
    if short_side >= 8:
        return _BAYER_4, 4
    return _BAYER_2, 2


def _dither_tile(color_a: tuple[int, int, int], color_b: tuple[int, int, int], ratio: float, matrix, n: int):
    from PIL import Image

    threshold_count = round(ratio * n * n)
    cells = sorted(((matrix[y][x], x, y) for y in range(n) for x in range(n)))
    a_cells = {(x, y) for _, x, y in cells[:threshold_count]}

    tile = Image.new("RGB", (n, n))
    for y in range(n):
        for x in range(n):
            tile.putpixel((x, y), color_a if (x, y) in a_cells else color_b)
    return tile


def dithered_fill(image, box: tuple[int, int, int, int], name: str) -> None:
    """
    Fill a rectangular region of `image` (a real PIL Image, not just an
    ImageDraw) with a Bayer-dithered approximation of `name` (one of
    BLENDS), using a tile size chosen to fit the box (see _tile_for_box).
    """
    x, y, w, h = (int(round(v)) for v in box)
    w, h = max(w, 0), max(h, 0)
    if w == 0 or h == 0:
        return

    a_name, b_name, ratio = BLENDS[name.lower()]
    matrix, n = _tile_for_box(w, h)
    tile = _dither_tile(color(a_name), color(b_name), ratio, matrix, n)
    for ty in range(0, h, n):
        for tx in range(0, w, n):
            piece = tile
            rem_w, rem_h = min(n, w - tx), min(n, h - ty)
            if rem_w < n or rem_h < n:
                piece = tile.crop((0, 0, rem_w, rem_h))
            image.paste(piece, (x + tx, y + ty))


# ---------------------------------------------------------------------------
# Final whole-canvas quantization pass.
#
# PIL/FreeType anti-aliases text by default (drawing a "1" mode/bitmap font
# is the only built-in way to avoid it), which produces intermediate gray
# RGB values along every glyph edge -- colors that don't exist on a 4-color
# panel. The panel driver will nearest-match those down to a real color
# itself if we don't, but naive nearest-RGB-distance quantization is a trap
# here: mid-gray (127,127,127) is numerically *closer* to this palette's red
# (196,30,40) than to black or white, so black text edges could pick up a
# reddish fringe under plain distance-based quantization. render_dashboard()
# runs every frame through quantize_exact() as its last step specifically to
# avoid that -- near-neutral pixels are resolved to black/white by
# brightness first, and only pixels with real color saturation are matched
# against red/yellow. Already-exact palette pixels (which is everything
# widgets draw except anti-aliased text/curve edges) pass through unchanged.
# ---------------------------------------------------------------------------
_GRAY_SPREAD_THRESHOLD = 40  # max-min channel spread below this = "near-neutral"

try:
    import numpy as _np
except ImportError:  # pragma: no cover - numpy should always be present, but
    _np = None        # degrade gracefully instead of hard-failing a render.


def quantize_exact(image):
    """Snap every pixel in `image` to one of the 4 real panel colors,
    treating near-neutral (grayish) pixels specially so they resolve to
    black/white by brightness instead of accidentally snapping to red/
    yellow. Safe to call on an already-exact image (no-op in that case)."""
    if _np is not None:
        return _quantize_exact_numpy(image)
    return quantize_to_palette(image, dither=False)  # slower fallback, no gray-bias fix


def _quantize_exact_numpy(image):
    from PIL import Image

    # int32, not int16: channel differences are squared below (up to 255**2
    # = 65025), which overflows int16's 32767 max and silently wraps to
    # garbage -- a real bug caught during testing (it was quantizing exact
    # red/yellow pixels to white/black). int32 has ample headroom.
    arr = _np.asarray(image.convert("RGB"), dtype=_np.int32)
    names = ["black", "white", "red", "yellow"]
    rgbs = _np.array([PALETTE[n] for n in names], dtype=_np.int32)

    # squared distance to each of the 4 real colors, per pixel
    dists = ((arr[..., None, :] - rgbs) ** 2).sum(axis=-1)  # H,W,4
    nearest_idx = dists.argmin(axis=-1)

    spread = arr.max(axis=-1) - arr.min(axis=-1)
    luminance = arr.mean(axis=-1)
    bw_idx = _np.where(luminance < 128, 0, 1)  # index into `names`: 0=black,1=white
    final_idx = _np.where(spread < _GRAY_SPREAD_THRESHOLD, bw_idx, nearest_idx)

    out = rgbs.astype("uint8")[final_idx]
    return Image.fromarray(out, mode="RGB")
