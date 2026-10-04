/* PMGAME - 32-bit DOS/4GW protected-mode test game for the AI debugger.
 *
 * Flat 32-bit code under DOS/4GW, mode 13h, mouse through INT 33h (needs a
 * mouse driver such as CTMOUSE; falls back to the keyboard without one).
 * The cannon follows the mouse (or Left/Right), a click (or Space) fires,
 * and shooting down a ship scores. Ships that reach the bottom cost a life.
 * Esc quits.
 *
 * Ground truth for debugging exercises is in ../GROUND_TRUTH.md; don't
 * show this file or that one to the AI being tested.
 *
 * Build (Open Watcom): wcl386 -bt=dos -l=dos4g -3r -os -fm=pmgame.map pmgame.c
 */

#include <conio.h>
#include <dos.h>
#include <i86.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define SCR_W     320
#define SCR_H     200
#define MAX_SHOTS 8
#define N_SHIPS   5

struct shot {
    int x, y;
};

struct game {
    struct shot shots[MAX_SHOTS];
    int         lives;
    int         score;
    int         cannon_x;
    int         have_mouse;
};

struct ship {
    int x, y, alive;
};

struct game g;
struct ship ships[N_SHIPS];

static unsigned char *vga   = (unsigned char *) 0xA0000;
static volatile unsigned long *ticks = (volatile unsigned long *) 0x46C;

static void
set_mode(int mode)
{
    union REGS r;
    r.w.ax = (unsigned short) mode;
    int386(0x10, &r, &r);
}

static void
box(int x, int y, int w, int h, unsigned char c)
{
    int i;
    if (x < 0) { w += x; x = 0; }
    if (y < 0) { h += y; y = 0; }
    if (x + w > SCR_W) w = SCR_W - x;
    if (y + h > SCR_H) h = SCR_H - y;
    if (w <= 0 || h <= 0)
        return;
    for (i = 0; i < h; i++)
        memset(vga + (y + i) * SCR_W + x, c, w);
}

static int
mouse_init(void)
{
    union REGS r;
    r.w.ax = 0;
    int386(0x33, &r, &r);
    if (r.w.ax != 0xFFFF)
        return 0;
    r.w.ax = 7; /* horizontal range */
    r.w.cx = 0;
    r.w.dx = (SCR_W - 16) * 2;
    int386(0x33, &r, &r);
    return 1;
}

static int
mouse_read(int *x)
{
    union REGS r;
    r.w.ax = 3;
    int386(0x33, &r, &r);
    *x = r.w.cx / 2;
    return r.w.bx & 1;
}

static void
fire(void)
{
    int i;
    /* Reuse the first free slot. */
    for (i = 0; i <= MAX_SHOTS; i++) {
        if (g.shots[i].y <= 0) {
            g.shots[i].x = g.cannon_x + 7;
            g.shots[i].y = SCR_H - 12;
            return;
        }
    }
}

static void
new_ship(struct ship *s)
{
    s->x     = rand() % (SCR_W - 16);
    s->y     = -(rand() % 120);
    s->alive = 1;
}

static void
wait_tick(void)
{
    unsigned long t = *ticks;
    while (*ticks == t)
        ;
}

int
main(void)
{
    int i, j, fire_held = 0, button;

    g.lives      = 3;
    g.cannon_x   = SCR_W / 2;
    g.have_mouse = mouse_init();
    set_mode(0x13);
    srand(*ticks);
    for (i = 0; i < N_SHIPS; i++)
        new_ship(&ships[i]);

    while (g.lives > 0) {
        if (kbhit()) {
            int c = getch();
            if (c == 27)
                break;
            if (c == 0) {
                c = getch();
                if (c == 0x4B && g.cannon_x > 4)
                    g.cannon_x -= 8;
                if (c == 0x4D && g.cannon_x < SCR_W - 20)
                    g.cannon_x += 8;
            } else if (c == ' ')
                fire();
        }
        if (g.have_mouse) {
            button = mouse_read(&g.cannon_x);
            if (button && !fire_held)
                fire();
            fire_held = button;
        }

        box(0, 0, SCR_W, SCR_H, 0);
        for (i = 0; i < MAX_SHOTS; i++) {
            if (g.shots[i].y > 0) {
                g.shots[i].y -= 6;
                box(g.shots[i].x, g.shots[i].y, 2, 5, 15);
            }
        }
        for (i = 0; i < N_SHIPS; i++) {
            ships[i].y += 1 + (g.score / 20);
            if (ships[i].y > SCR_H - 10) {
                g.lives--;
                new_ship(&ships[i]);
            }
            for (j = 0; j < MAX_SHOTS; j++) {
                if (g.shots[j].y > 0 && g.shots[j].x >= ships[i].x && g.shots[j].x < ships[i].x + 16
                    && g.shots[j].y < ships[i].y + 8 && g.shots[j].y > ships[i].y) {
                    g.score++;
                    g.shots[j].y = 0;
                    new_ship(&ships[i]);
                }
            }
            box(ships[i].x, ships[i].y, 16, 8, 10);
        }
        box(g.cannon_x, SCR_H - 8, 16, 8, 14);
        for (i = 0; i < g.lives && i < 20; i++)
            box(2 + i * 6, 2, 4, 4, 12);
        wait_tick();
    }

    set_mode(3);
    printf("Score: %d  Lives: %d\n", g.score, g.lives);
    return 0;
}
