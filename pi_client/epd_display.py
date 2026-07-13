"""
Thin wrapper around the vendor `waveshare_epd.epd10in85g` driver.

In dry_run mode (or if the driver/hardware isn't available -- e.g. you're
developing this client on a laptop, not the Pi) it just writes the
rendered frame to a PNG file instead of touching SPI, so the rest of the
client logic can be built and tested off-Pi.

The 10.85" HAT+ (G) does NOT support partial refresh -- every call to
show() is a full-panel redraw (~21s). That's expected and handled by the
debouncing in client.py, not here.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image

log = logging.getLogger("epd_display")


class EPDDisplay:
    def __init__(
        self,
        dry_run: bool = False,
        output_path: str = "./preview_frame.png",
        rotate: int = 0,
    ):
        self.dry_run = dry_run
        self.output_path = Path(output_path)
        self.rotate = rotate
        self._epd = None

        if not dry_run:
            try:
                from waveshare_epd import epd10in85g  # type: ignore

                self._epd = epd10in85g.EPD()
                self._epd.init()
                log.info("initialized epd10in85g hardware driver")
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "could not load/init waveshare_epd.epd10in85g (%s) -- "
                    "falling back to dry-run mode. See pi_client/install.sh "
                    "if you're on the actual Pi and expected hardware to work.",
                    exc,
                )
                self._epd = None
                self.dry_run = True

    def show(self, image: Image.Image) -> None:
        if self.rotate:
            image = image.rotate(self.rotate, expand=True)

        if self.dry_run or self._epd is None:
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            image.save(self.output_path)
            log.info("dry-run: wrote frame to %s", self.output_path)
            return

        try:
            self._epd.display(self._epd.getbuffer(image))
        except Exception:
            log.exception("failed to push frame to display")

    def clear(self) -> None:
        if self._epd is not None:
            self._epd.Clear()

    def sleep(self) -> None:
        """Put the panel into low-power mode. Call on clean shutdown."""
        if self._epd is not None:
            self._epd.sleep()
