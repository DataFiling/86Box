/* SBTEST - 32-bit PMODE/W test program: the sound setup check a game runs at
 * start. It finds the Sound Blaster from the BLASTER environment variable
 * (or the defaults), resets its DSP and reads the DSP version.
 *
 * Planted bug (see GROUND_TRUTH.md): the port in BLASTER ("A220") is read
 * as a decimal number, so A220 gives port 220 = 0DCh instead of 220h. With
 * BLASTER unset the default 220h is used and the card is found; with BLASTER
 * set (as every Sound Blaster install does) the reset goes to port 0E2h and
 * the card is "not found".
 *
 * Build (Open Watcom):
 *   wcl386 -bt=dos -l=pmodew -3r -os -fm=sbtest.map -fe=sbtest.exe sbtest.c
 */

#include <conio.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct sbconfig {
    unsigned port;
    int      irq, dma;
};

static void parse_blaster(struct sbconfig *cfg)
{
    const char *env = getenv("BLASTER");
    const char *p;

    cfg->port = 0x220;
    cfg->irq = 7;
    cfg->dma = 1;
    if (env == NULL)
        return;
    for (p = env; *p; p++) {
        switch (*p) {
        case 'A': case 'a': cfg->port = (unsigned) strtoul(p + 1, NULL, 10); break;
        case 'I': case 'i': cfg->irq = (int) strtoul(p + 1, NULL, 10); break;
        case 'D': case 'd': cfg->dma = (int) strtoul(p + 1, NULL, 10); break;
        }
        while (*p && *p != ' ')
            p++;
        if (!*p)
            break;
    }
}

static void delay_us(unsigned us)
{
    while (us--)
        inp(0x80); /* about 1 us per access on the ISA bus */
}

static int dsp_reset(unsigned port)
{
    int i;

    outp(port + 0x6, 1);
    delay_us(10);
    outp(port + 0x6, 0);
    for (i = 0; i < 1000; i++) {
        if ((inp(port + 0xE) & 0x80) && inp(port + 0xA) == 0xAA)
            return 1;
    }
    return 0;
}

static int dsp_read(unsigned port)
{
    int i;

    for (i = 0; i < 10000; i++) {
        if (inp(port + 0xE) & 0x80)
            return inp(port + 0xA);
    }
    return -1;
}

static void dsp_write(unsigned port, int v)
{
    int i;

    for (i = 0; i < 10000 && (inp(port + 0xC) & 0x80); i++)
        ;
    outp(port + 0xC, v);
}

int main(void)
{
    struct sbconfig cfg;
    int             major, minor;

    parse_blaster(&cfg);
    printf("Sound setup: BLASTER=%s\n", getenv("BLASTER") ? getenv("BLASTER") : "(not set)");
    if (!dsp_reset(cfg.port)) {
        printf("Sound Blaster not found. Check the BLASTER setting. Sound is off.\n");
        return 1;
    }
    dsp_write(cfg.port, 0xE1); /* get DSP version */
    major = dsp_read(cfg.port);
    minor = dsp_read(cfg.port);
    printf("Sound Blaster DSP %d.%02d ready (IRQ %d, DMA %d).\n", major, minor, cfg.irq, cfg.dma);
    return 0;
}
