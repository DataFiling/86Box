# 86Box AI debugger bridge

An [MCP](https://modelcontextprotocol.io) server that lets an AI client
(Claude Code, Claude Desktop) attach to a running 86Box and debug DOS-era
software live. It can pause, step, set breakpoints and watchpoints, read and
patch memory, disassemble, see the screen in any video mode, and type and use
the mouse, in real, V86 and protected mode (including 16-bit protected mode).
It knows about DOS: the programs in memory, a log of the DOS, DPMI, BIOS and
mouse calls a program makes (with file names and results), stopping at a
program's first instruction (also the 32-bit entry of DOS/4GW games), and
cheat-finder style searches for variables such as lives or score.

```
Claude ──MCP (stdio)──> server.py ──GDB remote protocol (TCP 12345)──> 86Box gdbstub
```

It covers steps 1 to 5 of a larger plan (see [Roadmap](#roadmap)).

## 1. Build 86Box with the GDB stub

The stub is a build-time option. It forces the CPU interpreter, since the
dynamic recompiler is incompatible with breakpoints, so the guest runs slower.
That is fine for DOS-era machines.

```sh
# Debian/Ubuntu dependencies: see .github/workflows/cmake_linux.yml
cmake --preset regular -DGDBSTUB=ON          # add -DQT=OFF -DSDL2=ON for the SDL frontend
cmake --build build/regular
```

The stub listens on TCP port 12345. To change it, set `gdbstub_port = N`
under `[General]` in the VM's `86box.cfg`. **With the stub enabled, 86Box
starts with the CPU paused**; the bridge's `resume` / `run_for` tools start it.
When the debugger disconnects, the CPU runs freely again.

## 2. Install and register the bridge

```sh
pip install -r tools/ai-debugger/requirements.txt

# Claude Code (run from anywhere):
claude mcp add 86box -- python3 /absolute/path/to/86Box/tools/ai-debugger/server.py
```

Claude Desktop (`~/.config/Claude/claude_desktop_config.json` on Linux):

```json
{
  "mcpServers": {
    "86box": {
      "command": "python3",
      "args": ["/absolute/path/to/86Box/tools/ai-debugger/server.py"],
      "env": { "BOX86_GDB_PORT": "12345" }
    }
  }
}
```

`BOX86_GDB_HOST` / `BOX86_GDB_PORT` select the emulator. The bridge connects
on first use and reconnects if 86Box is restarted.

## 3. Check it works

Start 86Box on any PC/XT/AT machine with no boot disk, then:

```sh
python3 tools/ai-debugger/smoke_test.py                    # prints RESULT: PASS
python3 tools/ai-debugger/smoke_test.py --protected-mode   # 386+ machines: also protected-mode checks
```

`--protected-mode` loads `testdata/pm_probe.bin` (source alongside), which
switches to protected mode and loops through 32-bit and 16-bit code segments,
then hard-resets the machine at the end. The basic test was verified on an
IBM PC 5150 (8088) and an AMI 486 (OPTi 495); the protected-mode part on the
486.

For testing on real DOS software, [testdata/](testdata/README.md) builds a
FreeDOS boot floppy with test games that have planted bugs, and configs for
386, 486 and Pentium machines.

See [DEVLOG.md](DEVLOG.md) for design decisions, test results and next steps.

## Without an MCP client

`run_tool.py` runs one tool per command, for scripts, shells and AI agents
that don't speak MCP:

```sh
python3 tools/ai-debugger/run_tool.py --port 12345 get_state
python3 tools/ai-debugger/run_tool.py read_memory address=ds:0026 length=16
python3 tools/ai-debugger/run_tool.py press_keys keys="up up space"
python3 tools/ai-debugger/run_tool.py screenshot      # prints the PNG's path
python3 tools/ai-debugger/run_tool.py help            # the tools, and how they fit together
python3 tools/ai-debugger/run_tool.py help scan_next  # one tool's parameters and description
```

Values are JSON when they parse as JSON, else strings (where `\n`, `\t` and
`\\` are unescaped, so `type_text text="DIR\n"` presses Enter); integer
parameters also take `0x` hex. Each command connects
for just that call and asks the stub to *hold*: a CPU left paused stays
paused between commands (and keeps its breakpoints), while one left running
keeps running. The bridge's modules (`gdb_rsp.py`, `dos.py`, `pcinput.py`)
can also be imported directly from Python.

## Tools

| Area | Tools |
|---|---|
| Execution | `status`, `pause`, `resume`, `run_for`, `wait_for_stop`, `step`, `step_over`, `step_out`, `run_until`, `hard_reset` |
| DOS programs | `dos_memory_map` (MCB chain, programs, which one is running), `wait_for_program_start` (stop at a program's first instruction; `protected_mode` for a DOS/4GW-style program's 32-bit entry; `command` types the command that starts it) |
| Interrupt calls | `log_interrupts` + `read_interrupt_log` (INT 21h DOS, 31h DPMI, 10h video, 33h mouse... with decoded arguments, file names, buffers and results), `catch_interrupt` (stop on a call or its return), `clear_interrupt_catches` |
| Symbols | `load_symbols` (an Open Watcom linker map, placed automatically for 16-bit programs and DOS/4GW-style objects, checked against the code in memory; `map_path=none` unloads), `lookup_symbol` (`module!name` for duplicated statics); names then work as addresses (`set_breakpoint address=main_`) and appear in disassembly, the CPU state, stacks, the INT log and watch_program |
| Addresses | `what_is` (symbol, PC memory area, DOS block and owner, DPMI block, the function it is in) |
| Code | `find_references` (instructions that use an address or call/jump to it), `log_hits` (run and record each time an address executes: registers, memory, callers, without stopping for long), `patch_code` (jmp/call/nop/ret, padded to whole instructions; `undo`) |
| Unattended runs | `watch_program` (start a program, watch it, and report whether it exited, crashed, hung or waits for input, with evidence) |
| Finding variables | `scan_memory` + `scan_next` (value, changed, decreased, -1...), `snapshot_memory` + `diff_memory`, `restore_memory` |
| State | `get_state` (registers, CPU mode, next instructions), `set_register`, `read_stack` |
| Protected mode | `get_segments` (descriptor caches, code/stack size, CPL, GDTR/IDTR/LDTR/TR), `read_descriptor_table` (decoded GDT, LDT or IDT entries) |
| Memory | `read_memory` (hex/words/dwords/text), `write_memory`, `search_memory` (hex with `??` wildcards, or quoted text; all RAM by default, without side effects), `disassemble` |
| Break/watch | `set_breakpoint`, `set_watchpoint` (write/read/access, any length), `clear_breakpoint`, `list_breakpoints` |
| Screen | `screenshot` (PNG of the displayed frame, any video mode; optional `downscale`, `save_path`), `read_text_screen` (BIOS mode, page, cursor, CP437 text; `plain` for the text only) |
| Input | `press_keys` (`"enter"`, `"ctrl+c"`, `"up up space"`), `type_text`, `key_down`/`key_up` (hold keys), `mouse_move`, `mouse_click`, `mouse_buttons` (drag), `mouse_scroll` |
| PC/DOS | `read_interrupt_vectors`, `io_read`, `io_write` |

Addresses are `SEG:OFF` (`1234:0100`, `ds:si`, `es:di+10`, `cs:eip`) or
linear (`B8000`, `0x12345`, `12345h`). Bare numbers are hexadecimal, as in
DEBUG.COM. `SEG:OFF` works in every CPU mode: segment registers use their
cached base, and in protected mode a numeric selector (`0028:00401000`) is
looked up in the GDT or LDT. Addresses are resolved when the tool is called,
in the CPU's current mode, and tool output says how (e.g. `linear 00012340
(protected-mode selector, base 00012340)`). Disassembly is 16- or 32-bit to
match the segment.

Example prompts:

- "GAME.EXE says it can't load its data. Log the DOS calls while it starts and tell me what fails."
- "Stop GAME at its first instruction, then find where it keeps the score."
- "Run GAME with watch_program and tell me why it hangs."

- "Run the game for 5 seconds, then tell me what code it's spending its time in."
- "Lives are 3. Search memory for them, I'll lose a life, then narrow it down
  and set a write watchpoint to find the code that decrements lives."
- "It hangs on the title screen. Pause it and work out what it's waiting for."
- "Get past the setup menu by picking Sound Blaster on port 220, then show me the title screen."

## Known limitations

- **Protected mode:** breakpoints and watchpoints are on linear addresses
  (virtual ones when paging is on), so one set on a selector whose base later
  changes stays at the old place. DOS-extender support is generic: DPMI calls
  are logged and decoded, and `wait_for_program_start(protected_mode=true)`
  finds the 32-bit entry of extenders that allocate the program's memory
  through DPMI (DOS/4GW does; tested with it only). It doesn't read the
  program's LE/LX object table.
- **No machine save states.** 86Box can't save and restore a running
  machine's device state, so the bridge can't either; `restore_memory` puts
  back memory from a snapshot (a variable, a patched routine), which is not
  the same as rewinding the machine.
- `INT n` calls are logged and caught when made with an `INT` instruction
  (as DOS programs call DOS, the BIOS and drivers); hardware interrupts and
  calls made by jumping to a handler (`pushf; call far`) are not. A call
  catch is checked before each instruction except the one execution resumes
  at, so resuming on a caught `INT` runs it.
- The interrupt log keeps the last 4096 calls (identical back-to-back calls
  count as one); value scans keep their state on disk (in the system temp
  directory), one scan at a time per emulator.
- With the stub enabled, 86Box runs the CPU interpreter only. That still
  keeps real time for a Pentium MMX 200 under a CPU-bound load on a modern
  host (measurements in [testdata/README.md](testdata/README.md)); faster
  emulated CPUs may fall behind.
- Input tools resume a paused CPU, since the guest must run to read input.
  The keyboard uses the US layout. Mouse movement is relative (mickeys), as
  real mice are; take a screenshot to see where the pointer went.
- Mouse input reaches the emulated serial, PS/2 and bus mice and has been
  checked end to end with CuteMouse. The wheel needs a wheel mouse in the VM
  (PS/2 mouse `buttons = 4` or more, i.e. "Wheel" in Settings) and a driver
  that looks for one (`CTMOUSE /O`).
- Watchpoints stop *after* the accessing instruction. On 8-bit-bus CPUs a
  word access reports its second byte's address.
- Pausing 86Box from its own UI stops the stub from servicing requests, so
  tools time out.
- Only one debugger client at a time (the stub's default).

## Emulator changes

All of these only take effect in builds with `-DGDBSTUB=ON`.

- **Watchpoints work on all CPUs** (`src/mem/mem.c`, `src/gdbstub.c`). They
  were silently skipped on 8086-class CPUs, whose `read_mem_*`/`write_mem_*`
  accessors had no hook, and on 486+ CPUs whenever a page was in the MMU lookup
  cache. Watched pages are now kept out of that cache.
- **New stub monitor commands** (`monitor help` in GDB lists them):
  `fi`/`fb` freeze and stream the last displayed frame, `kd`/`ku` press and
  release keys by scan code (`keyboard_input_injected()`, which works even
  when the keyboard requires capture), `mm`/`mb` move the mouse, turn the
  wheel (`mouse_wheel_clicks()`) and set its buttons, `bl` lists breakpoints
  and watchpoints, `state` says whether the CPU is running, and `hold` keeps
  a paused CPU paused, with its breakpoints, when the last client
  disconnects (and stops a new connection from pausing a running CPU). The
  stop reason is kept while the CPU stays stopped, so a client that
  reconnects learns why it stopped. `src/video/video.c` hands
  each completed frame to the stub, which keeps a copy.
- **Clean disconnects.** When the last client leaves (and isn't holding),
  its breakpoints and watchpoints are removed so the guest can't stop with
  nobody attached; injected keys and mouse buttons are always released.
- **Injected mouse input is delivered without a host mouse grab.** Serial,
  PS/2 and bus mice only accept input while the host mouse is captured; a
  `mouse_injected` flag, set by `mm`/`mb` and cleared when the debugger
  disconnects, lets it through without grabbing the user's real mouse.
- **Software interrupt log and catchpoints** (`src/gdbstub.c`; the `INT n`
  implementations in `src/cpu/x86_ops_int.h`, `808x.c` and `vx0.c` call
  `gdbstub_int()`). Monitor commands `tv`/`ts`/`tl`/`tc` log calls to chosen
  vectors with their registers, the buffers DS:(E)DX, DS:(E)SI and ES:(E)DI
  point to, and the results when they return (found by return address and
  stack pointer); `ca`/`cx` stop on calls (before the `INT` runs) or returns;
  `xr` stops when execution enters a linear range; `mr` reads RAM/ROM without
  side effects or faults (skipping device memory such as VGA), for memory
  snapshots. Costs one test per instruction while nothing is logged.
- **EFLAGS reads and writes respect lazy flags.** The interpreter computes
  arithmetic flags lazily; the stub read stale ZF/CF/... after e.g. a `cmp`,
  and a written EFLAGS could be overridden by the pending lazy state.
- **`TCP_NODELAY` on stub connections.** Replies are sent in small pieces,
  which Nagle's algorithm held back for ~85 ms each; requests now take ~1 ms.
- **`sg` monitor command** reports the segment descriptor caches (selector,
  base, limit, access, flags), GDTR, IDTR, LDTR, TR, and the CPU's current
  code/stack size and CPL, which the GDB register packets don't carry.
- **Fixes to existing stub behaviour** found by review: memory reads that hit
  a page fault returned corrupt replies and writes reported success (reads
  now return the bytes before the fault, writes an error), debugger accesses
  overwrote the guest's CR2, watch addresses at or above 2 GiB indexed out of
  bounds, a quick reconnect could have its new connection closed (descriptor
  reuse race), and sends to a departed client could raise SIGPIPE. 8086-class
  instruction fetches no longer trigger read watchpoints.

## Roadmap

1. **Bridge (done).** Execution control, memory, breakpoints, watchpoints, text screen.
2. **Screen and input (done):** screenshots in any video mode, keyboard and mouse.
3. **Protected-mode basics (done):** segment bases, selector lookup, 16/32-bit
   code and stack, descriptor tables.
4. **Test setup across three eras (done):** FreeDOS on 386, 486 and Pentium
   machines with test games that have planted bugs, AI debugging exercises
   graded against ground truth, and a speed measurement.
5. **DOS and extender awareness (done):** the running program (MCB chain,
   PSP, load segment), stopping at program start (real mode and DOS/4GW
   32-bit entry), decoded `INT 21h`/`31h`/`10h`/`33h`... call logs and
   catchpoints, memory snapshots, diffs and value scans. Machine save/restore
   is not possible (86Box has no save states).
6. **Speed:** not needed for Pentium-era games (the interpreter keeps real
   time at Pentium MMX 200). If later targets need it: let the dynamic
   recompiler run while no breakpoints, watchpoints or stepping are active.
7. **Autonomous loop:** an agent that plays/boots a game, detects hangs and
   crashes (stuck loops, invalid opcodes, exceptions), and diagnoses them.
