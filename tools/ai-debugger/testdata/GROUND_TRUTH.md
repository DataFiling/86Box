# Ground truth for the debugging exercises

**Do not show this file or `games/` to an AI being tested.** It is for
checking the AI's conclusions afterwards.

Offsets below come from the Open Watcom linker maps of the binaries on the
test floppy (built from `games/` with the commands in each source file's
header). Real-mode programs are loaded at a segment that depends on what is
resident (CTMOUSE, FreeCOM), so their variables are given relative to
DGROUP, the data segment in DS while the game runs.

## TXTGAME.EXE (16-bit real mode, text mode)

| Variable | Where | Type |
|---|---|---|
| lives | DS:005A | byte, starts at 3 |
| score | DS:005B | word, +10 per gem |
| level | DS:005D | byte |
| player | DS:00B8 | {x, y} bytes |

**Planted bug: hang after the 5th gem.** Every 50 points `flash_border()`
waits for vertical retrace by polling port **3DBh** instead of the CGA/VGA
input status register **3DAh**. On the emulated VGA, 3DBh reads as FFh, so
the second loop (`while (inp(0x3DB) & 0x08)`) never ends. An AI should find
the CPU spinning in a short `in al,dx / test al,8 / jnz` loop with DX=3DBh,
and that 3DAh is the right port.

## VGAGAME.EXE (16-bit real mode, mode 13h, own INT 9 handler)

| Variable | Where | Type |
|---|---|---|
| player | DS:0022 | {int x, int y, byte lives, byte shield, word score} |
| player.lives | DS:0026 | byte, starts at 3 |
| difficulty | DS:002A | byte, 3 |
| keys[128] | DS:0078 | key-state table written by the INT 9 handler |

**Planted bug: crash at game over.** When lives reach 0, `game_over()` calls
`end_screens[difficulty]()`, but the table has 3 entries (0-2) and
`difficulty` is 3, so it calls through the word after the table, a junk
near pointer. An AI should trace the indirect `call word ptr [...]` in
game_over with index 3 and spot the out-of-range index (fix: index
`difficulty - 1`, or a 4-entry table).

## PMGAME.EXE (32-bit flat, DOS/4GW, mode 13h, INT 33h mouse)

| Variable | Where (linear = DGROUP offset; DOS/4GW flat model, base 0) | Type |
|---|---|---|
| g | object 2 offset 0424h | struct game |
| g.shots[8] | g+00h .. g+3Fh | {int x, int y} x 8 |
| g.lives | g+40h | int, starts at 3 |
| g.score | g+44h | int |
| g.cannon_x | g+48h | int |
| ships[5] | object 2 offset 0474h | {x, y, alive} |

Object 2's runtime base depends on where DOS/4GW loads the program; find
`g` by searching for the lives value or through the code.

**Planted bug: lives jump to a large number after rapid fire.** `fire()`
scans `for (i = 0; i <= MAX_SHOTS; i++)`, one past the 8-entry array. With
all 8 shots in flight, a 9th shot uses `shots[8]`, which overlays `lives`
(x) and `score` (y): lives become `cannon_x + 7` and score 188. It only
triggers while score is 0 (the slot's "free" test reads score as y <= 0). A
write watchpoint on `g.lives` catches the store in `fire()`; the fix is
`i < MAX_SHOTS`.

## MOUSETST.EXE and BENCH.EXE

No planted bugs. MOUSETST prints the INT 33h position, buttons and press
counts. BENCH prints `BENCH int=... fpu=... mem=... vga=...`, iterations per
emulated second.
