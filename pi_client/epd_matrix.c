/*****************************************************************************
 * epd_matrix.c -- statistical test harness for the 10.85" (G) POWER_ON failure
 *
 * WHY THIS EXISTS
 * ---------------
 * This panel fails intermittently. Every experiment run once against it has
 * produced a result that the next run contradicted. Judging a variant on a
 * single attempt is worthless here.
 *
 * This runs a named variant N times, resetting the panel between attempts,
 * and reports how often POWER_ON actually completed. A variant that succeeds
 * 18/50 where the baseline succeeds 0/50 is a real signal; a variant that
 * succeeds once is not.
 *
 * VARIANTS
 * --------
 *   both     vendor sequence, commands broadcast to both controllers
 *   m        identical, but CS_M only (master IC / left half)
 *   s        identical, but CS_S only (slave IC / right half)
 *   nopoll   vendor sequence, fixed delay after POWER_ON instead of polling
 *   retry    POWER_ON sent three times, 5s apart
 *   offon    POWER_ON -> 5s -> POWER_OFF -> 2s -> POWER_ON
 *   cfg2     configuration block sent twice before POWER_ON
 *   all      run every variant in turn, print combined matrix
 *
 * BUILD (from the (G) demo's c/ directory):
 *
 *   gcc -O2 -o epd_matrix epd_matrix.c lib/Config/DEV_Config.c \
 *       -I lib/Config -D USE_WIRINGPI_LIB -D RPI -lwiringPi -lm
 *
 * RUN:
 *
 *   sudo ./epd_matrix both 20
 *   sudo ./epd_matrix all 10
 *
 * Each attempt costs up to <timeout> seconds on failure, so 20 attempts at
 * the default 20s timeout is about 7 minutes worst case per variant.
 *
 * Between attempts the panel gets POWER_OFF + DEEP_SLEEP and PWR is dropped,
 * so each attempt starts from the same state rather than inheriting the last.
 *****************************************************************************/

#include "DEV_Config.h"
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <time.h>

#define W (1360 / 2)
#define H 480

static int timeout_s = 20;

/* ------------------------------------------------------------------ */
/* chip-select targeted command/data -- CS toggled per byte, as the    */
/* vendor C driver does                                               */
/* ------------------------------------------------------------------ */

typedef enum { CS_BOTH, CS_M_ONLY, CS_S_ONLY } cs_mode_t;
static cs_mode_t cs_mode = CS_BOTH;

static void cs_assert(void)
{
    if (cs_mode == CS_BOTH || cs_mode == CS_M_ONLY)
        DEV_Digital_Write(EPD_CS_M_PIN, 0);
    if (cs_mode == CS_BOTH || cs_mode == CS_S_ONLY)
        DEV_Digital_Write(EPD_CS_S_PIN, 0);
}

static void cs_release(void)
{
    DEV_Digital_Write(EPD_CS_M_PIN, 1);
    DEV_Digital_Write(EPD_CS_S_PIN, 1);
}

static void CMD(UBYTE reg)
{
    DEV_Digital_Write(EPD_DC_PIN, 0);
    cs_assert();
    DEV_SPI_WriteByte(reg);
    cs_release();
}

static void DAT(UBYTE d)
{
    DEV_Digital_Write(EPD_DC_PIN, 1);
    cs_assert();
    DEV_SPI_WriteByte(d);
    cs_release();
}

/* ------------------------------------------------------------------ */

static double now_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

static void panel_reset(void)
{
    DEV_Digital_Write(EPD_RST_PIN, 1);
    DEV_Delay_ms(20);
    DEV_Digital_Write(EPD_RST_PIN, 0);
    DEV_Delay_ms(10);
    DEV_Digital_Write(EPD_RST_PIN, 1);
    DEV_Delay_ms(20);
}

static void send_config(void)
{
    CMD(0x4D); DAT(0x78);
    CMD(0x00); DAT(0x2F); DAT(0x29);
    for (int p = 0; p < 2; p++) {
        CMD(0x06);
        DAT(0x0D); DAT(0x12); DAT(0x30); DAT(0x20);
        DAT(0x19); DAT(0x3D); DAT(0x0C);
    }
    CMD(0x50); DAT(0x37);
    CMD(0x61); DAT(W / 256); DAT(W % 256); DAT(H / 256); DAT(H % 256);
    CMD(0x65); DAT(0x00); DAT(0x00); DAT(0x00); DAT(0x00);
    CMD(0xE0); DAT(0x01);
    CMD(0xE3); DAT(0x08);
    CMD(0xE5); DAT(0x08);
    CMD(0xE9); DAT(0x01);
}

/* Poll BUSY until it releases. Returns 1 on release, 0 on timeout.
 * transitions_out receives the number of BUSY edges seen, which
 * distinguishes "never moved" from "tried and gave up". */
