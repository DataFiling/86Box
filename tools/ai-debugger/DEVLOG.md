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
- No DOS-extender awareness yet (load address, DPMI calls). V86 mode is
  handled through the same descriptor caches but hasn't been tested under
  EMM386 yet.
- Watchpoints stop after the accessing instruction; on 8-bit-bus CPUs a word
  access reports its second byte's address.
- Pausing from 86Box's own UI stops request servicing (tools time out).
- One debugger client at a time; US keyboard layout.
- Interpreter only while the stub is enabled (measured real time up to a
  Pentium MMX 200).

## Next steps

1. **DOS and extender awareness:**
   - find the running program (PSP/MCB chain, extender load address);
   - break on program start;
   - log `INT 21h`/`INT 10h`/`INT 31h` calls with names;
   - memory snapshot and diff to find variables (all three exercise agents
     asked for this, since searching for a value and then re-searching by
     hand is slow);
   - save and restore machine state.
2. **Real games:** run shareware titles (e.g. Commander Keen, Doom) once
   network access allows it, or with images supplied by the user.
3. **Autonomous loop:** an agent that runs a game, detects hangs or crashes,
   and diagnoses them.
4. **Speed:** let the dynamic recompiler run while no breakpoints,
   watchpoints or stepping are active. Only needed for targets faster than a
   Pentium MMX 200.
5. Offer the emulator fixes upstream (86Box/86Box) as separate pull requests.
