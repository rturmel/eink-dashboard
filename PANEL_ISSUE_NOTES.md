# Waveshare 10.85" e-Paper HAT+ (G) — technical notes

## RESOLVED — Raspberry Pi OS version

**The panel does not work on Raspberry Pi OS Trixie (Debian 13). It works on Bookworm (Debian 12) with no other changes.**

On Trixie, `Init()` hangs forever waiting on BUSY after command `0x04` (POWER_ON). On Bookworm, the identical hardware initialises and refreshes correctly on the first attempt — Waveshare's C demo, their Python demo, and this project's client all work unmodified.

Everything below documents the investigation that preceded this. It is left intact because the exclusions are useful: two panels, two HAT+ boards, three Raspberry Pi models, eight driver stacks, both GPIO backends, four power supplies, and a 70-attempt statistical matrix were all eliminated before the OS was suspected.

Setup that works:

- Raspberry Pi OS **Bookworm** (Debian 12), 32-bit Lite
- Raspberry Pi 3 Model B, 5.1V regulated supply (`throttled=0x0`)
- SPI enabled, WiringPi 3.18
- Waveshare's official demo package from the product wiki (`10.85inch_e-Paper_G.zip`), not the GitHub repo

Not yet isolated: whether Bookworm alone is sufficient, or whether the official zip also differs from GitHub `master`. Both were changed at the same time. Building the GitHub sources on Bookworm would settle it.

The intermittent successes on Trixie (see Anomalies below) remain unexplained.

---

Working notes on a panel that will not complete its power-on sequence. Written for research and for asking others; no conclusions drawn about cause or fault.

---

## Hardware

| Item | Detail |
|---|---|
| Panel | Waveshare 10.85inch e-Paper HAT+ (G), 1360×480, 4-colour (black/white/red/yellow), dual-IC |
| Driver board | Waveshare 10.85inch e-Paper HAT+ (G), Interface Config switch = 0 (4-line SPI) |
| Connection | Panel FPC → interposer board → ribbon → HAT (factory assembly), and also tested panel FPC direct to HAT |
| Hosts | Raspberry Pi Zero 2 W, Raspberry Pi Zero W, Raspberry Pi 3 Model B Rev 1.2 |
| OS | Raspberry Pi OS 64-bit; Raspberry Pi OS Lite 32-bit (Trixie, kernel 6.18) |

Panel and HAT were both replaced once under RMA. Both the original and replacement sets behave the same.

---

## Symptom

`EPD_10in85g_Init()` hangs. Specifically:

1. `Reset()` completes — BUSY behaves correctly
2. The register configuration sequence is accepted
3. Command `0x04` (POWER_ON) is sent
4. BUSY asserts (goes low) within milliseconds
5. BUSY never releases

Every driver blocks forever at step 5. Nothing appears on the panel.

---

## Instrumented trace

Custom C program replicating `EPD_10in85g_Init()` exactly (CS asserted/released per byte, 20/10/20 ms reset), sampling BUSY at each stage. Built against Waveshare's `DEV_Config.c`, WiringPi backend.

```
DEV_Module_Init() ok
    [after module init           ] BUSY=0
-- settling 3s after PWR --
    [after 3s settle             ] BUSY=0
-- reset (20ms hi / 10ms lo / 20ms hi) --
    [RST high                    ] BUSY=1
    [RST low                     ] BUSY=0
    [RST high again              ] BUSY=1
  waiting on BUSY (post-reset), starts at 1
  -> released after 0.00s (0 transitions)
-- register configuration --
    [after 0x4D                  ] BUSY=1
    [after 0x00 (panel setting)  ] BUSY=1
    [after 0x06 x2 (booster)     ] BUSY=1
    [after 0x50                  ] BUSY=1
    [after 0x61 (resolution)     ] BUSY=1
    [after 0x65                  ] BUSY=1
    [after 0xE0/E3/E5/E9         ] BUSY=1
-- POWER_ON (0x04) --
    [immediately after 0x04      ] BUSY=1
  waiting on BUSY (power-on), starts at 0, timeout 45s
  -> TIMEOUT after 45s, BUSY still 0 (0 transitions)
```

Observations:

- BUSY tracks RST exactly (asserted while held in reset, released when reset lifts) — the controller responds to the reset line
- The full register sequence is accepted with BUSY idle throughout
- BUSY transitions 1 → 0 within milliseconds of `0x04` — the controller acknowledges POWER_ON
- BUSY then holds at 0 for 45 s with **zero transitions** — no oscillation, no partial recovery

---

## BUSY pin electrical characterisation

Measured at register level with `pinctrl` (Pi Zero W, BCM2835), no library in between.

```
$ pinctrl get 24
24: ip    -- | lo // GPIO24 = input
```

Forcing an internal pull-up does not change it:

