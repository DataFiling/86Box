/* TOUR - 32-bit DOS/32A test program: a demo that tours the game's levels
 * one after another, loading each level's map and sprite set.
 *
 * Planted bug (see GROUND_TRUTH.md): load_sprites() allocates a new sprite
 * cache for every level and overwrites the pointer to the old one without
 * freeing it (the map is freed correctly). The demo runs out of memory after
 * a few dozen levels.
 *
 * Build (Open Watcom):
 *   wcl386 -bt=dos -l=dos32a -3r -ox -fm=tour.map -fe=tour.exe tour.c
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAP_BYTES    (128L * 1024)
#define SPRITE_BYTES (192L * 1024)

struct level {
    int            number;
    unsigned char *map;
    unsigned long  checksum;
};

static unsigned char *sprite_cache;
static unsigned long  sprites_loaded;

static int load_sprites(int level)
{
    unsigned char *cache = malloc(SPRITE_BYTES);
    long           i;

    if (cache == NULL)
        return 0;
    for (i = 0; i < SPRITE_BYTES; i += 64)
        cache[i] = (unsigned char) (level + i);
    sprite_cache = cache; /* the previous level's cache is never freed */
    sprites_loaded++;
    return 1;
}

static int load_level(struct level *lv, int number)
{
    long i;

    lv->number = number;
    lv->map = malloc(MAP_BYTES);
    if (lv->map == NULL)
        return 0;
    for (i = 0; i < MAP_BYTES; i++)
        lv->map[i] = (unsigned char) ((i * 7 + number) & 0x3F);
    if (!load_sprites(number)) {
        free(lv->map);
        lv->map = NULL;
        return 0;
    }
    return 1;
}

static void play_level(struct level *lv)
{
    unsigned long sum = 0;
    long          i;

    for (i = 0; i < MAP_BYTES; i += 16)
        sum += lv->map[i] + sprite_cache[(i * 3) % SPRITE_BYTES];
    lv->checksum = sum;
}

static void unload_level(struct level *lv)
{
    free(lv->map);
    lv->map = NULL;
}

int main(void)
{
    struct level lv;
    int          n;

    printf("TOUR - visiting every level\n");
    for (n = 1; n <= 200; n++) {
        if (!load_level(&lv, n)) {
            printf("\nNot enough memory for level %d\n", n);
            return 1;
        }
        play_level(&lv);
        printf("Level %3d ok (%08lX)\r", n, lv.checksum);
        unload_level(&lv);
    }
    printf("\nAll levels visited.\n");
    return 0;
}
