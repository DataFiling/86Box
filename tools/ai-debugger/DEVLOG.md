# AI debugger: development log

Why each change was made, how it was tested, and what comes next. For how to
use the bridge, see [README.md](README.md).

## Goal

Let an AI (Claude, through MCP) run DOS-era games inside 86Box and debug them
live. That means seeing the screen, pressing keys, pausing, inspecting and
patching memory, and finding the code that changes things like lives or score.

## Architecture

```
Claude Code / Desktop
   │  MCP over stdio
   ▼
tools/ai-debugger/server.py      MCP tools (FastMCP), each run in a worker thread
   ├─ dos.py                     SEG:OFF parsing, register/hexdump formatting, BIOS text screen, IVT, disassembly (capstone)
   ├─ pcinput.py                 screenshots (frame fetch + pure-Python PNG), scan-code tables, key/mouse helpers
   └─ gdb_rsp.py                 GDB Remote Serial Protocol client
   │  TCP (default port 12345)
   ▼
86Box built with -DGDBSTUB=ON    src/gdbstub.c
```

Decisions:

- **Build on 86Box's existing GDB stub** rather than adding a new control
  channel. It already had pause, step, registers, memory, breakpoints,
  watchpoints and I/O-port monitor commands. New features (screen, input) are
  new monitor commands on the same connection, so the bridge also works when
  it runs on a different host from the emulator.
- **Python + MCP** because MCP is how Claude Code/Desktop call tools. Only two
  dependencies: `mcp` and `capstone` (optional, for disassembly).
- **Linear addresses everywhere.** The stub reports EIP and takes memory,
  breakpoint and watchpoint addresses as linear addresses. The bridge converts
  real-mode `SEG:OFF` to `SEG*16+OFF`.
- **Hardware-style breakpoints by default**, which don't modify guest memory
  and also work in ROM.
- **Input tools resume a paused CPU**, because a paused guest can't read the
  keyboard. This avoids "I typed but nothing happened".

## Protocol facts the bridge relies on (src/gdbstub.c)

- The CPU starts **paused** when the stub is enabled. Connecting pauses it,
  and it runs again when the last client disconnects.
- Each response must be acknowledged with `+` before the stub sends the next.
- `c`/`s` get no immediate reply; a `T..` stop packet arrives when the CPU
  stops. Byte `0x03` requests a pause.
- Packets are serviced between CPU time slices, so memory can be read while
  the guest runs.
- Register reply: segment registers carry stale upper bytes (masked to 16
  bits); EIP is linear. After a 286+ reset CS=F000 has base FFFF0000, so
  `SEG*16` doesn't hold until the first far jump. The bridge then shows only
  the linear PC.
- The number parser treats a leading `0` as **octal**, so the bridge always
  sends `0x`-prefixed hex or plain decimals.
- Monitor command dispatch matches only the **first letter** for the old
  `i`/`o`/`r` commands, so new command names must not start with those letters.
- With the stub enabled, CMake forces the CPU **interpreter** (no dynarec).

## Changes, in order

### 1. CLAUDE.md
A codebase guide for future AI sessions: build/test commands, code style, and
architecture (device_t, machines, memory/IO plumbing, CPU, video, platform
layer).

### 2. Watchpoints fixed on all CPUs (`e7f52e3`)
**Problem:** a write watchpoint on the BIOS tick counter (0040:006C) never
fired.
**Cause:** watchpoint checks live in `readmembl()`/`writemembl()` and friends,
but:
- 8086-class CPUs access memory through `read_mem_b/w`/`write_mem_b/w`, which
  had no hook.
- 486+ CPUs read and write through the MMU lookup caches (`readlookup2`,
  `writelookup2`) whenever a page is cached, which skips those functions.

**Fix:** hook the 8086-class accessors; `addreadlookup()`/`addwritelookup()`
refuse to cache pages that have a watchpoint (`GDBSTUB_PAGE_WATCHED`); the stub
calls `flushmmucache()` whenever the watchpoint list changes.
**Evidence:** on the 486, the same test without the fix missed 3/3 trials and
caught 3/3 with it. The 8088 now catches it too. Touched files compile without
warnings with the stub disabled.

### 3. MCP bridge (`cea6532`)
Execution control, registers, memory read/write/search, disassembly,
breakpoints/watchpoints, text screen, interrupt vectors, I/O ports, and an
end-to-end smoke test that drives the server through a real MCP client.

### 4. Screen and input in the stub (`e6c129b`)
New monitor commands:

| Command | Purpose |
|---|---|
| `fi` | Freeze the last completed frame; prints `width height frame#` |
| `fb` | Stream the frozen frame as RGB bytes (partial `O` packets) |
| `kd`/`ku` *scan* | Key down/up, set 1 scan code, `E0xx` for extended keys |
| `mm` *dx dy [dz]* | Relative mouse move (+ wheel) |
| `mb` *mask* | Mouse buttons held (1 left, 2 right, 4 middle) |

- **Frames:** `video_blit_memtoscreen_monitor()` calls `gdbstub_frame_blit()`,
  which copies each completed frame only while a client is connected. Both run
  on the emulation thread, so there's no locking and no torn frames. A paused
  guest keeps its last frame.
- **Mouse gate:** serial, PS/2 and bus mice ignore input unless the host
  mouse is captured. Calling `plat_mouse_capture()` would grab the user's real
  pointer, so a `mouse_injected` flag (set by `mm`/`mb`, cleared on
  disconnect) is OR-ed into those checks instead.
- **TCP_NODELAY:** each request took ~85 ms because the stub writes a reply in
  three small `send()` calls and Nagle's algorithm held them until the
  client's delayed ACK. With TCP_NODELAY on accepted sockets it's ~0.3–1 ms. A
  640×200 screenshot went from 4.3 s to 7 ms (also because `fb` streams
  instead of taking one request per 8 KB).

### 5. Bridge screen/input tools (`6412b00`)
`screenshot` (PNG encoded with zlib, optional downscale and save),
`press_keys`, `type_text`, `key_down`/`key_up`, `mouse_move` (large moves
split into steps), `mouse_click`, `mouse_buttons`, `mouse_scroll`.

### 6. Protected-mode basics
**Problem:** the GDB register packets carry selectors but not segment
bases, so in protected mode the bridge couldn't turn `SEL:OFF` into an
address, show `CS:EIP`, or know whether code is 16- or 32-bit. It refused
`SEG:OFF` there and assumed flat 32-bit code.

**Emulator:** a new `sg` monitor command reports each segment register's
descriptor cache (selector, base, limit, access byte, flags byte), GDTR,
IDTR, LDTR, TR, and the CPU's `use32`/`stack32`/CPL.

**Bridge:**
- `read_registers()` fetches `sg` too, falling back to the old behaviour on
  builds without it.
- One resolver (`dos.resolve`) for every tool. Segment registers use their
  cached base in all modes. Numeric selectors are `*16` in real/V86 mode, and
  in protected mode are looked up in the GDT or LDT, which also gives the
  code size.
- `CS:EIP` is shown in protected mode with the code/stack size, CPL, paging,
  and the segment bases. Real mode notes "unreal" segments (limits above 64
  KiB).
- A side benefit: right after a 286+ reset the bridge shows `F000:FFF0`
  (base FFFF0000), where before it could only show the linear PC.
