/* SHIPS - real-mode test program: a short space battle. The ship flies
 * through an asteroid field turn by turn until it reaches the station or its
 * shield fails.
 *
 * Planted bug (see GROUND_TRUTH.md): new_ship() never sets the shield; it
 * only "defaults" it when the field reads 0, as if malloc() returned zeroed
 * memory. After a fresh boot the heap memory is zero and the game works.
 * Run it again and malloc() returns the same memory, still holding the
 * negative shield the last game ended with, so the ship is destroyed at once.
 *
 * Build (Open Watcom):
 *   wcl -ms -bt=dos -0 -os -fm=ships.map -fe=ships.exe ships.c
 */

#include <stdio.h>
#include <stdlib.h>

struct ship {
    int  x, y;
    int  fuel;
    int  shield;
    char name[10];
};

static struct ship *new_ship(const char *name)
{
    struct ship *s = malloc(sizeof(struct ship));
    int          i;

    if (s == NULL)
        return NULL;
    s->x = 0;
    s->y = 5;
    s->fuel = 400;
    for (i = 0; i < 9 && name[i]; i++)
        s->name[i] = name[i];
    s->name[i] = 0;
    if (s->shield == 0)
        s->shield = 100; /* default shield strength */
    return s;
}

static const unsigned char rocks[] = { 3, 7, 1, 5, 2, 8, 4, 6, 0, 9, 3, 2, 7, 1, 5, 6, 8, 2, 4, 9 };

int main(void)
{
    struct ship *s = new_ship("Nomad");
    int          turn;

    if (s == NULL) {
        printf("Out of memory\n");
        return 1;
    }
    printf("%s launches with shield %d and fuel %d\n", s->name, s->shield, s->fuel);
    for (turn = 1; turn <= 20; turn++) {
        if (s->shield <= 0) {
            printf("Turn %d: %s is destroyed!\n", turn, s->name);
            break;
        }
        s->x += 2;
        s->fuel -= 20;
        if (rocks[turn - 1] > 4) {
            s->shield -= 9;
            printf("Turn %d: asteroid hit at %d,%d - shield %d\n", turn, s->x, s->y, s->shield);
        }
    }
    if (s->shield > 0)
        printf("%s reaches the station with shield %d.\n", s->name, s->shield);
    free(s);
    return 0;
}
