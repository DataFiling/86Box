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

Not yet tested: a real DOS game, and a DOS mouse driver end to end.

## Known limitations

- Protected mode: no segment bases from the stub, so `SEG:OFF` only works in
  real/V86 mode. Use linear addresses for DOS extenders.
- Watchpoints stop after the accessing instruction; on 8-bit-bus CPUs a word
  access reports its second byte's address.
- Pausing from 86Box's own UI stops request servicing (tools time out).
- One debugger client at a time; US keyboard layout.
- Interpreter only while the stub is enabled (fine up to ~486 speeds).

## Next steps

1. **Real-game test bed:** FreeDOS plus freeware games (text-mode, VGA, and
   one using the mouse), with an automated boot-and-play check. This also
   validates mouse input against a DOS driver.
2. **DOS awareness:** find the running program (PSP/MCB chain), break on
   program start, log `INT 21h`/`INT 10h` calls with names, memory diffing to
   find variables, save/restore machine state.
3. **Protected mode:** segment bases/descriptors through the stub, so
   `SEG:OFF` and 16/32-bit disassembly work under DOS extenders.
4. **Autonomous loop:** an agent that runs a game, detects hangs or crashes,
   and diagnoses them.
5. Offer the emulator fixes upstream (86Box/86Box) as separate pull requests.