static int poll_busy(double timeout, int *transitions_out)
{
    double start = now_s();
    int last = DEV_Digital_Read(EPD_BUSY_PIN);
    int transitions = 0;

    while (now_s() - start < timeout) {
        int v = DEV_Digital_Read(EPD_BUSY_PIN);
        if (v != last) { transitions++; last = v; }
        if (v == 1) {
            if (transitions_out) *transitions_out = transitions;
            return 1;
        }
        DEV_Delay_ms(10);
    }
    if (transitions_out) *transitions_out = transitions;
    return 0;
}

/* Leave the panel down and de-energised so the next attempt starts clean. */
static void panel_shutdown(void)
{
    CMD(0x02); DAT(0x00);
    DEV_Delay_ms(100);
    CMD(0x07); DAT(0xA5);
    DEV_Delay_ms(100);
    DEV_Digital_Write(EPD_PWR_PIN, 0);
    DEV_Delay_ms(1000);
    DEV_Digital_Write(EPD_PWR_PIN, 1);
    DEV_Delay_ms(500);
}

/* ------------------------------------------------------------------ */
/* one attempt of a given variant; returns 1 if POWER_ON completed     */
/* ------------------------------------------------------------------ */

static int attempt(const char *variant, int *transitions_out)
{
    cs_mode = CS_BOTH;
    if (strcmp(variant, "m") == 0) cs_mode = CS_M_ONLY;
    if (strcmp(variant, "s") == 0) cs_mode = CS_S_ONLY;

    panel_reset();
    if (!poll_busy(5.0, NULL)) {
        /* reset handshake itself failed -- unusual, report as failure */
        return 0;
    }

    send_config();
    if (strcmp(variant, "cfg2") == 0)
        send_config();

    if (strcmp(variant, "nopoll") == 0) {
        CMD(0x04);
        DEV_Delay_ms(15000);
        int v = DEV_Digital_Read(EPD_BUSY_PIN);
        if (transitions_out) *transitions_out = -1;   /* not measured */
        return v == 1;
    }

    if (strcmp(variant, "retry") == 0) {
        for (int i = 0; i < 3; i++) {
            CMD(0x04);
            if (poll_busy(5.0, transitions_out)) return 1;
        }
        return 0;
    }

    if (strcmp(variant, "offon") == 0) {
        CMD(0x04);
        DEV_Delay_ms(5000);
        CMD(0x02); DAT(0x00);
        DEV_Delay_ms(2000);
        CMD(0x04);
        return poll_busy(timeout_s, transitions_out);
    }

    /* both / m / s: vendor sequence */
    CMD(0x04);
    return poll_busy(timeout_s, transitions_out);
}

static void run_variant(const char *variant, int n)
{
    int ok = 0, total_transitions = 0, measured = 0;

    printf("\n=== variant '%s', %d attempts ===\n", variant, n);
    fflush(stdout);

    for (int i = 0; i < n; i++) {
        int tr = 0;
        double t0 = now_s();
        int r = attempt(variant, &tr);
        double dt = now_s() - t0;

        if (r) ok++;
        if (tr >= 0) { total_transitions += tr; measured++; }

        printf("  attempt %2d/%d: %-4s  %5.1fs", i + 1, n, r ? "OK" : "FAIL", dt);
        if (tr >= 0) printf("  busy_transitions=%d", tr);
        printf("\n");
        fflush(stdout);

        panel_shutdown();
    }

    printf("  --> %s: %d/%d succeeded (%.0f%%)", variant, ok, n,
           100.0 * ok / (n ? n : 1));
    if (measured)
        printf(", mean BUSY transitions %.1f", (double)total_transitions / measured);
    printf("\n");
    fflush(stdout);
}

int main(int argc, char **argv)
{
    const char *variant = (argc > 1) ? argv[1] : "both";
    int n = (argc > 2) ? atoi(argv[2]) : 10;
    if (argc > 3) timeout_s = atoi(argv[3]);

    if (DEV_Module_Init() != 0) {
        printf("DEV_Module_Init() failed\n");
        return 1;
    }
    printf("timeout per POWER_ON: %ds\n", timeout_s);

    if (strcmp(variant, "all") == 0) {
        const char *all[] = { "both", "m", "s", "nopoll", "retry", "offon", "cfg2" };
        for (unsigned i = 0; i < sizeof(all) / sizeof(all[0]); i++)
            run_variant(all[i], n);
    } else {
        run_variant(variant, n);
    }

    /* leave the panel asleep and unpowered */
    CMD(0x02); DAT(0x00); DEV_Delay_ms(100);
    CMD(0x07); DAT(0xA5); DEV_Delay_ms(100);
    DEV_Module_Exit();
    printf("\ndone\n");
    return 0;
}
