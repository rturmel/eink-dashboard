"""
ctypes wrapper around Waveshare's *C* driver for the 10.85" e-Paper HAT+ (G).

Why this exists
---------------
Waveshare ship two drivers for this panel that do not behave the same way:

  * ``EPD_10in85g.c``   -- the C driver. Verified working on real hardware.
  * ``epd10in85g.py``   -- the Python driver. Hangs forever in ``Init()``,
                           waiting on BUSY after command 0x04 (POWER_ON).

Attempts to fix the Python driver by matching the C driver's chip-select
handling and reset timing did not resolve the hang, so rather than continue
reverse-engineering the difference, this module builds the C driver as a
shared library and calls it directly. That guarantees byte-for-byte identical
behaviour to the demo that works.

Building the shared library
---------------------------
From a checkout of https://github.com/waveshare/e-Paper ::

    cd E-paper_Separate_Program/10.85inch_e-Paper_G/RaspberryPi/c
    gcc -shared -fPIC -O2 \\
        -o <pi_client>/waveshare_epd/libepd10in85g.so \\
        lib/Config/DEV_Config.c lib/e-Paper/EPD_10in85g.c \\
        -I lib/Config -I lib/e-Paper -I lib/GUI -I lib/Fonts \\
        -D USE_WIRINGPI_LIB -D RPI -lwiringPi -lm

WiringPi is required (the maintained fork at github.com/WiringPi/WiringPi).
The C sources build against it cleanly and that is the backend the working
demo uses.

Image buffer layout
-------------------
``getbuffer()`` produces the same 2-bits-per-pixel packed buffer the C driver
expects: 340 bytes per row (1360 px / 4), 480 rows. The C ``Display()``
function reads the left half of each row for the master IC and the right half
for the slave IC, which is why the panel's two matrices land in the right
places without any extra work here.
"""

from __future__ import annotations

import ctypes
import logging
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

EPD_WIDTH = 1360 // 2  # 680 -- half the panel, one IC's worth
EPD_HEIGHT = 480

_SO_NAME = "libepd10in85g.so"
_SEARCH_DIRS = [
    Path(__file__).parent / "waveshare_epd",
    Path(__file__).parent,
    Path("/usr/local/lib"),
    Path("/usr/lib"),
]

# Colour indices as the C driver expects them (2 bits per pixel).
BLACK_IDX = 0
WHITE_IDX = 1
YELLOW_IDX = 2
RED_IDX = 3


def _load_library() -> ctypes.CDLL:
    for directory in _SEARCH_DIRS:
        candidate = directory / _SO_NAME
        if candidate.exists():
            logger.info("loading %s", candidate)
            return ctypes.CDLL(str(candidate))
    raise RuntimeError(
        f"Cannot find {_SO_NAME}. Build it from Waveshare's C sources -- "
        "see the module docstring in epd10in85g_clib.py for the gcc command."
    )


class EPD:
    def __init__(self):
        self.width = EPD_WIDTH
        self.height = EPD_HEIGHT

        # Kept for API compatibility with the pure-Python driver.
        self.BLACK = 0x000000
        self.WHITE = 0xFFFFFF
        self.YELLOW = 0x00FFFF
        self.RED = 0x0000FF

        self._lib = _load_library()

        self._lib.DEV_Module_Init.restype = ctypes.c_ubyte
        self._lib.EPD_10in85g_Init.restype = None
        self._lib.EPD_10in85g_Clear.argtypes = [ctypes.c_ubyte]
        self._lib.EPD_10in85g_Clear.restype = None
        self._lib.EPD_10in85g_Display.argtypes = [ctypes.POINTER(ctypes.c_ubyte)]
        self._lib.EPD_10in85g_Display.restype = None
        self._lib.EPD_10in85g_Sleep.restype = None
        self._lib.DEV_Module_Exit.restype = None

    def Init(self) -> None:
        rc = self._lib.DEV_Module_Init()
        if rc != 0:
            raise RuntimeError(f"DEV_Module_Init() failed with code {rc}")
        self._lib.EPD_10in85g_Init()

    def Clear(self, color: int = WHITE_IDX) -> None:
        """Fill the panel with one of the four colours.

        Note this takes a 2-bit colour *index* (0=black, 1=white, 2=yellow,
        3=red), matching the C API -- not the pre-expanded byte the Python
        driver used.
        """
        self._lib.EPD_10in85g_Clear(ctypes.c_ubyte(color & 0x03))

    def display(self, image) -> None:
        buf = (ctypes.c_ubyte * len(image))(*image)
        self._lib.EPD_10in85g_Display(buf)

    def sleep(self) -> None:
        self._lib.EPD_10in85g_Sleep()
        self._lib.DEV_Module_Exit()

    # ------------------------------------------------------------------
    # image packing -- identical output to Waveshare's Python getbuffer()
    # ------------------------------------------------------------------

    def getbuffer(self, image: Image.Image):
        pal_image = Image.new("P", (1, 1))
        pal_image.putpalette(
            (0, 0, 0, 255, 255, 255, 255, 255, 0, 255, 0, 0) + (0, 0, 0) * 252
        )

        imwidth, imheight = image.size
        full_width = self.width * 2  # 1360

        if imwidth == full_width and imheight == self.height:
            image_temp = image
        elif imwidth == self.height and imheight == full_width:
            image_temp = image.rotate(90, expand=True)
        else:
            raise ValueError(
                "Invalid image dimensions: %d x %d, expected %d x %d"
                % (imwidth, imheight, full_width, self.height)
            )

        image_4color = image_temp.convert("RGB").quantize(palette=pal_image)
        raw = bytearray(image_4color.tobytes("raw"))

        width_bytes = full_width // 4  # 340
        buf = bytearray(width_bytes * self.height)
        idx = 0
        for j in range(self.height):
            base = j * width_bytes
            for i in range(width_bytes):
                buf[base + i] = (
                    (raw[idx] << 6)
                    + (raw[idx + 1] << 4)
                    + (raw[idx + 2] << 2)
                    + raw[idx + 3]
                )
                idx += 4
        return buf
