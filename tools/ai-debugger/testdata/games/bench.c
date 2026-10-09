/* BENCH - fixed CPU, memory and VGA workload for comparing emulation
 * speed, timed by the BIOS tick counter (18.2 Hz).
 *
 * Runs each of four kernels for about 3 seconds of emulated time and prints
 * how many iterations completed per second:
 *   int   - integer arithmetic and branches (a small LCG/hash loop)
 *   fpu   - x87 floating point (a polynomial and divides)
 *   mem   - 64 KiB memcpy/memset round trips in extended memory
 *   vga   - full mode 13h frame fills (64000 bytes per frame) to A0000
 * The score line is easy to grep: "BENCH int=... fpu=... mem=... vga=...".
 *
 * Build (Open Watcom): wcl386 -bt=dos -l=dos4g -3r -fp3 -ox bench.c
 * (386 code and a 387 FPU, so the same binary runs on every test machine.)
 */

#include <dos.h>
#include <i86.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define SECONDS 3
#define TICKS   (SECONDS * 182 / 10)

static volatile unsigned long *ticks = (volatile unsigned long *) 0x46C;
static volatile unsigned       sink;

static unsigned long
run(void (*kernel)(void))
{
    unsigned long t0, n = 0;

    /* Start on a tick edge. */
    t0 = *ticks;
    while (*ticks == t0)
        ;
    t0 = *ticks;
    while (*ticks - t0 < TICKS) {
        kernel();
        n++;
    }
    return n / SECONDS;
}

static void
k_int(void)
{
    unsigned x = sink, i;
    for (i = 0; i < 1000; i++) {
        x = x * 1103515245u + 12345u;
        if (x & 0x100)
            x ^= x >> 7;
        else
            x += i;
    }
    sink = x;
}

static void
k_fpu(void)
{
    double a = 1.0001, s = 0.0;
    int    i;
    for (i = 0; i < 200; i++) {
        s += ((a * 0.5 + 1.25) * a - 3.75) / (a + 2.0);
        a *= 1.0003;
    }
    sink = (unsigned) s;
}

static unsigned char *buf_a, *buf_b;

static void
k_mem(void)
{
    memcpy(buf_b, buf_a, 65536);
    memset(buf_a, buf_b[sink & 0xFFFF], 65536);
}

static void
k_vga(void)
{
    static unsigned char c;
    memset((void *) 0xA0000, c++, 64000);
}

int
main(void)
{
    union REGS    r;
    unsigned long n_int, n_fpu, n_mem, n_vga;

    buf_a = malloc(65536);
    buf_b = malloc(65536);
    if (!buf_a || !buf_b) {
        printf("BENCH: out of memory\n");
        return 1;
    }
    memset(buf_a, 1, 65536);

    printf("BENCH: %d s per kernel...\n", SECONDS);
    n_int = run(k_int);
    n_fpu = run(k_fpu);
    n_mem = run(k_mem);
    r.w.ax = 0x13;
    int386(0x10, &r, &r);
    n_vga = run(k_vga);
    r.w.ax = 0x03;
    int386(0x10, &r, &r);

    printf("BENCH int=%lu fpu=%lu mem=%lu vga=%lu\n", n_int, n_fpu, n_mem, n_vga);
    return 0;
}
