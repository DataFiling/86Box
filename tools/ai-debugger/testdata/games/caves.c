/* CAVES - real-mode test program: a cave explorer that scores each cave by
 * the size of the open area reachable from the entrance, then shows a high
 * score table with each explorer's title.
 *
 * Planted bug (see GROUND_TRUTH.md): make_title() returns a pointer to its
 * own local buffer. add_score() keeps that pointer in the table, and by the
 * time the table is printed the stack space has been reused, so the titles
 * come out as garbage.
 *
 * Build (Open Watcom):
 *   wcl -ms -bt=dos -0 -os -fm=caves.map -fe=caves.exe caves.c
 */

#include <stdio.h>
#include <string.h>

#define W 40
#define H 8

static const char *caves[3][H] = {
    { "########################################",
      "#E....##################################",
      "#.##..##################################",
      "#.##....################################",
      "#....##.################################",
      "######..################################",
      "########################################",
      "########################################" },
    { "########################################",
      "#E..........############################",
      "#.########..############################",
      "#.#......#..############################",
      "#.#.####.#....##########################",
      "#...#..#.####.##########################",
      "#####..#......##########################",
      "########################################" },
    { "########################################",
      "#E....................................##",
      "#.......####.......####........#......##",
      "#...........######.......####..#......##",
      "#..##..............#.........#........##",
      "#..##......####....#....##............##",
      "#.......................##............##",
      "########################################" },
};

struct entry {
    unsigned long score;
    const char   *title; /* "Name the Rank" */
};

char         grid[H][W + 1];
int          cells;
struct entry table[5];

static void fill(int x, int y)
{
    if (x < 0 || y < 0 || x >= W || y >= H || grid[y][x] == '#' || grid[y][x] == '*')
        return;
    grid[y][x] = '*';
    cells++;
    fill(x + 1, y);
    fill(x - 1, y);
    fill(x, y + 1);
    fill(x, y - 1);
}

static const char *rank(unsigned long score)
{
    return score >= 2000 ? "Legend" : score >= 1000 ? "Pathfinder" : score >= 300 ? "Scout" : "Rookie";
}

static char *make_title(const char *name, unsigned long score)
{
    char title[24];

    sprintf(title, "%s the %s", name, rank(score));
    return title;
}

static void add_score(const char *name, unsigned long score)
{
    int i, j;

    for (i = 0; i < 5 && table[i].score >= score; i++)
        ;
    if (i == 5)
        return;
    for (j = 4; j > i; j--)
        table[j] = table[j - 1];
    table[i].score = score;
    table[i].title = make_title(name, score);
}

int main(void)
{
    static const char *names[] = { "Anna", "Ben", "Cora" };
    int c, i;

    for (i = 0; i < 5; i++) {
        table[i].score = 250 - i * 50;
        table[i].title = "Computer";
    }
    for (c = 0; c < 3; c++) {
        for (i = 0; i < H; i++)
            strcpy(grid[i], caves[c][i]);
        cells = 0;
        fill(1, 1);
        printf("Cave %d explored by %s: %d cells, %lu points\n", c + 1, names[c], cells, (unsigned long) cells * 10);
        add_score(names[c], (unsigned long) cells * 10);
    }
    printf("\nHIGH SCORES\n");
    for (i = 0; i < 5; i++)
        printf("%d. %-24s %6lu\n", i + 1, table[i].title, table[i].score);
    return 0;
}
