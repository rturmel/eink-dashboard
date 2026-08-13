/*****************************************************************************
 * epd_prime.c -- prime the Waveshare 10.85" (G) so a following init succeeds
 *
 * THE PROBLEM
 * -----------
 * From a cold start this panel does not complete POWER_ON (0x04). BUSY is
 * asserted and never released, and every driver hangs there forever.
 *
 * THE OBSERVED WORKAROUND
 * -----------------------
 * Running Waveshare's Python demo, interrupting it twice, and then running
 * the C demo works. The reason is what the interrupted Python demo leaves
 * behind. Its sleep() is:
 *
 *     POWER_OFF (0x02) -> delay 100ms -> DEEP_SLEEP (0x07/0xA5)
 *     -> delay 2000ms  -> module_exit()
 *
 * The second Ctrl+C lands inside that 2000ms delay, so the panel receives a
 * clean POWER_OFF and DEEP_SLEEP, but module_exit() never runs and PWR stays
 * HIGH. A subsequent init then succeeds.
 *
 * That depends on interrupting inside a 2-second window, which is why it is
 * unreliable by hand. This program performs the same sequence deterministically
 * and stops in exactly the right state -- panel deep-slept, PWR still asserted.
 *
 * Deliberately does NOT call DEV_Module_Exit(): dropping PWR is precisely what
 * destroys the primed state.
 *
 * BUILD (from the (G) demo's c/ directory):
 *
 *   gcc -O2 -o epd_prime epd_prime.c lib/Config/DEV_Config.c \
 *       -I lib/Config -D USE_WIRINGPI_LIB -D RPI -lwiringPi -lm
 *
 * RUN:
 *
 *   sudo ./epd_prime && sudo ./epd
 *****************************************************************************/

#include "DEV_Config.h"
#include <stdio.h>

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

int main(void)
{
    if (DEV_Module_Init() != 0) {
        printf("DEV_Module_Init() failed\n");
        return 1;
    }

    printf("reset...\n");
    DEV_Digital_Write(EPD_RST_PIN, 1);
    DEV_Delay_ms(20);
    DEV_Digital_Write(EPD_RST_PIN, 0);
    DEV_Delay_ms(10);
    DEV_Digital_Write(EPD_RST_PIN, 1);
    DEV_Delay_ms(20);

    printf("power off...\n");
    send_command_all(0x02);
    send_data_all(0x00);
    DEV_Delay_ms(100);

    printf("deep sleep...\n");
    send_command_all(0x07);
    send_data_all(0xA5);
    DEV_Delay_ms(100);

    /* No DEV_Module_Exit() -- PWR must stay high. That is the whole point. */
    printf("primed: deep sleep sent, PWR still asserted\n");
    printf("now run: sudo ./epd\n");
    return 0;
}
