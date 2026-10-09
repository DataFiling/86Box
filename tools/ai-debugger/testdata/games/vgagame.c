/* VGAGAME - VGA mode 13h real-mode test game for the AI debugger.
 *
 * Installs its own INT 9 keyboard handler and keeps a key-state table, as
 * most action games did, so held keys keep moving the ship. Rocks fall from
 * the top; each hit costs a life. Left/Right move, Esc quits.
 *
 * Ground truth for debugging exercises is in ../GROUND_TRUTH.md; don't
 * show this file or that one to the AI being tested.
 *
 * Build (Open Watcom): wcl -ms -bt=dos -2 -os -fm=vgagame.map vgagame.c
 */

#include <conio.h>
#include <dos.h>
#include <i86.h>
#include <stdlib.h>
#include <string.h>

#define SCR_W   320
#define SCR_H   200
#define N_ROCKS 6

struct ship {
    int           x, y;
    unsigned char lives;
    unsigned char shield; /* frames of invulnerability after a hit */
    unsigned int  score;
};

struct rock {
    int x, y, speed;
};

struct ship   player = { 150, 180, 3, 0, 0 };
struct rock   rocks[N_ROCKS];
unsigned char difficulty = 3;
volatile unsigned char keys[128];

static unsigned char far *vga   = (unsigned char far *) MK_FP(0xA000, 0);
static volatile unsigned long far *ticks = (volatile unsigned long far *) MK_FP(0x0040, 0x006C);
static void(__interrupt __far *old_int9)(void);

static void __interrupt __far
kbd_handler(void)
{
    unsigned char sc = inp(0x60);
    keys[sc & 0x7F]  = (sc & 0x80) ? 0 : 1;
    outp(0x20, 0x20);
}

static void
set_mode(unsigned char mode)
{
    union REGS r;
    r.w.ax = mode;
    int86(0x10, &r, &r);
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
        _fmemset(vga + (unsigned) (y + i) * SCR_W + x, c, w);
}

static void
draw_lives(void)
{
    int i;
    box(0, 0, 60, 8, 0);
    for (i = 0; i < player.lives && i < 10; i++)
        box(2 + i * 6, 2, 4, 4, 12);
}

static void
reset_rock(struct rock *r)
{
    r->x     = rand() % (SCR_W - 12);
    r->y     = -(rand() % 100);
    r->speed = 1 + rand() % difficulty;
}

/* End screens, chosen by difficulty. */
static void
end_easy(void)
{
    box(100, 80, 120, 40, 2);
}

static void
end_normal(void)
{
    box(100, 80, 120, 40, 14);
}

static void
end_hard(void)
{
    box(100, 80, 120, 40, 4);
}

static void (*end_screens[3])(void) = { end_easy, end_normal, end_hard };

static void
game_over(void)
{
    end_screens[difficulty]();
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
    int i;

    set_mode(0x13);
    old_int9 = _dos_getvect(9);
    _dos_setvect(9, kbd_handler);
    srand((unsigned) *ticks);
    for (i = 0; i < N_ROCKS; i++)
        reset_rock(&rocks[i]);

    while (!keys[1] && player.lives) {
        box(player.x, player.y, 20, 8, 0);
        if (keys[0x4B] && player.x > 0)
            player.x -= 4;
        if (keys[0x4D] && player.x < SCR_W - 20)
            player.x += 4;

        for (i = 0; i < N_ROCKS; i++) {
            box(rocks[i].x, rocks[i].y, 12, 12, 0);
            rocks[i].y += rocks[i].speed * 2;
            if (rocks[i].y > SCR_H) {
                player.score++;
                reset_rock(&rocks[i]);
            }
            if (!player.shield && rocks[i].y + 12 > player.y && rocks[i].y < player.y + 8
                && rocks[i].x + 12 > player.x && rocks[i].x < player.x + 20) {
                player.lives--;
                player.shield = 18;
                reset_rock(&rocks[i]);
            }
            box(rocks[i].x, rocks[i].y, 12, 12, 7);
        }
        if (player.shield)
            player.shield--;
        box(player.x, player.y, 20, 8, (player.shield & 2) ? 9 : 11);
        draw_lives();
        wait_tick();
    }

    if (!player.lives) {
        game_over();
        for (i = 0; i < 36; i++)
            wait_tick();
    }

    _dos_setvect(9, old_int9);
    set_mode(0x03);
    return 0;
}
