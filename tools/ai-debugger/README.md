# 86Box AI debugger bridge

An [MCP](https://modelcontextprotocol.io) server that lets an AI client
(Claude Code, Claude Desktop) attach to a running 86Box and debug DOS-era
software live. It can pause, step, set breakpoints and watchpoints, read and
patch memory, disassemble, see the screen in any video mode, and type and use
the mouse, in real, V86 and protected mode (including 16-bit protected mode).

```
Claude ──MCP (stdio)──> server.py ──GDB remote protocol (TCP 12345)──> 86Box gdbstub
```

It covers steps 1 to 3 of a larger plan (see [Roadmap](#roadmap)).

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
then hard-resets the machine at the end. It was verified on an IBM PC 5150
(8088) and an AMI 486 (OPTi 495).

See [DEVLOG.md](DEVLOG.md) for design decisions, test results and next steps.

## Tools

| Area | Tools |
|---|---|
| Execution | `status`, `pause`, `resume`, `run_for`, `wait_for_stop`, `step`, `step_over`, `run_until`, `hard_reset` |
| State | `get_state` (registers, CPU mode, next instructions), `set_register`, `read_stack` |
| Protected mode | `get_segments` (descriptor caches, code/stack size, CPL, GDTR/IDTR/LDTR/TR), `read_descriptor_table` (decoded GDT, LDT or IDT entries) |
| Memory | `read_memory` (hex/words/dwords/text), `write_memory`, `search_memory` (hex with `??` wildcards, or text), `disassemble` |
| Break/watch | `set_breakpoint`, `set_watchpoint` (write/read/access, any length), `clear_breakpoint`, `list_breakpoints` |
| Screen | `screenshot` (PNG of the displayed frame, any video mode; optional `downscale`, `save_path`), `read_text_screen` (BIOS mode, page, cursor, CP437 text) |
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

- "Run the game for 5 seconds, then tell me what code it's spending its time in."
- "Lives are 3. Search memory for them, I'll lose a life, then narrow it down
  and set a write watchpoint to find the code that decrements lives."
- "It hangs on the title screen. Pause it and work out what it's waiting for."
- "Get past the setup menu by picking Sound Blaster on port 220, then show me the title screen."

## Known limitations

- **Protected mode:** breakpoints and watchpoints are on linear addresses
  (virtual ones when paging is on), so one set on a selector whose base later
  changes stays at the old place. The bridge doesn't yet know about DOS
  extenders themselves (where the program was loaded, its DPMI calls).
- With the stub enabled, 86Box runs the CPU interpreter only; Pentium-era
  games may run slowly. Measured in the next step.
- Input tools resume a paused CPU, since the guest must run to read input.
  The keyboard uses the US layout. Mouse movement is relative (mickeys), as
  real mice are; take a screenshot to see where the pointer went.
- Mouse input reaches the emulated serial, PS/2 and bus mice; it has not yet
  been checked end to end against a DOS mouse driver.
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
  release keys by scan code, and `mm`/`mb` move the mouse and set its buttons.
  `src/video/video.c` hands each completed frame to the stub, which copies it
  only while a client is connected.
- **Injected mouse input is delivered without a host mouse grab.** Serial,
  PS/2 and bus mice only accept input while the host mouse is captured; a
  `mouse_injected` flag, set by `mm`/`mb` and cleared when the debugger
  disconnects, lets it through without grabbing the user's real mouse.
- **`TCP_NODELAY` on stub connections.** Replies are sent in small pieces,
  which Nagle's algorithm held back for ~85 ms each; requests now take ~1 ms.
- **`sg` monitor command** reports the segment descriptor caches (selector,
  base, limit, access, flags), GDTR, IDTR, LDTR, TR, and the CPU's current
  code/stack size and CPL, which the GDB register packets don't carry.

## Roadmap

1. **Bridge (done).** Execution control, memory, breakpoints, watchpoints, text screen.
2. **Screen and input (done):** screenshots in any video mode, keyboard and mouse.
3. **Protected-mode basics (done):** segment bases, selector lookup, 16/32-bit
   code and stack, descriptor tables.
4. **Test setup across three eras:** FreeDOS on a 286/386 (early real mode),
   a 486 (DOS-extender games) and a Pentium machine, one freeware or shareware
   game each, an automated boot-and-play check, and a speed measurement on the
   Pentium machine.
5. **DOS and extender awareness:** find the running program (PSP, MCB chain,
   load address), break on program start, named `INT 21h`/`INT 31h` call
   logs, memory diffing for finding variables, and save/restore of machine state.
6. **Speed for Pentium-era games, if step 4 shows it's needed:** let the
   dynamic recompiler run while no breakpoints, watchpoints or stepping are active.
7. **Autonomous loop:** an agent that plays/boots a game, detects hangs and
   crashes (stuck loops, invalid opcodes, exceptions), and diagnoses them.
