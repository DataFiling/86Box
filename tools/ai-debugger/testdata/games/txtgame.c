/* TXTGAME - text-mode real-mode test game for the AI debugger.
 *
 * 80x25 text mode, written straight to B800:0000; BIOS keyboard (INT 16h);
 * paced by the BIOS tick count. Arrow keys move '@', '*' gems give 10
 * points, touching an 'E' costs a life, Esc quits.
 *
 * Ground truth for debugging exercises is in ../GROUND_TRUTH.md; don't
 * show this file or that one to the AI being tested.
 *
 * Build (Open Watcom): wcl -ms -bt=dos -0 -os -fm=txtgame.map txtgame.c
 */

#include <bios.h>
#include <conio.h>
#include <dos.h>
#include <i86.h>
#include <stdlib.h>

#define W        80
#define H        24 /* row 24 is the status line */
#define N_ENEMY  4
#define N_GEM    6

typedef struct {
    unsigned char x, y;
} pos_t;

/* Game state, kept together so it is easy to find in memory. */
unsigned char lives = 3;
unsigned int  score = 0;
unsigned char level = 1;
pos_t         player;
pos_t         enemy[N_ENEMY];
pos_t         gem[N_GEM];

static unsigned short far *screen = (unsigned short far *) MK_FP(0xB800, 0);
static volatile unsigned long far *ticks = (volatile unsigned long far *) MK_FP(0x0040, 0x006C);

static void
put(int x, int y, char c, unsigned char attr)
{
    screen[y * W + x] = (unsigned short) (attr << 8) | (unsigned char) c;
}

static void
puts_at(int x, int y, const char *s, unsigned char attr)
{
    while (*s)
        put(x++, y, *s++, attr);
}

static void
put_num(int x, int y, unsigned int n, unsigned char attr)
{
    char buf[6];
    int  i = 5;
    buf[i] = 0;
    do {
        buf[--i] = (char) ('0' + n % 10);
        n /= 10;
    } while (n && i);
    puts_at(x, y, &buf[i], attr);
}

static void
draw_status(void)
{
    int x;
    for (x = 0; x < W; x++)
        put(x, H, ' ', 0x1F);
    puts_at(1, H, "LIVES:", 0x1F);
    put_num(8, H, lives, 0x1E);
    puts_at(12, H, "SCORE:", 0x1F);
    put_num(19, H, score, 0x1E);
    puts_at(26, H, "LEVEL:", 0x1F);
    put_num(33, H, level, 0x1E);
    puts_at(50, H, "Arrows move, Esc quits", 0x17);
}

static void
place(pos_t *p)
{
    p->x = (unsigned char) (1 + rand() % (W - 2));
    p->y = (unsigned char) (1 + rand() % (H - 2));
}

/* Flash the screen border when a level is completed. Waits for the start
   of vertical retrace so the colour change doesn't tear. */
static void
flash_border(void)
{
    int i;
    for (i = 0; i < 6; i++) {
        while (!(inp(0x3DB) & 0x08)) /* status register */
            ;
        while (inp(0x3DB) & 0x08)
            ;
        outp(0x3D9, (i & 1) ? 0x0E : 0x00);
    }
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
    union REGS r;
    int        i, done = 0;
    unsigned   key;

    r.w.ax = 0x0003; /* 80x25 colour text */
    int86(0x10, &r, &r);
    r.h.ah = 0x01; /* hide the cursor */
    r.w.cx = 0x2000;
    int86(0x10, &r, &r);

    srand((unsigned) *ticks);
    player.x = W / 2;
    player.y = H / 2;
    for (i = 0; i < N_ENEMY; i++)
        place(&enemy[i]);
    for (i = 0; i < N_GEM; i++)
        place(&gem[i]);

    while (!done && lives) {
        /* input */
        while (_bios_keybrd(_KEYBRD_READY)) {
            key = _bios_keybrd(_KEYBRD_READ);
            switch (key >> 8) {
                case 0x48: if (player.y > 1)     player.y--; break;
                case 0x50: if (player.y < H - 2) player.y++; break;
                case 0x4B: if (player.x > 1)     player.x--; break;
                case 0x4D: if (player.x < W - 2) player.x++; break;
                case 0x01: done = 1; break;
            }
        }

        /* enemies drift towards the player every other tick */
        if (*ticks & 1) {
            for (i = 0; i < N_ENEMY; i++) {
                if (rand() & 3)
                    continue;
                if (enemy[i].x < player.x) enemy[i].x++;
                else if (enemy[i].x > player.x) enemy[i].x--;
                if (enemy[i].y < player.y) enemy[i].y++;
                else if (enemy[i].y > player.y) enemy[i].y--;
            }
        }

        /* collisions */
        for (i = 0; i < N_GEM; i++) {
            if (gem[i].x == player.x && gem[i].y == player.y) {
                score += 10;
                place(&gem[i]);
                if (score % 50 == 0) {
                    level++;
                    flash_border();
                }
            }
        }
        for (i = 0; i < N_ENEMY; i++) {
            if (enemy[i].x == player.x && enemy[i].y == player.y) {
                lives--;
                place(&enemy[i]);
                player.x = W / 2;
                player.y = H / 2;
            }
        }

        /* draw */
        for (i = 0; i < W * H; i++)
            screen[i] = 0x0720;
        for (i = 0; i < W; i++) {
            put(i, 0, '\xCD', 0x09);
            put(i, H - 1, '\xCD', 0x09);
        }
        for (i = 0; i < N_GEM; i++)
            put(gem[i].x, gem[i].y, '*', 0x0E);
        for (i = 0; i < N_ENEMY; i++)
            put(enemy[i].x, enemy[i].y, 'E', 0x0C);
        put(player.x, player.y, '@', 0x0F);
        draw_status();

        wait_tick();
    }

    r.w.ax = 0x0003;
    int86(0x10, &r, &r);
    if (!lives)
        cputs("GAME OVER\r\n");
    return 0;
}
