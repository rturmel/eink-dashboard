"""
Corrected driver for the Waveshare 10.85" e-Paper HAT+ (G).

Waveshare's own Python driver (waveshare_epd/epd10in85g.py) does NOT match
their C driver (EPD_10in85g.c) for this panel, and the Python version does
not work on real hardware. Two differences, both fixed here:

1. CHIP SELECT. The C driver asserts and releases CS around every single
   byte:

       DC=0; CS_M=0; CS_S=0; WriteByte(reg); CS_M=1; CS_S=1

   The Python driver instead calls CS_ALL(0) once at the start of Init(),
   holds both chip selects low for the whole register sequence, and raises
   them at the end. The controller latches on the CS rising edge, so none
   of the configuration writes commit -- command 0x04 (POWER_ON) then
   reaches an unconfigured chip and BUSY never releases. This is the bug
   that makes the stock Python driver hang forever in Init().

2. RESET TIMING. C uses 20ms high / 10ms low / 20ms high. Python used
   200 / 2 / 200 -- a 2ms low pulse where the C driver (which works) uses
   10ms.

Pixel data is still sent in bulk (CS held low across the block) rather than
byte-by-byte as the C code does, because 163,200 individual SPI writes with
four GPIO transitions each is unusably slow from Python. Holding CS across
a single contiguous data phase is what the C driver's own SendnData_0/_1
helpers do, so this stays within its behaviour.

Reference: E-paper_Separate_Program/10.85inch_e-Paper_G/RaspberryPi/c/lib/e-Paper/EPD_10in85g.c
"""

import logging

from PIL import Image

import epdconfig

logger = logging.getLogger(__name__)

EPD_WIDTH = 1360 // 2
EPD_HEIGHT = 480


