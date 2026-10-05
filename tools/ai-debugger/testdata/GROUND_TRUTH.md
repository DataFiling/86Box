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

## DATAGAME.EXE (16-bit real mode, DOS file I/O)

| Variable | Where | Type |
|---|---|---|
| levels[8] | DS:02E0 | {char name[12]; u16 enemies, time} |
| level_count | DS:0360 | int, stays 0 |

DGROUP is at load segment + 026Bh.

**Planted bug: "level data corrupt (code 3)" although LEVELS.DAT is fine.**
`open_data()` looks up the open mode in `open_modes[] = { O_RDONLY, O_WRONLY,
O_RDWR }` with an access enum whose read value is 1 (`ACCESS_NONE` is 0), so
LEVELS.DAT is opened **write-only**: INT 21h AX=3D01h (mode 01h). Open
Watcom's `read()` refuses to read a write-only handle itself (no INT 21h
AH=3Fh is made), the magic check fails, and `load_levels()` returns 3. An AI
should find, with the INT 21h log, the open with mode 01h followed by a
close and no read, and conclude the file is opened for writing. Fix: open it
read-only (mode 0), e.g. index the table with `access - 1` or start the enum
at `ACCESS_READ = 0`. A binary patch that makes the open use mode 0 is also
correct. The file itself (69 bytes, "LVL1", 4 levels) is fine.

## MOUSETST.EXE and BENCH.EXE

No planted bugs. MOUSETST prints the INT 33h position, buttons and press
counts. BENCH prints `BENCH int=... fpu=... mem=... vga=...`, iterations per
emulated second.

## FastDoom, unpatched (real game, DOS/4GW, built with FASTDOOM_PATCH=0)

**Symptom:** the game hangs near the end of startup, after it switches to
mode 13h (black screen), always at about the same point.

**Cause:** FastDoom's timer interrupt handler `TS_ServiceSchedule` (in
`FASTDOOM/ns_task.c`, a Watcom `__interrupt` function) saves the interrupted
stack, switches SS:ESP to its own zero-based stack, and keeps addressing its
locals `ptr`/`next` through EBP. When IRQ0 arrives while the CPU is in real
mode (inside DOS, e.g. reading the WAD), DOS/4GW runs the handler on its own
interrupt stack, whose selector has a non-zero base (143DF0h in our runs),
so after the switch `[ebp-8]`/`[ebp-4]` address low linear memory: FreeDOS's
disk buffers at 2A00h-5200h. The handler writes the address of `HeadTask`
(the task list head, data offset 10DA0h, linear 20FDA0h) twice there.
Depending on what it hits, FreeDOS's buffer list becomes a loop without its
start or a cached FAT sector (holding the WAD's cluster chain) gets 8 bad
bytes; FreeDOS prints "Run chkdsk: Bad FAT value/index" and WAD reads return
zeros. An all-zero patch then fails the bounds check in `V_MarkRect`
(v_video.c:85, X=-1 Y=-1, from `V_DrawPatchScreen0` / `D_Display`), which
calls `I_Backtrace`; its log write to `fdoom.log` (a seek from the end) loops
forever in FreeDOS's `searchblock`. The disk itself is fine.

Only the `-debug` build is affected: `-d2` keeps `ptr`/`next` in the stack
frame; the release build keeps them in registers.

**Evidence an AI should find:** a DOS call that never returns (watch_program:
stuck inside INT 21h), the CPU looping in the FreeDOS kernel, "Run chkdsk"
in the DOS output, corrupted kernel buffers, the value 0020FDA0 in them, and
with a protected-mode-only write watchpoint over the buffers
(set_watchpoint cpu_mode="protected"), the writing instruction in
`TS_ServiceSchedule` with EBP pointing into low memory.

**Fix:** don't address locals through EBP after changing SS: make `ptr` and
`next` static (fastdoom-ns_task-stack.diff), or compute EBP for the new
stack too.
