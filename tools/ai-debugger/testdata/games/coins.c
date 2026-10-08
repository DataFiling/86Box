/* COINS - 32-bit DOS/4GW test program: a 10-second coin rush. A 1000 Hz
 * timer interrupt drops coins; the main loop banks the points they are
 * worth (5 each) and draws the counters straight into text-mode video
 * memory.
 *
 * Planted bug (see GROUND_TRUTH.md): the main loop banks points with
 * "score += pending; pending = 0;" while the timer interrupt adds to
 * pending. An interrupt between the read of pending and the store of 0
 * loses its 5 points, so the final score is a little less than coins * 5,
 * by a different amount on each run.
 *
 * Build (Open Watcom):
 *   wcl386 -bt=dos -l=dos4g -3r -ox -fm=coins.map -fe=coins.exe coins.c
 */

#include <conio.h>
#include <dos.h>
#include <i86.h>
#include <stdio.h>

#define PIT_HZ  1193182L
#define TICK_HZ 1000

static void(__interrupt __far *old_timer)(void);
static volatile unsigned long ticks;
static volatile unsigned long coins;
static volatile unsigned long pending;
static unsigned long          score;
static unsigned long          chain_acc;
static unsigned long          last_thousand;
static unsigned long          lives = 3;

static void __interrupt __far timer_handler(void)
{
    ticks++;
    if (ticks % 3 == 0) {
        coins++;
        pending += 5;
    }
    /* Keep the BIOS clock at 18.2 Hz: pass every 65536 PIT counts on. */
    chain_acc += PIT_HZ / TICK_HZ;
    if (chain_acc >= 65536L) {
        chain_acc -= 65536L;
        _chain_intr(old_timer); /* the BIOS handler acknowledges the IRQ */
    }
    outp(0x20, 0x20);
}

static void set_rate(unsigned divisor)
{
    _disable();
    outp(0x43, 0x36);
    outp(0x40, divisor & 0xFF);
    outp(0x40, divisor >> 8);
    _enable();
}

static void draw_number(int row, int col, unsigned long v)
{
    unsigned short *vram = (unsigned short *) 0xB8000 + row * 80 + col;
    int             i;

    for (i = 9; i >= 0; i--) {
        vram[i] = 0x0E00 | ('0' + (int) (v % 10));
        v /= 10;
    }
}

int main(void)
{
    unsigned long frame = 0;

    printf("COIN RUSH - 10 seconds, 5 points per coin\n\n");
    printf("Coins:\nScore:\nLives:\n");
    old_timer = _dos_getvect(0x08);
    _dos_setvect(0x08, timer_handler);
    set_rate((unsigned) (PIT_HZ / TICK_HZ));

    while (ticks < 10UL * TICK_HZ) {
        score += pending;
        if (score / 1000 != last_thousand) { /* a bonus life every 1000 points */
            last_thousand = score / 1000;
            lives++;
            draw_number(4, 8, lives);
        }
        pending = 0;
        draw_number(2, 8, coins);
        draw_number(3, 8, score);
        frame++;
    }

    set_rate(0);
    _dos_setvect(0x08, old_timer);
    score += pending;
    pending = 0;
    printf("\nTime! %lu coins collected, score %lu, %lu lives.\n", coins, score, lives);
    return 0;
}