class EPD:
    def __init__(self):
        self.width = EPD_WIDTH
        self.height = EPD_HEIGHT

        self.BLACK = 0x000000  # 00  BGR
        self.WHITE = 0xFFFFFF  # 01
        self.YELLOW = 0x00FFFF  # 10
        self.RED = 0x0000FF  # 11

        self.EPD_CS_M_PIN = epdconfig.EPD_CS_M_PIN
        self.EPD_CS_S_PIN = epdconfig.EPD_CS_S_PIN
        self.EPD_DC_PIN = epdconfig.EPD_DC_PIN
        self.EPD_RST_PIN = epdconfig.EPD_RST_PIN
        self.EPD_BUSY_PIN = epdconfig.EPD_BUSY_PIN
        self.EPD_PWR_PIN = epdconfig.EPD_PWR_PIN

    # ------------------------------------------------------------------
    # low level -- mirrors EPD_10in85g.c exactly
    # ------------------------------------------------------------------

    def Reset(self):
        epdconfig.digital_write(self.EPD_RST_PIN, 1)
        epdconfig.delay_ms(20)
        epdconfig.digital_write(self.EPD_RST_PIN, 0)
        epdconfig.delay_ms(10)
        epdconfig.digital_write(self.EPD_RST_PIN, 1)
        epdconfig.delay_ms(20)

    def SendCommand_0(self, reg):
        epdconfig.digital_write(self.EPD_DC_PIN, 0)
        epdconfig.digital_write(self.EPD_CS_M_PIN, 0)
        epdconfig.spi_writebyte(reg)
        epdconfig.digital_write(self.EPD_CS_M_PIN, 1)

    def SendCommand_1(self, reg):
        epdconfig.digital_write(self.EPD_DC_PIN, 0)
        epdconfig.digital_write(self.EPD_CS_S_PIN, 0)
        epdconfig.spi_writebyte(reg)
        epdconfig.digital_write(self.EPD_CS_S_PIN, 1)

    def SendCommand_ALL(self, reg):
        epdconfig.digital_write(self.EPD_DC_PIN, 0)
        epdconfig.digital_write(self.EPD_CS_M_PIN, 0)
        epdconfig.digital_write(self.EPD_CS_S_PIN, 0)
        epdconfig.spi_writebyte(reg)
        epdconfig.digital_write(self.EPD_CS_M_PIN, 1)
        epdconfig.digital_write(self.EPD_CS_S_PIN, 1)

    def SendData_0(self, data):
        epdconfig.digital_write(self.EPD_DC_PIN, 1)
        epdconfig.digital_write(self.EPD_CS_M_PIN, 0)
        epdconfig.spi_writebyte(data)
        epdconfig.digital_write(self.EPD_CS_M_PIN, 1)

    def SendData_1(self, data):
        epdconfig.digital_write(self.EPD_DC_PIN, 1)
        epdconfig.digital_write(self.EPD_CS_S_PIN, 0)
        epdconfig.spi_writebyte(data)
        epdconfig.digital_write(self.EPD_CS_S_PIN, 1)

    def SendData_ALL(self, data):
        epdconfig.digital_write(self.EPD_DC_PIN, 1)
        epdconfig.digital_write(self.EPD_CS_M_PIN, 0)
        epdconfig.digital_write(self.EPD_CS_S_PIN, 0)
        epdconfig.spi_writebyte(data)
        epdconfig.digital_write(self.EPD_CS_M_PIN, 1)
        epdconfig.digital_write(self.EPD_CS_S_PIN, 1)

    def SendnData_0(self, buf, length):
        epdconfig.digital_write(self.EPD_DC_PIN, 1)
        epdconfig.digital_write(self.EPD_CS_M_PIN, 0)
        epdconfig.spi_writebyte2(buf, length)
        epdconfig.digital_write(self.EPD_CS_M_PIN, 1)

    def SendnData_1(self, buf, length):
        epdconfig.digital_write(self.EPD_DC_PIN, 1)
        epdconfig.digital_write(self.EPD_CS_S_PIN, 0)
        epdconfig.spi_writebyte2(buf, length)
        epdconfig.digital_write(self.EPD_CS_S_PIN, 1)

    def ReadBusy(self):
        logger.info("e-Paper busy")
        while epdconfig.digital_read(self.EPD_BUSY_PIN) == 0:
            epdconfig.delay_ms(10)
        epdconfig.delay_ms(10)
        logger.info("e-Paper busy release")

    # kept so existing callers that used the old name still work
    ReadBusyH = ReadBusy

    def TurnOnDisplay(self):
        self.SendCommand_ALL(0x12)
        self.SendData_ALL(0x00)
        self.ReadBusy()

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def Init(self):
        logger.info("EPD init...")
        epdconfig.module_init()

        self.Reset()
        self.ReadBusy()

        self.SendCommand_ALL(0x4D)
        self.SendData_ALL(0x78)

        self.SendCommand_ALL(0x00)
        self.SendData_ALL(0x2F)
        self.SendData_ALL(0x29)

        # sent twice, exactly as the C driver does (47uH)
        for _ in range(2):
            self.SendCommand_ALL(0x06)
            for b in (0x0D, 0x12, 0x30, 0x20, 0x19, 0x3D, 0x0C):
                self.SendData_ALL(b)

        self.SendCommand_ALL(0x50)
        self.SendData_ALL(0x37)

        self.SendCommand_ALL(0x61)
        self.SendData_ALL(self.width // 256)
        self.SendData_ALL(self.width % 256)
        self.SendData_ALL(self.height // 256)
        self.SendData_ALL(self.height % 256)

        self.SendCommand_ALL(0x65)
        for _ in range(4):
            self.SendData_ALL(0x00)

        self.SendCommand_ALL(0xE0)
        self.SendData_ALL(0x01)

        self.SendCommand_ALL(0xE3)
        self.SendData_ALL(0x08)

        self.SendCommand_ALL(0xE5)
        self.SendData_ALL(0x08)

        self.SendCommand_ALL(0xE9)
        self.SendData_ALL(0x01)

        self.SendCommand_ALL(0x04)  # POWER_ON
        self.ReadBusy()

    def getbuffer(self, image):
        """Quantise a PIL image to the panel's 4 colours, 2 bits per pixel."""
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
        buf_4color = bytearray(image_4color.tobytes("raw"))

        width_bytes = full_width // 4  # 340
        buf = [0x00] * (width_bytes * self.height)
        idx = 0
        for j in range(self.height):
            for i in range(width_bytes):
                buf[i + j * width_bytes] = (
                    (buf_4color[idx] << 6)
                    + (buf_4color[idx + 1] << 4)
                    + (buf_4color[idx + 2] << 2)
                    + buf_4color[idx + 3]
                )
                idx += 4
        return buf

    def Clear(self, color=0x55):
        half = self.width // 4  # 170 bytes per half-row
        row = [color] * half

        self.SendCommand_0(0x10)
        for _ in range(self.height):
            self.SendnData_0(row, half)

        self.SendCommand_1(0x10)
        for _ in range(self.height):
            self.SendnData_1(row, half)

        self.TurnOnDisplay()

    def display(self, image):
        half = self.width // 4  # 170 -- bytes per half-row
        stride = self.width // 2  # 340 -- bytes per full row

        self.SendCommand_0(0x10)
        for i in range(self.height):
            start = i * stride
            self.SendnData_0(image[start : start + half], half)

        self.SendCommand_1(0x10)
        for i in range(self.height):
            start = i * stride + half
            self.SendnData_1(image[start : start + half], half)

        self.TurnOnDisplay()

    def sleep(self):
        self.SendCommand_ALL(0x02)  # POWER_OFF
        self.SendData_ALL(0x00)
        epdconfig.delay_ms(100)
        self.ReadBusy()

        self.SendCommand_ALL(0x07)  # DEEP_SLEEP
        self.SendData_ALL(0xA5)
        epdconfig.delay_ms(100)
        epdconfig.module_exit()