```
$ pinctrl set 24 ip pu; pinctrl get 24
24: ip    -- | lo
```

Control pin (GPIO22, unconnected) follows the pulls correctly, confirming `pinctrl set` works on this board:

```
$ pinctrl set 22 ip pu; pinctrl get 22
22: ip    -- | hi
$ pinctrl set 22 ip pd; pinctrl get 22
22: ip    -- | lo
```

So BUSY is actively driven low during the hang, not floating.

Note: the `--` pull field is expected — BCM2835 pull registers are write-only and cannot be read back.

---

## HAT identification

The HAT's ID EEPROM is readable, so the Pi communicates with the board:

```
$ cat /proc/device-tree/hat/product /proc/device-tree/hat/vendor
Waveshare 10.85inch e-Paper HAT+ (G)
WAVESHARE
```

---

## Software stacks tested

All produce the same failure.

| # | Stack | Notes |
|---|---|---|
| 1 | Waveshare Python `epd10in85g.py` + `DEV_Config_*.so` | ctypes, bit-banged SPI |
| 2 | Waveshare C demo, WiringPi backend | `E-paper_Separate_Program/10.85inch_e-Paper_G` |
| 3 | Waveshare C demo, bcm2835 backend | same sources, `USELIB_RPI` switched |
| 4 | Waveshare mono demo (`EPD_10in85`) supplied by support | wrong driver for a (G) panel |
| 5 | Raw Python `spidev` + `RPi.GPIO` | init sequence reimplemented manually |
| 6 | Third-party pure-Python driver | hardware SPI on `/dev/spidev0.0` + `0.1`, kernel CS, `gpiozero` |
| 7 | Corrected Python driver (own) | CS toggled per byte, C-matching reset timing |
| 8 | ctypes wrapper around the C driver (own) | calls `EPD_10in85g_Init()` directly |

Stacks 1–5 and 7 bit-bang SPI with manual CS. Stack 6 uses kernel hardware SPI with hardware chip-select. Stack 3 vs 2 differs only in GPIO library, and therefore in bit-banged SPI clock rate.

---

## Clean-room reproduction

Fresh SD card, Raspberry Pi OS Lite 32-bit, provisioned with only `git`, `build-essential`, WiringPi, and a fresh clone of `github.com/waveshare/e-Paper`. No project code, no Python, no vendored libraries.

```
Debug: EPD_10in85g_test Demo
set wiringPi lib success !!!
Debug: e-Paper Init and Clear...
Debug: e-Paper busy
Debug: e-Paper busy release
Debug: e-Paper busy
[hangs]
```

---

## Variables tested and eliminated

- **Panel** — two units
- **Driver HAT** — two units
- **Raspberry Pi** — three boards, three SoC generations (BCM2835, BCM2710A1, BCM2837)
- **OS / architecture** — 64-bit and 32-bit
- **Driver software** — eight independent stacks (above)
- **GPIO backend / SPI clock rate** — WiringPi and bcm2835 builds of identical C sources
- **Kernel SPI interface** — `dtparam=spi=on` and disabled
- **Cabling** — factory interposer + ribbon, and panel FPC direct to HAT
- **Cable orientation and seating** — reseated and inspected repeatedly
- **Interface Config switch** — verified at 0 (4-line SPI)
- **Reset timing** — six RST low durations, 2000 µs down to 50 µs, per the wiki FAQ entry on BUSY staying asserted. BUSY reports ready in all cases.
- **Power supply** — four sources: 5V/2.5A adapter, powered USB hub, Apple 12W (5.2V/2.4A), and a 5.1V/2.5A regulated supply. The last reports `throttled=0x0` (no undervoltage or throttling since boot) and fails identically.
- **PWR settle time** — up to 5 minutes with PWR asserted before init

---

## Anomalies — intermittent successes

The panel has produced correct output on a small number of occasions. These are not reproducible on demand and are not explained by any variable above.

**Occasion 1.** Partial refresh. The panel visibly flashed through colour passes, then stopped partway, leaving a permanent faint dark band across roughly the bottom 30%. E-ink retains an image only where particles were driven, so a refresh started and did not finish.

**Occasion 2.** Full demo run. Waveshare's `EPD_10in85g_test` completed multiple refresh passes, rendering the demo's colour test pattern. Reproduced later on a clean SD card.

**Occasion 3.** Half-panel render. The demo ran and painted only the left half (master IC / CS_M, GPIO8), leaving the previous content on the right half (slave IC / CS_S, GPIO7).

In every success, the run was preceded by executing Waveshare's **Python** demo (bcm2835 backend), interrupting it twice with Ctrl+C, and then running the **C** demo. Attempts to reproduce the precondition deterministically all failed:

