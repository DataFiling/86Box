/* DATAGAME - real-mode test program for the AI debugger's DOS awareness.
 *
 * Loads its level table from LEVELS.DAT (on the test floppy) and lists the
 * levels. It reports "level data corrupt" although the file is fine.
 *
 * Ground truth for debugging exercises is in ../GROUND_TRUTH.md; don't
 * show this file or that one to the AI being tested.
 *
 * Build (Open Watcom): wcl -ms -bt=dos -0 -os -fm=datagame.map datagame.c
 */

#include <fcntl.h>
#include <io.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_LEVELS 8

enum { ACCESS_NONE, ACCESS_READ, ACCESS_WRITE, ACCESS_UPDATE };

struct level {
    char name[12];
    unsigned short enemies;
    unsigned short time;
};

struct level levels[MAX_LEVELS];
int          level_count;

static const int open_modes[] = { O_RDONLY, O_WRONLY, O_RDWR };

/* Open a data file for the given kind of access. */
static int
open_data(const char *name, int access)
{
    return open(name, open_modes[access] | O_BINARY);
}

static int
load_levels(void)
{
    char          magic[4];
    unsigned char count;
    int           fd = open_data("LEVELS.DAT", ACCESS_READ);

    if (fd < 0)
        return 1;
    if (read(fd, magic, 4) != 4 || memcmp(magic, "LVL1", 4)) {
        close(fd);
        return 3;
    }
    if (read(fd, &count, 1) != 1 || count == 0 || count > MAX_LEVELS) {
        close(fd);
        return 4;
    }
    if (read(fd, levels, count * sizeof(struct level)) != (int) (count * sizeof(struct level))) {
        close(fd);
        return 5;
    }
    close(fd);
    level_count = count;
    return 0;
}

int
main(void)
{
    int i, err;

    printf("DATAGAME 1.0\n");
    err = load_levels();
    if (err) {
        printf("Error: level data corrupt (code %d). Reinstall the game.\n", err);
        return err;
    }
    for (i = 0; i < level_count; i++)
        printf("Level %d: %-12.12s enemies %u, time %u s\n", i + 1, levels[i].name, levels[i].enemies, levels[i].time);
    return 0;
}