- Disassembly, `step_over` and `read_stack` use the real code and stack sizes.
  `set_register ip/eip` takes the offset within CS; `pc` takes a linear
  address.
- New tools: `get_segments`, and `read_descriptor_table` for the GDT, LDT and
  IDT (segments, TSS/LDT entries and gates decoded).
- Breakpoint, watchpoint and run-until results say how the address was
  resolved. A protected-mode selector given while the CPU is still in real
  mode is (correctly) taken as a real-mode segment, which the test hit once
  and which an AI could otherwise miss.

**Test program:** `testdata/pm_probe.asm` (NASM, with the assembled `.bin`)
is loaded by the debugger at 0000:8000. It builds a GDT, enters protected
mode, and loops between a flat 32-bit code segment and a 16-bit code segment
based at 8100h that increments a word through FS (base 12340h). That covers
16- and 32-bit protected-mode code, non-zero bases, and selector lookup.
`smoke_test.py --protected-mode` runs it on 386+ machines and hard-resets
afterwards.

### 7. Review (`8b8756a`, `415974d`, `4eeba0a`)
A review workflow (88 agents: finders across the stub, the memory hooks and
the bridge, then adversarial verifiers) confirmed 38 issues, all fixed. The
main ones:
- **Stub:** `m`/`M` packets that hit a page fault sent corrupt replies or
  reported success, and they overwrote the guest's CR2. A watch address at
  2 GiB or above indexed out of bounds. A quick reconnect could have its new
  socket closed by the old client's cleanup (descriptor reuse); this is now
  fixed with a `gone` flag, closing under the client mutex, and `shutdown()`.
  A send to a departed client could raise SIGPIPE. Streaming a frame (`fb`)
  deadlocked if the client left.
- **Memory hooks:** 8086-class instruction prefetch triggered read
  watchpoints. Fetches now use `read_mem_fetch_*`, which skips the hook.
- **Bridge:** a NUL strip cut trailing black pixels from frames. `step_over`
  mishandled `lcall` and `rep` before non-string ops. Several tools were
  wrong in protected mode or at segment wrap.
- **New:** `hold`, so `run_tool.py` can leave a paused CPU paused between
  commands, and `bl`, so a reconnecting bridge knows the stub's breakpoints.

### 8. Test setup across three eras (`6fe825c`, `7d47ede`, `7d0bb99`)
The DOS tools are built from source, because the network policy blocks the
usual download sites and only git access to GitHub works: Open Watcom v2,
the FreeDOS kernel and FreeCOM, JWasm and CuteMouse. `testdata/build_testbed.sh`
compiles them into one boot floppy. It holds five programs:
- three with planted bugs (TXTGAME, VGAGAME, PMGAME);
- MOUSETST, to check INT 33h;
- BENCH, to measure speed.

There are three VM configs (386, 486, Pentium MMX). Details are in
[testdata/README.md](testdata/README.md) and the answers in
`testdata/GROUND_TRUTH.md`.

**Speed:** the interpreter keeps real time at Pentium 100 (59% of one host
core) and Pentium MMX 200 (0.99x real time, 73%). Pentium-era games
therefore don't need dynarec support in debug builds.

### 9. AI debugging exercises
Three agents each got one machine and only the bridge (through
`run_tool.py`), the user's symptom, and the task: find the cause, find
where the game keeps lives, propose a fix. None of them saw the source code
or the ground truth. A separate agent graded each report against
`GROUND_TRUTH.md`.

| Machine / program | Diagnosis | Lives location | Fix | How it got there |
|---|---|---|---|---|
| 386 / TXTGAME | correct | correct (DS:005A) | correct (3DAh) | paused the hang, found a `in al,dx / test al,8 / jnz` spin with DX=3DBh, patched both port words in RAM and saw the game continue |
| 486 / VGAGAME | correct | correct (DS:0026) | correct (`difficulty - 1`) | write watchpoint on lives, then traced `call [bx+0033]` with BX=6 one past the 3-entry table to a junk target and an invalid opcode |
| Pentium / PMGAME | correct | correct (linear 14C464h) | correct (`i < MAX_SHOTS`) | reproduced with rapid fire; a write watchpoint on lives caught `fire()` writing `shots[8]`, then it patched `jg` to `jge` and retested |

All three also handled a misleading remark relayed from the user ("a blank
screen with a yellow box") without letting it skew the diagnosis.

### 10. Fixes from the exercise feedback (`8d4a934`)
The agents reported friction with the tools, which was fixed:
- **Stop reason across reconnects.** Each `run_tool.py` call reconnects. The
  stub then reported a fresh break, so a watchpoint hit seen in one command
  showed as "signal 5" in the next. The stub now keeps the stop reason while
  the CPU stays stopped. A new `state` command tells the bridge whether the
  CPU is running, and `hold` also stops a connection from pausing a running
  CPU. `status` shows "CPU is stopped. Last stop: write watchpoint at ...".
- **`step_out`:** runs until the current procedure returns, by stepping over
  calls until a `ret`/`retf`/`iret` at the starting depth.
- **Input while stopped.** If a breakpoint hits while keys or clicks are
  being injected, the tool stops injecting and says so, instead of reporting
  success.
- **Small things:**
  - `clear_breakpoint` needs an address (it used to clear everything when
    called without one) and says what it removed.
  - `search_memory` labels hits as SEG:OFF when the start was SEG:OFF.
  - `read_text_screen plain=true` returns just the text.
  - 16-bit `98h`/`99h` now disassemble as `cbw`/`cwd`.
  - `run_tool.py`: unescapes `\n` (so `type_text text="PMGAME\n"` works),
    accepts `0x` integers, and names missing required parameters.
- **Mouse wheel.** The PMGAME agent found that the wheel didn't reach
  MOUSETST. There were two causes, both in the test setup, not the stub. The
  PS/2 mouse defaults to 2 buttons (no wheel), and CuteMouse only looks for
  a wheel with `/O`. With `buttons = 4` and `CTMOUSE /O`, 3 clicks read as
  wheel -3 through INT 33h.

### 11. DOS and extender awareness
**Goal:** what the exercise agents asked for and the roadmap's step 5: know
which program runs and where it is, see the DOS/DPMI/BIOS calls it makes,
stop at its start, and find variables without manual search loops.

**Emulator** (`src/gdbstub.c`, with `gdbstub_int()` called by `INT n` in the
386 interpreters and both 8086 cores):
- Interrupt log (`tv`/`ts`/`tl`/`tc`): records with registers, 64 bytes at
  DS:(E)DX, DS:(E)SI and ES:(E)DI, and on return the registers and buffers
  again. A return is detected when execution reaches the instruction after
  the `INT` with the same SS:SP; a pending list plus a 1024-entry hash keeps
  this to one table lookup per instruction. Identical back-to-back calls are
  folded into one record with a count, so DOS's keyboard polling (26,000
  calls in a few seconds) takes one record.
- Catchpoints (`ca`/`cx`): a call catch checks the bytes of the next
  instruction (`CD xx` after prefixes) before it runs, the same way on every
  core; a return catch stops at the return address.
- `xr`: stop when execution enters a linear range.
- `mr`: side-effect-free reads (RAM/ROM only, through the page tables
  without faulting), so snapshots never touch VGA latches or other devices.
- EFLAGS reads and writes now account for the interpreter's lazy flags.

