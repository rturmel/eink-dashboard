/*****************************************************************************
 * epd_trace.c -- instrumented init sequence for the Waveshare 10.85" (G)
 *
 * Replicates EPD_10in85g_Init() step by step, printing the BUSY pin state
 * after every stage and timing each wait, so we can see exactly where and
 * how the panel stops responding.
 *
 * Waveshare's own driver prints only "busy" / "busy release", which tells us
 * nothing about what BUSY was doing between those points. This does.
 *
 * Does not touch Waveshare's sources -- the SendCommand/SendData helpers in
 * EPD_10in85g.c are static and unreachable, so they are reimplemented here
 * identically (CS asserted and released around every byte, which is what the
 * working C driver does).
 *
 * BUILD (from the (G) demo's c/ directory):
 *
 *   gcc -O2 -o epd_trace \
 *       ~/eink-dashboard/pi_client/epd_trace.c \
 *       lib/Config/DEV_Config.c \
 *       -I lib/Config \
 *       -D USE_WIRINGPI_LIB -D RPI -lwiringPi -lm
 *
 * RUN:
 *
 *   sudo ./epd_trace
 *
 * It never blocks forever -- every wait is bounded, so it always reaches the
 * end and leaves the panel powered down rather than energised.
 *****************************************************************************/

#include "DEV_Config.h"
#include <stdio.h>
#include <time.h>

#define EPD_WIDTH  (1360 / 2)
#define EPD_HEIGHT 480

static double now_seconds(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

static void trace_busy(const char *label)
{
    printf("    [%-28s] BUSY=%d\n", label, DEV_Digital_Read(EPD_BUSY_PIN));
    fflush(stdout);
}

/* CS asserted/released around each byte -- matches EPD_10in85g.c */
static void send_command_all(UBYTE reg)
{
    DEV_Digital_Write(EPD_DC_PIN, 0);
    DEV_Digital_Write(EPD_CS_M_PIN, 0);
    DEV_Digital_Write(EPD_CS_S_PIN, 0);
    DEV_SPI_WriteByte(reg);
    DEV_Digital_Write(EPD_CS_M_PIN, 1);
    DEV_Digital_Write(EPD_CS_S_PIN, 1);
}

static void send_data_all(UBYTE data)
{
    DEV_Digital_Write(EPD_DC_PIN, 1);
    DEV_Digital_Write(EPD_CS_M_PIN, 0);
    DEV_Digital_Write(EPD_CS_S_PIN, 0);
    DEV_SPI_WriteByte(data);
    DEV_Digital_Write(EPD_CS_M_PIN, 1);
    DEV_Digital_Write(EPD_CS_S_PIN, 1);
}

/* Bounded busy wait. Returns 1 if BUSY went high, 0 on timeout.
 * Samples every 10ms and reports transitions, so a BUSY line that toggles
 * without settling is visible rather than looking like a flat hang. */
static int wait_busy(const char *label, double timeout_s)
{
    double start = now_seconds();
    int last = DEV_Digital_Read(EPD_BUSY_PIN);
    int transitions = 0;

    printf("  waiting on BUSY (%s), starts at %d, timeout %.0fs\n",
           label, last, timeout_s);
    fflush(stdout);

    while (1) {
        int v = DEV_Digital_Read(EPD_BUSY_PIN);
        if (v != last) {
            transitions++;
            printf("    t=%6.2fs  BUSY %d -> %d\n", now_seconds() - start, last, v);
            fflush(stdout);
            last = v;
        }
        if (v == 1) {
            printf("  -> released after %.2fs (%d transitions)\n",
                   now_seconds() - start, transitions);
            fflush(stdout);
            return 1;
        }
        if (now_seconds() - start > timeout_s) {
            printf("  -> TIMEOUT after %.0fs, BUSY still %d (%d transitions)\n",
                   timeout_s, v, transitions);
            fflush(stdout);
            return 0;
        }
        DEV_Delay_ms(10);
    }
}

int main(void)
{
    printf("=== epd_trace: instrumented 10.85\" (G) init ===\n");

    if (DEV_Module_Init() != 0) {
        printf("DEV_Module_Init() FAILED\n");
        return 1;
    }
    printf("DEV_Module_Init() ok\n");
    trace_busy("after module init");

    /* Let the panel's rails come up. DEV_Module_Init() raises PWR and
     * Waveshare's driver charges straight into reset; on this panel that is
     * too fast and POWER_ON never completes. */
    printf("\n-- settling 3s after PWR --\n");
    DEV_Delay_ms(3000);
    trace_busy("after 3s settle");

    printf("\n-- reset (20ms hi / 10ms lo / 20ms hi) --\n");
    DEV_Digital_Write(EPD_RST_PIN, 1);
    DEV_Delay_ms(20);
    trace_busy("RST high");
    DEV_Digital_Write(EPD_RST_PIN, 0);
    DEV_Delay_ms(10);
    trace_busy("RST low");
    DEV_Digital_Write(EPD_RST_PIN, 1);
    DEV_Delay_ms(20);
    trace_busy("RST high again");

    wait_busy("post-reset", 15.0);

    printf("\n-- register configuration --\n");

    send_command_all(0x4D);
    send_data_all(0x78);
    trace_busy("after 0x4D");

    send_command_all(0x00);
    send_data_all(0x2F);
    send_data_all(0x29);
    trace_busy("after 0x00 (panel setting)");

    for (int pass = 0; pass < 2; pass++) {
        send_command_all(0x06);
        send_data_all(0x0D); send_data_all(0x12); send_data_all(0x30);
        send_data_all(0x20); send_data_all(0x19); send_data_all(0x3D);
        send_data_all(0x0C);
    }
    trace_busy("after 0x06 x2 (booster)");

    send_command_all(0x50);
    send_data_all(0x37);
    trace_busy("after 0x50");

    send_command_all(0x61);
    send_data_all(EPD_WIDTH / 256);
    send_data_all(EPD_WIDTH % 256);
    send_data_all(EPD_HEIGHT / 256);
    send_data_all(EPD_HEIGHT % 256);
    trace_busy("after 0x61 (resolution)");

    send_command_all(0x65);
    send_data_all(0x00); send_data_all(0x00);
    send_data_all(0x00); send_data_all(0x00);
    trace_busy("after 0x65");

    send_command_all(0xE0); send_data_all(0x01);
    send_command_all(0xE3); send_data_all(0x08);
    send_command_all(0xE5); send_data_all(0x08);
    send_command_all(0xE9); send_data_all(0x01);
    trace_busy("after 0xE0/E3/E5/E9");

    printf("\n-- POWER_ON (0x04) -- this is where it normally hangs --\n");
    send_command_all(0x04);
    trace_busy("immediately after 0x04");

    int ok = wait_busy("power-on", 45.0);

    printf("\n-- result: POWER_ON %s --\n", ok ? "COMPLETED" : "DID NOT COMPLETE");

    /* Leave the panel powered down rather than energised, whatever happened. */
    printf("\n-- powering down --\n");
    send_command_all(0x02);
    send_data_all(0x00);
    DEV_Delay_ms(100);
    send_command_all(0x07);
    send_data_all(0xA5);
    DEV_Delay_ms(100);
    DEV_Module_Exit();
    printf("done\n");

    return ok ? 0 : 1;
}
