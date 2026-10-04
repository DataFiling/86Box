# 86Box AI debugger bridge

An [MCP](https://modelcontextprotocol.io) server that lets an AI client
(Claude Code, Claude Desktop) attach to a running 86Box and debug DOS-era
software live. It can pause, step, set breakpoints and watchpoints, read and
patch memory, disassemble, and read the text screen.

```
Claude ──MCP (stdio)──> server.py ──GDB remote protocol (TCP 12345)──> 86Box gdbstub
```

It is step 1 of a larger plan (see [Roadmap](#roadmap)).

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
python3 tools/ai-debugger/smoke_test.py      # prints RESULT: PASS
```

It was verified on an IBM PC 5150 (8088) and an AMI 486 (OPTi 495).

## Tools

| Area | Tools |
|---|---|
| Execution | `status`, `pause`, `resume`, `run_for`, `wait_for_stop`, `step`, `step_over`, `run_until`, `hard_reset` |
| State | `get_state` (registers, CPU mode, next instructions), `set_register`, `read_stack` |
| Memory | `read_memory` (hex/words/dwords/text), `write_memory`, `search_memory` (hex with `??` wildcards, or text), `disassemble` |
| Break/watch | `set_breakpoint`, `set_watchpoint` (write/read/access, any length), `clear_breakpoint`, `list_breakpoints` |
| PC/DOS | `read_text_screen` (BIOS mode, page, cursor, CP437 text), `read_interrupt_vectors`, `io_read`, `io_write` |

Addresses are `SEG:OFF` (`1234:0100`, `ds:si`, `es:di+10`, `cs:ip`) or
linear (`B8000`, `0x12345`, `12345h`). Bare numbers are hexadecimal, as in
DEBUG.COM.

Example prompts:

- "Run the game for 5 seconds, then tell me what code it's spending its time in."
- "Lives are 3. Search memory for them, I'll lose a life, then narrow it down
  and set a write watchpoint to find the code that decrements lives."
- "It hangs on the title screen. Pause it and work out what it's waiting for."

## Known limitations

- **Protected mode:** the stub does not expose segment bases, so `SEG:OFF`
  is only accepted in real/V86 mode. For flat DOS extenders (DOS/4GW etc.)
  use linear addresses; disassembly assumes 32-bit code in protected mode.
- **No screenshots or keyboard/mouse input yet:** graphics-mode screens can't
  be read, and the AI can't press keys. See the roadmap.
- Watchpoints stop *after* the accessing instruction. On 8-bit-bus CPUs a
  word access reports its second byte's address.
- Pausing 86Box from its own UI stops the stub from servicing requests, so
  tools time out.
- Only one debugger client at a time (the stub's default).

## Emulator changes

`src/mem/mem.c` and `src/gdbstub.c` were changed so that watchpoints work on
all CPUs. Previously they were silently skipped on 8086-class CPUs (whose
`read_mem_*`/`write_mem_*` accessors had no hook) and on 486+ CPUs whenever a
page was in the MMU lookup cache. Watched pages are now kept out of that cache.
This only affects builds with `-DGDBSTUB=ON`.

## Roadmap

1. **Bridge (this).** Execution control, memory, breakpoints, watchpoints, text screen.
2. **Emulator-side additions to the stub's monitor commands:** screenshots
   (graphics modes), keyboard/mouse injection, segment bases/descriptors for
   protected mode, `INT xx` call tracing, and save/restore of machine state.
3. **DOS awareness:** find the running program (PSP, MCB chain, load
   segment), break on program start, named `INT 21h` call logs, and memory
   diffing for finding variables (lives, score, timers).
4. **Autonomous loop:** an agent that plays/boots a game, detects hangs and
   crashes (stuck loops, invalid opcodes, exceptions), and diagnoses them.
