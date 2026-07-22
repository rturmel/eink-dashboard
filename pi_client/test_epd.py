import sys, time
sys.path.insert(0, "waveshare_epd")
from waveshare_epd import epd10in85g, epdconfig

epd = epd10in85g.EPD()

def bounded_busy(self, timeout=60):
    start = time.time()
    print("busy H")
    while epdconfig.digital_read(self.EPD_BUSY_PIN) == 0:
        if time.time() - start > timeout:
            print(f"TIMED OUT after {timeout}s -- forcing ahead anyway")
            return
        epdconfig.delay_ms(20)
    print("busy release (%.2fs)" % (time.time() - start))

epd10in85g.EPD.ReadBusyH = bounded_busy

print("Init...")
epd.Init()
print("Clear...")
epd.Clear()
print("done -- check the panel now")