- Setting PWR/RST/CS_M/CS_S/DC high via `pinctrl` then running the C demo — worked once, not subsequently
- A C program issuing reset → POWER_OFF (0x02) → DEEP_SLEEP (0x07/0xA5) leaving PWR high, replicating exactly where the second Ctrl+C lands in the Python `sleep()` — did not prime
- The same plus the full register configuration and POWER_ON with a 15 s dwell — did not prime the following run

---

## Statistical test matrix

Because the fault is intermittent, single-attempt experiments are unreliable. A harness (`epd_matrix.c`) was written to run each variant repeatedly, de-energising the panel between attempts so each starts from an identical state, and to report success rates rather than anecdotes.

"Success" means POWER_ON completed — the controller asserted BUSY and then released it. A run where BUSY is already high when first sampled is counted as a failure, not a success, since the command was evidently not acted upon.

10 attempts per variant, 20s timeout, Raspberry Pi 3B on a 5.1V supply (`throttled=0x0`), WiringPi backend:

| Variant | Description | Result | BUSY behaviour |
|---|---|---|---|
| `both` | vendor sequence, both controllers | 0/10 | asserted, never released |
| `m` | CS_M only (master IC) | 0/10 | asserted, never released |
| `s` | CS_S only (slave IC) | 0/10 | **never asserted** |
| `nopoll` | fixed 15s delay, no BUSY polling | 0/10 | still low at 15s |
| `retry` | POWER_ON sent 3×, 5s apart | 0/10 | asserted, never released |
| `offon` | POWER_ON → 5s → POWER_OFF → 2s → POWER_ON | 0/10 | asserted, never released |
| `cfg2` | configuration block sent twice | 0/10 | asserted, never released |

The identical baseline run built against **bcm2835** instead of WiringPi also gives 0/10 with zero BUSY transitions, eliminating the GPIO backend and bit-banged SPI clock rate.

### Master / slave asymmetry

The one difference in the matrix: addressed to **CS_M**, the controller acknowledges POWER_ON by asserting BUSY (then never releases it). Addressed to **CS_S**, BUSY is never asserted at all — no response within 3s.

This is consistent with the half-panel render, where only the master half updated.

Caveat: on a dual-IC panel the BUSY line may be driven by the master controller alone, in which case a CS_S-only test would show no BUSY response regardless of slave health. Waveshare's pin table lists a single BUSY pin without specifying which IC drives it.

---

## Unresolved contradiction

Two runs of the same code path on the same hardware, minutes apart, disagree about whether POWER_ON completes:

| Program | Method after `0x04` | Result |
|---|---|---|
| `epd_trace` | poll BUSY every 10 ms for 45 s | BUSY flat at 0, zero transitions |
| `epd_prime2` | fixed 15 s delay, no polling, then read once | **BUSY = 1** (POWER_ON completed) |

Both WiringPi, both same register sequence, both cold start. A GPIO read should not affect whether the controller completes its power-up.

In the run where POWER_ON completed, sending `0x12` (refresh) afterwards did not start a refresh — BUSY never went low within 3 s, and the panel did not change.

---

## Driver discrepancy (may or may not be relevant)

Waveshare's Python and C drivers for this panel are not equivalent implementations.

The Python file's own header identifies it as a different panel's driver:

```python
# * | File        :	  epd12in48.py
```

Differences from `EPD_10in85g.c`:

| | C driver | Python driver |
|---|---|---|
| Chip select | asserted/released around **every byte** | `CS_ALL(0)` once, held low across the entire init sequence |
| Reset timing | 20 ms high / 10 ms low / 20 ms high | 200 / 2 / 200 ms |

A corrected Python driver matching the C behaviour on both points was written and tested; it does not resolve the hang.

---

## Environment

- `dtparam=spi=on`; `/dev/spidev0.0` and `/dev/spidev0.1` both present
- WiringPi 3.18 (maintained fork, github.com/WiringPi/WiringPi)
- bcm2835 library 1.71
- `DEV_Config.h` for this panel: `Hardware_SPI 0`, `SPI_line 1` — SPI is bit-banged in software over GPIO10 (MOSI) and GPIO11 (SCLK); `/dev/spidev` is not used by the driver
- Pin mapping: RST 17, DC 25, CS_M 8, CS_S 7, BUSY 24, PWR 18, MOSI 10, SCLK 11

---

## Code written during investigation

In `pi_client/` of this repository:

- `epd_trace.c` — instrumented init with per-stage BUSY sampling and bounded waits
- `epd_prime.c` — deterministic POWER_OFF/DEEP_SLEEP leaving PWR asserted
- `reset_panel.py` — recover the panel from a hung refresh, no busy waits so it cannot itself hang
- `epd10in85g_fixed.py` — Python driver corrected to match the C driver's CS handling and reset timing
- `epd10in85g_clib.py` — ctypes wrapper calling Waveshare's C driver directly
- `epd_matrix.c` — statistical harness; runs a named variant N times with a clean reset between attempts and reports success rates
