#!/usr/bin/env python3
"""
Recover the e-Paper panel from a hung or interrupted refresh.

Hardware-resets the panel, sends POWER_OFF then DEEP_SLEEP, and drops the
HAT's power-enable pin so the panel is genuinely de-energised.

Why this exists
---------------
If a refresh is interrupted -- Ctrl+C, a crash, or an Init() that hangs
waiting on BUSY after POWER_ON (0x04) -- the panel is left powered in its
high-voltage state. Waveshare's precaution #1 for this panel:

    Note that the screen cannot be powered on for a long time. When the
    screen does not refresh, the screen must be set to sleep mode or power
    off. Otherwise, the screen will remain in a high voltage state for a
    long time, which will damage the diaphragm and cannot be repaired.

Pulling the wall plug also de-energises it, but does so mid-transaction.
This shuts it down in the order the controller expects.

Deliberately contains NO busy waits, so it cannot itself hang -- which
matters, because the situations you need it for are exactly the ones where
BUSY is stuck.

Usage
-----
    sudo ./venv/bin/python reset_panel.py

Afterwards the panel is in deep sleep. A normal Init() wakes it; deep sleep
is exited by re-initialising, so no special handling is needed by callers.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# The vendored epd10in85g.py does a flat `import epdconfig`, so the
# waveshare_epd directory itself has to be on sys.path -- same shim as
# epd_display.py uses.
_WAVESHARE_EPD_DIR = str(Path(__file__).parent / "waveshare_epd")
if _WAVESHARE_EPD_DIR not in sys.path:
    sys.path.insert(0, _WAVESHARE_EPD_DIR)

import epdconfig as cfg  # noqa: E402  (must follow the sys.path insert)

RST = cfg.EPD_RST_PIN
DC = cfg.EPD_DC_PIN
CS_M = cfg.EPD_CS_M_PIN
CS_S = cfg.EPD_CS_S_PIN

CMD_POWER_OFF = 0x02
CMD_DEEP_SLEEP = 0x07
DEEP_SLEEP_MAGIC = 0xA5


def send_command(reg: int) -> None:
    """Send a command byte to both controller ICs.

    CS is asserted and released around the byte, matching EPD_10in85g.c --
    this panel latches on the CS rising edge.
    """
    cfg.digital_write(DC, 0)
    cfg.digital_write(CS_M, 0)
    cfg.digital_write(CS_S, 0)
    cfg.spi_writebyte(reg)
    cfg.digital_write(CS_M, 1)
    cfg.digital_write(CS_S, 1)


def send_data(value: int) -> None:
    """Send a data byte to both controller ICs."""
    cfg.digital_write(DC, 1)
    cfg.digital_write(CS_M, 0)
    cfg.digital_write(CS_S, 0)
    cfg.spi_writebyte(value)
    cfg.digital_write(CS_M, 1)
    cfg.digital_write(CS_S, 1)


def hardware_reset() -> None:
    """Toggle RST with the timing from Waveshare's C driver (20/10/20 ms)."""
    cfg.digital_write(RST, 1)
    time.sleep(0.020)
    cfg.digital_write(RST, 0)
    time.sleep(0.010)
    cfg.digital_write(RST, 1)
    time.sleep(0.020)


def main() -> int:
    cfg.module_init()

    print("hardware reset...")
    hardware_reset()

    print("power off...")
    send_command(CMD_POWER_OFF)
    send_data(0x00)
    time.sleep(0.1)

    print("deep sleep...")
    send_command(CMD_DEEP_SLEEP)
    send_data(DEEP_SLEEP_MAGIC)
    time.sleep(0.1)

    # Also drops the PWR pin, so the panel is de-energised rather than idle.
    cfg.module_exit()
    print("done -- panel in deep sleep, rails down")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