**Bridge:** `dosinfo.py` (call decoders, MCB chain, current PSP from DOS's
swappable data area at DOS data segment + 330h, found through the first-MCB
pointer at +24h), `memscan.py` (snapshots, diffs, value scans with state on
disk), and the tools `dos_memory_map`, `log_interrupts`,
`read_interrupt_log`, `catch_interrupt`, `wait_for_program_start`,
`snapshot_memory`, `diff_memory`, `scan_memory`, `scan_next` and
`restore_memory`.

**Program start detection:**
- Real mode: catch `INT 21h AX=4B00h`, check the file name, then stop at
  the first instruction executed in memory that was free at that moment.
  That is the new program's entry, since DOS loads it there and runs no
  other code there. TXTGAME: `240E:05B2`, Watcom's startup.
- DOS/4GW: the log showed it loading the program file from 16-bit
  protected mode and allocating its objects with DPMI `0501h`. So: catch
  the protected-mode open of the program file, then the returns of
  `0501h`/`0503h`, and stop when execution first enters one of those
  blocks. PMGAME: linear `12D5C8` (Watcom's 32-bit startup); data block at
  `14C000`, as in the answer file.

**Speed:** a 16 MB snapshot or scan step takes 1.2 s. The first version
took 17 s: replies were accumulated with `bytes +=`, which is quadratic.

**Not possible:** machine save/restore. 86Box has no save states, and
restoring RAM alone would leave devices out of step; `restore_memory` only
puts back chosen bytes.

### 12. Review and second exercise round
A workflow ran 3 reviewers (stub, Python modules, server tools) and 2
exercise agents, each graded by a separate agent against `GROUND_TRUTH.md`.

| Exercise | Diagnosis | Locations | Fix | Key tool |
|---|---|---|---|---|
| 486 / DATAGAME (new: "level data corrupt") | correct (opened write-only, mode 01h) | correct (DS:02E0) | correct (patched `mov dx,1` to 0 in memory, levels listed) | `read_interrupt_log` |
| Pentium / PMGAME (startup, score, entry) | correct | correct (score 14C468h, `inc` at 12D3FA) | correct (entry 12D5C8h) | `wait_for_program_start protected_mode`, log, watchpoint |

Review findings fixed:
- **High:** a hardware breakpoint on a caught `INT` made it unpassable (the
  hardware-break check replaced the catch's stop reason, which dropped its
  run-once marker).
- **Medium:** call catches used to abort the `INT` and rewind EIP, so the
  interpreter could still deliver a pending trap or IRQ before stopping.
  Both are fixed by the redesign above (check the next instruction instead
  of aborting).
- **Medium:** on NEC V20/V30 the stub saw EIP one byte ahead (prefetched
  opcode), breaking return tracking, ranges and hardware breakpoints.
- **Low:** the log fold after `tc`; sequence number wraparound; lazy flags
  rebuilt on 8086 cores; a return catch overriding a watchpoint stop.
- **Bridge:**
  - The MCB chain with `DOS=UMB` and the link off.
  - A stop lost while `wait_for_program_start` was typing.
  - A failed exec reported as a start.
  - Reflections split into several calls.
  - `run_tool` turning `4E00` into a float.
  - Scan `unchanged` excluding untouched bytes.
  - Snapshot pickles in a shared temp directory (now a private per-user
    directory).
  - Several decoders (INT 33h, INT 13h, DPMI 0003h/0202h/0204h, PSP
    environment under DPMI).

Exercise feedback acted on:
- `run_tool.py help`;
- program-entry wording;
- a warning when a selector is used while the CPU is in real mode;
- input tools saying which stop they resume from;
- null selectors shown without a base;
- `scan_memory aligned`.

### 13. Unattended runs and a real game (FastDoom)
**`watch_program`:** starts a program and watches it, then reports whether
it exited (and with what code and error text), crashed, hung (and inside
which call), or waits for input, with the evidence. Checks run every
0.5–2 s:
- CPU position samples (a small loop);
- INT call activity (folded repeats count);
- a hash of the program's memory (minus the area interrupt frames rewrite)
  and of the screen;
- how long the innermost DOS/BIOS call has been running, in emulated time.

It caught every test case:

| Program | Outcome |
|---|---|
| DATAGAME | exits with code 3 and its error |
| VGAGAME | "Invalid Opcode" crash |
| TXTGAME | port-polling hang |
| the prompt | waits for input |

Two false alarms were fixed on the way: frame-pacing loops looked like hangs
(fixed with the memory and screen hash), and a missed return looked like a
call still running (fixed by checking for later calls at the same depth).

**FastDoom:** only GitHub is reachable here, so the real game is FastDoom
built from source with the free Freedoom data, on a 64 MB hard disk image
(`testdata/build_fastdoom.sh`). It hung at the end of every startup. The
debugger traced it to a FastDoom bug:
1. `watch_program`: stuck for minutes inside an `INT 21h` open, made from
   FastDoom's 32-bit code and passed down by DOS/4GW; the CPU loops in the
   FreeDOS kernel; DOS printed "Run chkdsk: Bad FAT value".
2. Walking FreeDOS's buffer list in guest memory: one header held
   `next=FDA0 prev=0020`. A later run instead had 8 bad bytes in a cached
   FAT sector (`A0 FD 20 00` twice). The disk image passes fsck, so memory
   was being corrupted.
3. A FreeDOS backtrace from a catchpoint on the "Run chkdsk" output (kernel
   map symbols): `int21 AH=3Fh` → `DosRWSft` → `rwblock` → `map_cluster` →
   `next_cluster`, on the WAD's handle, whose SFT was intact.
4. `0x0020FDA0` in FastDoom's linker map: `HeadTask`, the timer task list
   head in `ns_task.c`.
5. A write watchpoint over DOS's buffers was too slow, since DOS writes
   there constantly. So the stub gained a watchpoint CPU-mode filter
   (`wf pm`, `set_watchpoint cpu_mode="protected"`). The next run stopped on
   the write: `TS_ServiceSchedule+4Eh`, `mov [ebp-8],eax`, with
   EBP=00004CA4.
6. The interrupted stack was DOS/4GW's interrupt stack, selector 00B0 with
   base 143DF0h. The handler switches SS to its own zero-based stack but
   keeps using EBP for its locals, so they land in low memory whenever IRQ0
   interrupts real-mode code (DOS reading the WAD).

The exercise agent (section 14) later traced the rest of the chain: the
corrupted FAT sector made WAD reads return zeros, a bounds check in
`V_MarkRect` called `I_Backtrace`, and its log write is the DOS call that
hangs. It also noticed that only the `-debug` build (`-d2`) keeps the locals
on the stack; a clean release build keeps them in EDX/ECX (checked with
wdis), so the bug is latent there.

Making the two locals static (`testdata/fastdoom-ns_task-stack.diff`) fixes
it: the game loads and plays its demos under the debugger on the Pentium MMX
machine. The bug should be reported upstream (viti95/FastDoom). Its
write-up is in `GROUND_TRUTH.md`, so the unpatched build can serve as a
real-world exercise.

### 14. Symbols, and an AI exercise on a real bug
**Symbols:** `load_symbols` reads an Open Watcom linker map and places it.
- 16-bit programs go at their load segment.
- DOS/4GW objects are placed from the entry point (the map's entry offset)
  and from the DPMI block whose size matches each object; checked against
  `HeadTask` being a valid list head.
- Names then work as addresses, and name+offset appears in:
  - disassembly (function labels, branch targets, memory operands relative
    to DS);
  - the CPU state and stack dumps (code symbols only, within code segments);
  - the INT log and `watch_program`.

**Exercise:** a fresh agent got the unpatched FastDoom build, its source and
linker map, and only the symptom: "freezes on the black screen after
loading". It had no access to these notes. In about 7½ minutes and 33 tool
calls it found:
1. With `watch_program`: the game hung inside an `INT 21h` seek, and DOS had
   printed "Bad FAT" messages.
2. With `set_watchpoint` over DOS memory and `cpu_mode="protected"`: the
   first hit was `TS_ServiceSchedule_+4E` with EBP=4CA4 and stack 00B0, base
   143DF0.
3. With a breakpoint on `I_Backtrace_`: the rest of the chain, which I had
   missed. The FAT chain corruption makes WAD reads return zeros, a bounds
   check in `V_MarkRect` fails, and the backtrace's log write is the DOS
   call that hangs.

It also pointed out that only the `-debug` build keeps the locals on the
stack, which I confirmed with wdis on a clean release build. That narrowed
the upstream report.

Graded against `GROUND_TRUTH.md`: cause, evidence and fix correct.

Its tool feedback, all acted on:
- `read_descriptor_table` takes hex and a `selector`;
- `run_tool` accepts common parameter aliases (`count`, `name`, `path`);
- `watch_program` collapses repeated events;
- a note when EBP is far from ESP (a frame pointer from another stack);
- a new `what_is` tool (symbol, PC area, DOS block and owner, DPMI block).

### 15. Exercise without symbols, a code review, and code tools

**Exercise (no source, no map):** a fresh agent got the release FastDoom
running its demos on the Pentium machine, with the task a game trainer has:
find the player's health, the code that lowers it, and make the player
invulnerable. In about 8 minutes and 42 tool calls it:
1. Found health at 001F9D7C (player struct 001F9D50 + 2Ch) with
   `scan_memory`/`scan_next` in two steps, plus the actor's own copy at
   mobj+7Ch and the status bar's cached copy.
2. Found both damage routines: `P_DamageMobj` at 1A1C60, and a second copy
   at 1A1F38 that damaging floors use. A write watchpoint gave the
   first one; when health still dropped after patching it, the watchpoint
   gave the second.
3. Patched a `jmp` to each routine's epilogue at the start of the player
   block (1A1D81, 1A1F8A). It verified the patch over 13 hits: health
   stayed at 100.

Graded correct against the source and map, which the agent never saw. It
also correctly identified the checks it skipped: the god-mode cheat flag and
the invulnerability power-up.

Its feedback led to these new tools and fixes:
- **`find_references`:** lists the instructions that use an address as an
  absolute operand, or call or jump to it. Each candidate is decoded with
  capstone and accepted only when the address is that instruction's real
  displacement or immediate.
- **`log_hits`:** runs, and records each execution of an address (registers,
  memory, return addresses) without the agent having to loop over
  breakpoints.
- **`patch_code`:**
  - writes `jmp`/`call` with the offset computed, `nop` or `ret`, and keeps
    the original bytes so `undo` puts them back;
  - pads a patch that ends inside an instruction with NOPs;
  - warns when it removes push/pop instructions.
- **`what_is`** names the function an address is in when there is no
  symbol: the nearest call target before it.
- **`search_memory`:**
  - searches all RAM by default and says which range it searched (it used to
    search only the first MiB, and silently found nothing in a DOS/4GW
    game);
  - reads without side effects;
  - takes quoted text.
- **`wait_for_stop`** says when there was no new stop.

**Code review** of the newest code (an independent agent with test maps and
scripts) found and fixed:
- `symbols`:
  - C++ names with spaces were cut short;
  - .COM maps were off by 100h;
  - statics with the same name in several modules silently resolved to one
    of them (now ambiguous, with `module!name` to pick);
  - names reached past their segment, so the stack and other programs were
    labelled as the last symbol + a large offset;
  - bad bases were saved and broke every later tool.
- **Disassembly comments:** they named stack-relative and far operands as
  if they were data symbols.
- **`watch_program`:**
  - the program's output included the shell's echo;
  - backspaces weren't applied;
  - `stop_when` didn't honour `exit`;
  - a program waiting at a DOS Abort/Retry prompt was called hung.
- **`run_tool`:** string parameters lost their exact text (`1.50` became
  `1.5`).

**Live testing then found:**
- **`load_symbols` accepted a map from another build.** The debug build's
  map, loaded against the release build, gave plausible but wrong names.
  Before loading, it now checks that near calls in the code land on the
  map's functions:

  | Map | Calls that land on its functions |
  |---|---|
  | correct | 70% or more (static functions aren't in maps) |
  | wrong build | about 1% |

  Below 30% it refuses the map. `hard_reset` now forgets symbols, patches
  and the last program start, and `map_path=none` unloads symbols.
- **Wrong code size:** disassembly and `patch_code` undo picked 16-bit code
  for a 32-bit address when the CPU happened to be in real mode (inside DOS
  for the game).
- **Duplicate calls:** `watch_program` listed every DOS call a DOS/4GW game
  makes twice, once from protected mode and once as the extender's real-mode
  copy. The copy is now dropped, and identical failures are counted
  instead of repeated.
- **False EBP warning:** the EBP note fired when release code used EBP as a
  general register.
- **Live patching is dangerous:** NOPing the pushes at a live function's
  entry crashed the game a few minutes later in the extender's real-mode
  code. That is why `patch_code` now warns about stack instructions. Writes
  from the stub go through the CPU's write path, so the recompiler drops
  stale code (the exercise agent confirmed patched code took effect at
  once).

### 16. DOS/32A

FastDoom ships DOS/32A 9.1.2, the most common replacement for DOS/4GW; many
games were rebound to it. I first bound it inside the Pentium VM with
DOS/32A's own tools (`SB /R`, then `SS` with FastDoom's settings).
`testdata/bind_dos32a.py` now does the same outside DOS. It rewrites the
extender's MZ header, appends the LE part with its data-pages offset moved,
and copies in the settings block. Its output is byte-identical to SB+SS.
`build_fastdoom.sh` takes `FASTDOOM_EXTENDER=dos32a` and `FASTDOOM_DEBUG=0`.

**What worked unchanged:**
- `wait_for_program_start(protected_mode=true)` stopped at the 32-bit entry.
  DOS/32A allocates the program's objects through its own DPMI (0501h,
  called from inside its loader), so the existing detection applies. It
  places objects tightly: code at 100010h, data at 165230h.
- `load_symbols` placed the release map automatically: 617 of 625 call
  targets landed on its functions.
- Names in `log_hits`, `find_references` and `what_is` worked. For example,
  `log_hits address=P_DamageMobj_` named the caller
  `PTR_ShootTraverse_+343`, and `find_references target=_players+2C` listed
  every health access by function.

**What needed fixes:**
- **DPMI calls from DOS/32A's kernel decoded as garbage.** The kernel is
  16-bit protected-mode code. It passes DOS calls down by asking its DPMI
  host to simulate a real-mode interrupt (0300h) with ES:EDI. That host
  serves a 32-bit client, so it uses the full EDI. The stub captured ES:DI,
  which was right during loading but wrong in game time, when EDI's upper
  half is set. The stub now takes the full register in protected mode when
  the upper half is set and the offset is inside the segment's limits.
- **Every DOS call appeared twice.** DOS/32A passes calls down differently
  from DOS/4GW: a DPMI 0300h from its kernel, not a real-mode INT 21h. The
  copy is now recognised as a reflection (`dosinfo.is_reflection`, shared by
  the INT log and `watch_program`).
- **Pending calls survived a reset.** A hard reset left the stub's pending
  calls in place, so every later call nested under an exec that would never
  return. `resetx86()` now marks them as never returning. A soft reset is
  left alone: 286 extenders and HIMEM use it to get back to real mode, and
  their calls do return.
- **`type_text` lost keys.** Typing ahead while DOS was copying a file
  overflowed the BIOS's 15-key buffer, and the next command arrived
  garbled. It now waits while the buffer is nearly full.
- **False hang.** `watch_program` called DOS/4GW's private calls
  (INT 21h FF00h/FF01h, which run the whole program) a hang. Calls with
  AH >= 70h, past the documented DOS functions, no longer count as calls
  that should return at once.
- **`what_is` guessed functions for heap data.** It guessed a function for
  a heap object (a map actor). It now guesses only inside the program's
  code, and says when an address is outside the program's objects.

**The FastDoom bug under DOS/32A.** The unpatched `-debug` build doesn't
hang under DOS/32A; it loads and plays. To find out why:
1. `log_hits` on the faulty store, logging EBP. During WAD loading, EBP was
   always 30074h.
2. An access watchpoint on 3006Ch-30073h showed DOS/32A's kernel pushing
   through those bytes, so they are on its stack.
3. At the store, the handler's saved stack was 0018:00030068, and
   selector 0018 has base 0.

The handler reserves its locals on the interrupted stack (`mov ebp,esp;
sub esp,0Ch`). With a zero-based stack, linear EBP-8 is exactly that
reserved slot, so the stores are harmless. Under DOS/4GW the interrupt
stack's base is 143DF0h, and the same stores hit DOS's buffers. The bug
report and `GROUND_TRUTH.md` now say the bug depends on the extender as
well as on the build.

### 17. Logpoints, hit counts, stops between commands

These fix the remaining problems from the exercise feedback and the DOS/32A
round:

- **`log_hits` stopped the CPU at every hit.** Each stop cost several round
  trips, so it managed about 9 hits per real second. The stub now has
  logpoints (`lp`/`ll` monitor commands), which record each hit into a
  4096-entry ring buffer while the program runs at full speed. Each record
  holds the registers, segment selectors, 16 bytes at SS:(E)SP and an
  optional dword at DS:reg+disp or a linear address.
  - On FastDoom's timer handler it logged 357 hits in 10.16 emulated
    seconds: Doom's 35 Hz tick.
  - Times are now emulated seconds.
  - Callers are listed only when the stack top really is a return address,
    one just after a call (direct to this address, or indirect). This holds
    in 16- and 32-bit code; a mid-function address no longer shows junk
    "callers".
- **Hit counts:** breakpoints and watchpoints count the times they stopped
  the CPU (a fourth column in `bl`), and `list_breakpoints` shows them.
- **`wait_for_stop` across commands:** the stub's `state` reply now includes
  a stop counter, and `resume` records it. A stop that happened between a
  `resume` and a later `wait_for_stop` (separate `run_tool` commands) is now
  reported as that stop. It used to be "No new stop: the CPU was already
  stopped".
- **Memory reads at junk addresses:** `read_memory` masks addresses to 32
  bits and turns an unexpected reply into a GdbError, not a ValueError.

### 18. PMODE/W and CauseWay

Open Watcom ships two more extenders that games used: PMODE/W and CauseWay.
I linked the 32-bit test game and the release FastDoom with each, and with
DOS/32A for comparison. `build_fastdoom.sh` takes
`FASTDOOM_EXTENDER=pmodew|causeway`. Both games load and play, and FastDoom
ran its demos under each.

Program-start detection was built around DOS/4GW and broke twice:

- **PMODE/W** opens the program file from real mode, before switching to
  protected mode. It loaded the test game's code into conventional memory
  (31000h), outside any DPMI block. The detection waited for a
  protected-mode open, so it never fired.
- **CauseWay** runs 32-bit code of its own (CPL 3, paging on) under at least
  five code selectors before the program.

The new detection:

- The stub has a one-shot catch for the first 32-bit protected-mode
  instruction (`xr pm32`), optionally skipping given code selectors.
- `wait_for_program_start(protected_mode=true)` accepts the program file
  being opened in any mode. It then stops at the first 32-bit instruction,
  or when execution enters DPMI memory allocated after the open.
- If that code isn't the program, the bridge skips its selector and goes on.
  The test is Open Watcom's startup code: a short jump over "WATCOM". Code
  running at CPL 3 with paging on (CauseWay's kernel) is skipped unless it
  has that signature.
- Old 86Box builds without `pm32` fall back to DPMI blocks only.

Results:

| Extender | Test game entry | FastDoom entry | Map check (calls on functions) |
|---|---|---|---|
| DOS/4GW | 12D5C8h | (unchanged) | 24/34 |
| DOS/32A | 1005D8h | 151180h | 24/34, 617/625 |
| PMODE/W | 315C8h | 152170h | 24/34, 617/625 |
| CauseWay | 4355C8h | 486170h | 24/34, 617/625 |

**Placing the data object.** Matching block sizes put PMODE/W's data at the
start of its block (100008h). The data is really at 101000h: PMODE/W puts
something in front of it. `load_symbols` now places each non-entry object
from the code itself. It collects the absolute addresses in the code
(`[disp32]` operands, `push imm32`), which the extender has relocated, and
finds the base at which most of them land on that object's symbols. On
PMODE/W this found 101000h, with 169 of 210 sampled references agreeing.
The game's "Score:" string at 101004h (CONST at offset 4) confirms it. It
needs no DPMI blocks at all, so CauseWay is covered too.

**Other fixes:**
- **Stale program start:** a remembered start now carries the machine's time
  counter, and is ignored after a reset or a VM restart (the counter goes
  back).
- **Wrong map:** `load_symbols` refuses automatic placement when the map's
  program name differs from the started program's and the code check is
  inconclusive.
- **False hang:** DPMI vendor extensions (INT 31h AX >= FF00h; CauseWay runs
  the program inside FF24h) no longer count as calls that should return at
  once.
- A resized DPMI block is no longer listed twice.

Logpoints, symbols and `find_references` work under CauseWay's paging. The
smoke test passes on all four machines (from a DOS prompt; under CauseWay
the BIOS timer handler it breakpoints doesn't run).

### 19. EMM386 (JemmEx), V86 mode and VCPI

Most 386+ DOS setups loaded EMM386, which runs DOS in V86 mode under a
32-bit monitor and makes DOS extenders switch to protected mode through VCPI.

**Setup:** I built JemmEx (JEMM386 with its own XMS driver) from source,
using JWasm, with Open Watcom's `wlink` standing in for JWlink. JWasm had to
be rebuilt with `_FORTIFY_SOURCE` off: its listing writer overflows a 3-byte
field, which fortified glibc aborts on. `testdata/build_emm_floppy.sh` makes
the floppy. A fifth VM (Pentium MMX, port 12349) boots it. `get_state`
reports V86 mode (paging on, CPL 3), and the smoke test passes there.

**What broke:** program-start detection. Under a V86 monitor, every
interrupt or I/O trap from V86 code runs the monitor's 32-bit code, so "the
first 32-bit instruction" was always JEMM's. The block-range catch fired
there too. Under VCPI each side has its own page tables, so PMODE/W's DPMI
block at 111008h overlapped the numbers of JEMM's code at 11472Eh. And
JEMM's VCPI server (DExxh functions) runs in the client's address space,
under the client's own selector 28h.

**Fix:**

- **Stub:** the 32-bit catch can skip a code segment by selector and base
  (`skipb`), or a linear range of one address space (`skipr LO HI CR3`).
  The block ranges honour the skips too.
- **Bridge:** `wait_for_program_start` reads the monitor's IDT before the
  program starts. Any protected-mode IDT installed then belongs to the V86
  monitor. From it, the bridge skips the handlers' code segments and their
  linear range in the monitor's address space.
- **Acceptance:** with a monitor present, a stop counts as the entry only
  with Open Watcom's startup signature; anything else (like the VCPI server
  under the client's selector) is skipped by selector and base.

Results under JemmEx, all graded against the maps:

| Program | Entry | Map check (calls on functions) |
|---|---|---|
| test game, DOS/4GW | 43D5C8h | 24/34 |
| test game, DOS/32A | 1115D8h | 24/34 |
| test game, PMODE/W | 265C8h | 24/34 |
| test game, CauseWay | 4355C8h | 24/34 |
| FastDoom, DOS/4GW (VCPI) | 4D2170h | 617/625 |

Also checked under JemmEx:
- FastDoom plays.
- `log_hits` named its callers.
- A write watchpoint on the player's health stopped in
  `P_TouchSpecialThing_` (a health pickup), through the extender's paging.

Without EMM386 every entry is unchanged. The smoke test passes on all five
machines.

### 20. Round 3: five blind exercises with different kinds of bugs

Five new test games, one planted bug each (sources in `testdata/games/`,
answers in `GROUND_TRUTH.md`). One fresh agent per game got only the
user's symptom, no source and no notes; four ran in parallel on separate
VMs. Only TOUR's agent had a linker map.

| Game | Bug | Machine | Result | Time | Tool calls |
|---|---|---|---|---|---|
| CAVES | function returns a pointer to its own stack buffer | 486, real mode | correct | ~10 min | 22 |
| SHIPS | uninitialized heap field (malloc assumed to zero) | 386, real mode | correct, fix proven by patching memory | ~12 min | 25 |
| COINS | race: main loop reads then clears a counter the timer IRQ adds to | Pentium, DOS/4GW | correct, fix proven by patching in `xchg` (score then exactly coins x 5) | ~18 min | 25 |
| TOUR | sprite cache leaked every level | Pentium + JemmEx, DOS/32A over VCPI, with map | correct | ~10 min | 17 |
| SBTEST | BLASTER port parsed as decimal | Pentium + JemmEx, PMODE/W | correct, fix proven by setting the register | ~10 min | 17 |

Five out of five, each with the code addresses, the evidence and a working
fix. The common path was:
- `watch_program` to reproduce the bug;
- `search_memory` for a string the program prints;
- `find_references` or a watchpoint to reach the code;
- disassembly, then a live patch to prove the fix.

**Tool feedback, and what changed:**
- **`find_references` missed 16-bit references.** It found nothing for
  real-mode data: Watcom 16-bit code passes near pointers as `mov ax,ofs` /
  `push ofs` and addresses globals as `[ofs]`. It now also matches
  `[disp16]` operands and `imm16` values equal to the target's offset in
  its segment (the given segment, else DS), labelling immediates "offset
  value". On CAVES it found the format-string load and the six uses of
  the score table.
- **`disassemble` stopped silently** at bytes capstone can't decode (a
  start in the middle of an instruction). It now shows them as `db`, goes
  on, and says the start may be mid-instruction.
- **No backtrace.** `read_stack` now marks return addresses: values right
  after a near call (or indirect call) in the current code segment, and in
  real mode far CS:IP pairs after a far call. The callers of a library
  routine are then visible at a glance.
- **`log_hits`:**
  - It now stops when the program exits (it catches INT 21h AH=4Ch while
    logging) and leaves the CPU at the exit call, with memory still
    readable. TOUR's run ended after 6 s instead of waiting the full 60.
  - It shows the first 20 and last 40 hits; the end of a run is what
    matters for "fails after N".
  - It summarises each logged value (counts when there are few distinct
    values, the range otherwise).
  - Its memory operand follows the CPU's segment rules: `[ebp+x]`/`[esp+x]`
    are SS-relative, and `ss:`/`ds:` prefixes are accepted. In COINS the
    timer handler runs on DOS/4GW's stack (SS base 143DF0h), so a
    DS-relative read logged garbage. The stub's logpoints take SS-relative
    operands for this.
- **`read_text_screen`** now returns plain text with a header line (mode,
  size, cursor); JSON with `format=json`.
- **`search_memory`:**
  - It takes `text=`.
  - It marks hits above 1 MB that are the first MB seen again: it checks
    that A20 is off by comparing 0 and 100000h.
- **Parameter names:** `run_tool help` lists every tool's parameters, and
  more aliases are accepted (`address`/`target`, `register`, `text`, `hex`,
  `map`, `file`, `seconds`).
- **Wording:** a breakpoint from another run_tool command is "set by an
  earlier command".

Not done:
- Conditional breakpoints.
- Recording what an interrupt handler interrupted.
- Naming C runtime routines (inp/outp/strtoul) without a map.

All four agents asked for one or more of these; they are the next
candidates. The first two are done in section 21, the third in section 22.

### 21. Conditional breakpoints and what an interrupt interrupted

**Conditions in the stub.** Each breakpoint and watchpoint can carry one
condition, set with the monitor command
`bc TYPE ADDR SRC REG ARG SIZE OP VALUE` (or `bc TYPE ADDR off`):

- The tested value is a register (including AH/BH/CH/DH), a linear dword,
  word or byte, or memory at DS:reg+disp or SS:reg+disp.
- The operators are `==`, `!=`, `<`, `>`, `<=`, `>=`, `&` (any bit set) and
  `changed`. `changed` compares against the value when the condition was
  set, then against the previous test.

The test runs inside the emulator, so a false condition costs no round trip
and the program keeps its speed. Execution breakpoints test it before the
instruction. Watchpoints test it after the accessing instruction (the stop
is deferred one instruction), so they see the new value. `bl` now also
reports how often the condition was false.

**Recording interrupts.** Every CPU core calls `gdbstub_irq(vector)` when it
takes a hardware interrupt: `386.c`, both places in `386_dynarec.c`,
`808x.c` and `vx0.c` (NEC V20/V30). The stub keeps the last one: its vector, CPU mode, CS and the
linear address it interrupted, and its time. Logpoint records grew from 88
to 104 bytes to carry it. The bridge reads either size.

**Bridge:**

- **`condition=` on `set_breakpoint`/`set_watchpoint`.**
  - Examples: `eax == 5`, `ah == 4C`, `byte [esi+3] & 80`,
    `[ss:ebp-8] != 0`, `word [symbol] < #10`, `[esi] changed`.
  - Numbers are hex. A leading `#` makes a number decimal, and `-1` is
    FFFFFFFF.
  - For a watchpoint, a condition that starts with its operator tests the
    watched memory itself: `== 0`, `changed`.
  - `[es:di]` and similar are refused rather than silently turned into a
    fixed address.
- **Conditions are kept** in the state directory, so `list_breakpoints`
  shows them, with `hits=` and `false=`, across run_tool commands.
- **Setting a point where one exists replaces it.** Before this the stub
  refused it with E22.
- **`log_hits` on an interrupt handler.** It recognises a handler (at least
  80% of hits come within 0.5 ms of a hardware interrupt with the same
  vector). It then lists the addresses the interrupts came in at, and adds
  "interrupted ADDR" to each hit.

**Checks on the Pentium VM, with COINS:**

| Check | Result |
|---|---|
| Write watch on `pending` (14C464), `== 0` | Stopped first in the C runtime's BSS clear (`rep stosd`), then only at `main_+86` (`pending = 0`) |
| Same watch, `> 0` | Stopped only in the timer handler (12D07E), with 119 zero writes counted as false |
| Same watch, `changed` | Alternated: main loop (5→0), handler (0→5), main loop |
| Breakpoint at `main_+63`, `ax == 5` | Stopped with EAX=5 after 61 false passes |
| `log_hits` on the handler (12D010), 500 hits | "This is an interrupt handler: each hit follows an INT 08h". Most of the 22 interrupted addresses were in `draw_number`. `main_+70` and `main_+7D` are inside the race window `main_+5E`..`main_+86` between reading and clearing `pending`: the bug, seen directly |
| `log_hits` on `main_+5E` | No handler report |

On the 386, a breakpoint on DOS's INT 21h entry (00D9:12E8) with
`ah == 19` stopped at FreeCOM's get-drive call after 12 false passes
(EAX=000019CD). `smoke_test.py` passes on all five VMs.

On the IBM PC (8088), `log_hits` on the BIOS timer handler (F000:FEA5)
showed every tick interrupting the keyboard wait loop at FE847, 0.0549 s
apart (18.2 Hz).

Bugs found while testing:

- `bc` read one field too many and always failed.
- On 8086-class CPUs the stub reported the CPU clock as the TSC rate. Those
  cores count crystal ticks (`xt_cpu_multi`, 3 per clock on the 5150), so
  emulated times in the INT log and `log_hits` were 3 times too long.
- `monitor help` was missing `bc`, `lp`/`ll` and the `xr` options; it now
  lists them.

### 22. Naming library functions without a map

Round 3's agents asked for C runtime names (`inp_`, `outp_`, `strtoul_`)
in programs without a map. `identify_functions` names them from the
compiler's own libraries, much as IDA's FLIRT does.

**Signatures** (`libsigs.py`). It reads OMF libraries and object files:
LNAMES, SEGDEF, EXTDEF/LEXTDEF/CEXTDEF/COMDEF, PUBDEF and LPUBDEF (statics
too), LEDATA and FIXUPP. Each public or static symbol in a code segment
becomes a function: its bytes up to the next symbol, with the bytes the
linker fills in masked. Its fixups record what it calls and which variables
it uses, by name. Open Watcom's `lib286`/`lib386` DOS libraries give about
8,500 32-bit and 16,500 16-bit signatures in 3 s. They are cached in the
state directory, so later runs take a fraction of a second.

**Matching** (`libsigs.identify`):

1. Signatures whose first 6 bytes are known are looked up at every byte of
   the code. Signatures with 6 or more known bytes are tried at call
   targets only.
2. Where matches overlap, the one with the most known bytes wins. That
   drops short signatures matching inside longer functions, and tables
   (all zeros, repeated bytes) are never matched.
3. Weak matches (fewer than 16 known bytes) must come from a library that
   confident matches came from: this dropped the C++ library's string
   operators matching game code in FastDoom. Where they call something
   that has a signature, the code at the call target must match it: this
   dropped `_lfntosfn_` at a GUS driver routine.
4. Functions with the same bytes but different calls (`printf_`,
   `printf_s_`, `wprintf_s_`) are told apart by whether their calls and
   variable addresses land on what was found under those names. A remaining
   tie goes to the library the unambiguous matches came from (the
   register- rather than the stack-convention C library).
5. A found function's fixups name what it calls, even when the code there
   isn't the library's. `__CMain` names `main_`. In 32-bit code they also
   name the runtime's variables (`__psp`, `___iob`, `_errno`).
6. A module's functions stay in order, so short functions right before or
   after a found one from the same module are matched there.
7. Thin wrappers (`jmp __close_`) are named where every call they make
   goes to the function of that name.

Aliases (`malloc_`/`_nmalloc_`) are all written to the generated map, with
the plainest name shown. Every other called function is `sub_ADDRESS`.

**Bridge.** The names are written as a map in Open Watcom's format
(`identified.map` in the state directory) and loaded by `load_symbols`'
machinery, so breakpoints, disassembly, stacks and logs use them as they
would a map's. The code region is:

- 32-bit: the DPMI block holding the entry point, or the DOS block when the
  extender placed the code in conventional memory (PMODE/W).
- 16-bit: the program's DOS memory, one 64 KiB frame for the entry's code
  segment and one for each segment that far calls go to.

`libraries=` takes files and directories; the word `watcom` adds the
default libraries. Easy OMF-386 libraries (Phar Lap's format, used by the
AWE32 SDK) are read too: 16-bit record types with 32-bit fields, and
32-bit "offset" fixups.

**Accuracy against the linker maps.** An address counts as right when a
name there is the map's:

| Program | Code | Right | Wrong | Map symbols left as sub_ |
|---|---|---|---|---|
| COINS (DOS/4GW) | 16 KB | 121 | 0 | 3 |
| TOUR (DOS/32A) | 12 KB | 118 | 0 | 3 |
| SBTEST (PMODE/W, in DOS memory) | 67 KB block | 138 | 1 (`_edata`, a linker label at `___env_mask`'s address) | 3: `___GETDSEnd_` (a label), `__get_errno_ptr_`, `__full_io_exit_` |
| CAVES (16-bit, small model) | 9 KB | 67 | 0 | 6 |
| FastDoom (DOS/4GW), Open Watcom libraries | 408 KB | 343 | 1 (`_edata` again) | clib3r: 271 of its 288 symbols named |
| FastDoom with its AWE32 and GUS SDK libraries | 408 KB | 601 | 1 | clib3r 271/288, gf1_osf 136/136, pawe32 36/45 (the rest are code-range labels) |

"Right" counts addresses. In COINS, CAVES and SBTEST (checked against
the call targets in the code) every directly called function in the map
got its real name; the misses are reached otherwise (exit and errno paths). The game's own functions get `sub_`
names, and `main_` is named by `__CMain`'s call.
Identification takes under 2 s on FastDoom. The misses are mostly linker
labels (`___GETDSEnd_`, `___begtext`) and functions only called through
pointers (`__clock_init_` from the init table).

Before these steps, the first runs got the following wrong. Each fix is
above:

- `_GroupTable` matched every byte (a table).
- C++ operators matched FastDoom code.
- `printf_` showed `wprintf_s_` as an alternative.
- `cstart` from the stack-convention library was used, which lost the
  variable names.
- `main_` wasn't named: a library's own `main_` didn't match the program's.
- The AWE32 library was skipped: Easy OMF-386 wasn't read.
- AWE32 functions mismatched: their 32-bit offsets were taken as 16-bit.

**Blind exercise.** SBTEST under JemmEx again, without a map, now with
`identify_functions`. The agent:

- reproduced the bug with `watch_program`;
- named `main_`, `getenv_`, `strtoul_`, `inp_`, `outp_` and `printf_`;
- found `mov ebx,0xA` / `call strtoul_` in the `A` case of the BLASTER
  parser;
- confirmed EAX=0DCh after the call, and `outp_` to port 0E2h with
  `log_hits`;
- suggested base 16, or patching the byte at 26072 to 10h.

It was correct, in about 10 minutes with 12 tool calls (round 3 without
names: about 10 minutes, 17 calls). The agent's words: "the bug was obvious
on first read of `main_` and its callees: the strtoul base register stands
out once the call is named". It also noticed the default IRQ 7 doesn't match
the card's IRQ 5, which is true and not the planted bug.

What it found wrong, now fixed:

- **Duplicate names.** PMODE/W keeps the program file it read in the same
  DOS block as the relocated code, so heap functions were named twice: in
  the file copy and in the code. When names repeat, only the code reached
  from the entry point by direct calls and jumps is kept
  (`libsigs.reachable_span`); here 26010-2990C of the block's
  125B0-29930. This gave 136 right and 1 alias wrong.
- **Stale names.** Names stayed after the program exited and DOS reused its
  memory. Identified names now lapse when another program starts. The
  output says how long they last.
- **Strings.** Data references showed as `DATA+FFC`. Disassembly now shows
  the string an address points to (`mov eax, 0x112004 ; DATA+FFC
  "BLASTER"`), read without side effects.
- **Wording.** `wait_for_stop`'s "Stopped after the last resume, before this
  call" now reads "Already stopped (it stopped after the last resume,
  before this wait)".

### 23. Attaching to a program that is already running

`load_symbols` and `identify_functions` needed a `wait_for_program_start`
stop for 32-bit programs, but a bug often shows mid-game. For an Open
Watcom program they now find the program themselves (`_attach_watcom`):

1. Search all RAM for the startup signature (`EB xx "WATCOM"`).
2. For each hit, follow the calls and jumps the code really makes from
   there, decoded with capstone, to get the code's extent.
3. Keep the copy the CPU is running: CS:EIP or return addresses on the
   stack inside it. When the CPU is elsewhere (in DOS, or the extender), let
   it run to the next 32-bit protected-mode instruction, passing over V86
   monitor and extender code, as `wait_for_program_start` does. A CPU that
   was paused stays stopped there, in the program's code.

The result is saved like a program start (`"attached": True`).

Fixes on the way:

- **Decoding the calls.** Following raw `E8`/`E9` bytes from FastDoom's
  entry wandered through the game data (the WAD loaded above the code)
  and gave a "code" range of 144000-57F7FF. Requiring a target to be
  called twice stopped after 5 functions. Following only decoded call and
  jump instructions reached exactly the code object, 1710D4-1D4AC0 (the
  map has 171000-1D7000). Without capstone, a 64 KiB-gap rule limits the
  byte scan.
- **Jumped-to functions.** The startup code jumps to `__CMain` rather than
  calling it, so targets of the entry function's jumps count as function
  starts.
- **Stale starts.** A program start saved before the emulator was
  restarted was still used: the reset check compares the CPU's time
  counter, which had already passed the old value. Starts now also keep
  the first bytes at the entry point (or the PSP) and are dropped when
  memory no longer holds them.
- **Runtime variables.** With no extender block known, they are kept
  when they lie just above the code.

Results, with the program running and no program start seen:

| Program | Paused in | Result |
|---|---|---|
| FastDoom (DOS/4GW), `load_symbols` | DOS (real mode) | ran to `read_+E4`; map placed at 171000/1D7000, 617 of 625 calls fit |
| FastDoom, `identify_functions` | the game | 342 right, 1 alias wrong (as with a program start: 343) |
| PMGAME built with CauseWay (paging, CPL 3) | the game | 106 library functions named, `main_` found, the game loop's `kbhit_`/`getch_`/`rand_` named |

COINS, CAVES and SBTEST (also under JemmEx) give the same results as
before. `smoke_test.py` passes on all five VMs.

## Test results

Test machines, built with `-DQT=OFF -DSDL2=ON -DGDBSTUB=ON` and run headless
under Xvfb:

- IBM PC 5150, 8088 @ 4.77 MHz, CGA, boots to Cassette BASIC
- AMI 486 (OPTi 495), i486DX-33, VGA, serial mouse on COM1

| Check | Result |
|---|---|
| `smoke_test.py` on both machines | PASS |
| Write watchpoint on 0040:006C | Stops right after the BIOS timer's `inc word [6C]` on both CPUs |
| Typing into BASIC | `PRINT "Hello, World! (a+b)*c <=> [x]; 100%";7*6` came through exactly and printed `... 42` |
| F1 on the 486 | Entered AMIBIOS setup; colour VGA screenshot pixel-correct |
| CGA graphics (`SCREEN 1`) | Screenshot correct; `read_text_screen` reports a graphics mode |
| Serial mouse | Injected moves/buttons produce Microsoft-mouse packets on COM1 (e.g. `4C 0B 3C`) |
| `smoke_test.py --protected-mode` on the 486 | PASS: `CS:IP = 0020:0000` 16-bit and `CS:EIP = 0008:0000802C` 32-bit, `inc word ptr fs:[0]` / `inc dword ptr [0x806e]` decoded at the right size, `fs:0` and `0018:0000` both linear 12340, write watchpoint through FS fired, 32-bit stack at `0010:0009F000`, GDT decoded |

| FreeDOS test floppy on 386, 486, Pentium MMX | Boots to `READY` with CuteMouse on all three |
| Debugging exercises (section 9) | 3 of 3 graded correct on diagnosis, lives location and fix |
| CuteMouse end to end | Moves, buttons (serial and PS/2) and wheel (PS/2, `CTMOUSE /O`) reach INT 33h |
| Speed (BENCH) | Real time at Pentium MMX 200 with the interpreter |
| Reconnect lifecycle | Breakpoints cleared on disconnect without `hold`, kept with it; paused CPU stays paused; frames advance; run state and stop reason survive reconnects |

Not yet tested: commercial or shareware games (the downloads are blocked in
this environment), and V86 mode under EMM386.

## Known limitations

- Breakpoints and watchpoints are on linear addresses, resolved when set; one
  set through a selector whose base later changes stays at the old address.
- Watchpoints stop after the accessing instruction; on 8-bit-bus CPUs a word
  access reports its second byte's address.
- Pausing from 86Box's own UI stops request servicing (tools time out).
- One debugger client at a time; US keyboard layout.
- Interpreter only while the stub is enabled (measured real time up to a
  Pentium MMX 200).

## Next steps

1. **Report the FastDoom bug upstream**: `testdata/fastdoom-bug-report.md` is
   ready to paste (this environment can't post to that repository).
2. More AI exercises on real software, e.g. a bug hunt without a map.
3. **More real games:** other open-source DOS games build with Open Watcom.
4. **Read LE/LX object tables** from the program file to place objects
   without a program-start stop.
5. **Speed:** let the dynamic recompiler run while no breakpoints,
   watchpoints or stepping are active.
6. Offer the emulator changes upstream (86Box/86Box).
