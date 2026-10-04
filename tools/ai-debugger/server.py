"""MCP server exposing a running 86Box (built with -DGDBSTUB=ON) as debugger
tools for an AI client such as Claude Code or Claude Desktop.

Run:  python3 server.py           (stdio transport)
Env:  BOX86_GDB_HOST (default 127.0.0.1), BOX86_GDB_PORT (default 12345)
"""

import functools
import os
import threading
import time

import anyio
from mcp.server.fastmcp import FastMCP, Image

import dos
import pcinput
from gdb_rsp import (BP_HARDWARE, WP_ACCESS, WP_READ, WP_WRITE, GdbClient, GdbError)

INSTRUCTIONS = """\
Tools for live-debugging DOS-era software running inside the 86Box PC emulator.

Addresses: "SEG:OFF" (e.g. "1234:0100", "ds:si", "es:di+10", "cs:eip") or linear
addresses ("B8000", "0x12345", "12345h"). Bare numbers are HEX, as in DEBUG.COM.
SEG:OFF works in every CPU mode: segment registers use their cached base, and in
protected mode a numeric selector (e.g. "0028:00401000") is looked up in the
GDT/LDT. Breakpoints and watchpoints are set on the resulting linear address
(with paging on, that is the virtual linear address). Disassembly picks 16- or
32-bit code from the segment; get_segments and read_descriptor_table show the
descriptors (useful under DOS extenders such as DOS/4GW).

The emulator starts PAUSED when the debugger is enabled: call `resume` to boot it.
Most inspection tools work while the guest is running, but registers are only
meaningful when paused. Typical loop: resume -> run_for / wait_for_stop ->
get_state -> disassemble / read_memory -> set breakpoints/watchpoints -> resume.
Find a variable (e.g. lives) by search_memory for its value, change it in game,
search again, then set_watchpoint on the surviving address to find the code
that modifies it.

`screenshot` shows the emulated display (any video mode). Keyboard and mouse
tools (press_keys, type_text, key_down/key_up, mouse_*) resume the CPU if it
is paused, since the guest has to run to receive input; they leave it running.
"""

mcp = FastMCP("86box-debugger", instructions=INSTRUCTIONS)

_client = None
_client_lock = threading.Lock()
_points = {}  # (kind, linear) -> {"kind":..., "address":..., "length":..., "expr":...}

KIND_NAMES = {BP_HARDWARE: "breakpoint", WP_WRITE: "write-watch", WP_READ: "read-watch",
              WP_ACCESS: "access-watch"}


def client():
    global _client
    with _client_lock:
        if _client is None or not _client.connected:
            host = os.environ.get("BOX86_GDB_HOST", "127.0.0.1")
            port = int(os.environ.get("BOX86_GDB_PORT", "12345"))
            c = GdbClient(host, port)
            try:
                c.connect()
            except OSError as e:
                raise GdbError(
                    "Cannot reach the 86Box GDB stub at %s:%d (%s). Start 86Box built with "
                    "-DGDBSTUB=ON; the port is set by gdbstub_port in 86box.cfg." % (host, port, e))
            _client = c
            _points.clear()
        return _client


def tool(fn):
    """Register a blocking function as an MCP tool, run off the event loop."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

    return mcp.tool()(wrapper)


# ---- helpers ------------------------------------------------------------

def _regs():
    return client().read_registers()


def _resolve(address, regs):
    return dos.resolve(address, regs, client())


def _cs_loc(regs, linear):
    """Label code at `linear` as CS:offset when it lies inside CS."""
    try:
        cs = dos.seg_cache(regs, "cs")
    except ValueError:
        return None
    if 0 <= linear - cs["base"] <= cs["limit"]:
        return dos.Loc(linear, cs["sel"], cs["base"], dos.code_bits(regs))
    return None


def _code_at(regs, linear, count):
    c = client()
    data = c.read_memory(linear, count * 15)
    return dos.disassemble(data, linear, dos.code_bits(regs), _cs_loc(regs, linear), count)


def _state_report(stop=None, code_lines=6):
    c = client()
    regs = _regs()
    parts = []
    if stop is not None:
        why = {"breakpoint": "hit breakpoint", "watchpoint": "watchpoint triggered",
               "pause": "paused", "trap": "stopped (single step)"}.get(stop.reason, stop.reason)
        if stop.reason == "watchpoint":
            why += " (%s access at linear %08X)" % (stop.watch_kind, stop.watch_addr)
        parts.append("STOP: " + why)
    parts.append(dos.format_registers(regs))
    parts.append("Next instructions:\n" + _code_at(regs, regs["eip"], code_lines))
    if c.running:
        parts.insert(0, "NOTE: CPU is running; register values are a moving snapshot.")
    return "\n\n".join(parts)


# ---- connection / execution control ---------------------------------------

@tool
def status() -> str:
    """Connect if needed and report whether the CPU is running, plus current state."""
    c = client()
    return ("CPU is RUNNING" if c.running else "CPU is PAUSED") + "\n\n" + _state_report()


@tool
def pause() -> str:
    """Pause the emulated CPU and show registers and the next instructions."""
    c = client()
    if not c.running:
        return "Already paused.\n\n" + _state_report()
    stop = c.pause()
    return _state_report(stop)


@tool
def resume() -> str:
    """Resume execution and return immediately. Use wait_for_stop or run_for afterwards."""
    c = client()
    c.resume()
    return "Resumed."


@tool
def run_for(seconds: float = 2.0) -> str:
    """Resume, let the guest run for up to `seconds` of host time, then pause.

    Returns early if a breakpoint or watchpoint is hit."""
    c = client()
    if not c.running:
        c.resume()
    stop = c.wait_stop(timeout=seconds)
    if stop is None:
        stop = c.pause()
        return "Ran for %.1fs without hitting a breakpoint.\n\n%s" % (seconds, _state_report(stop))
    return _state_report(stop)


@tool
def wait_for_stop(timeout_seconds: float = 30.0) -> str:
    """Wait (while running) until a breakpoint/watchpoint hits. Does not pause on timeout."""
    c = client()
    if not c.running:
        return "CPU is not running.\n\n" + _state_report()
    stop = c.wait_stop(timeout=timeout_seconds)
    if stop is None:
        return "No stop within %.1fs; CPU still running." % timeout_seconds
    return _state_report(stop)


@tool
def step(count: int = 1) -> str:
    """Single-step `count` instructions (max 1000), following calls and interrupts."""
    c = client()
    if c.running:
        c.pause()
    count = max(1, min(count, 1000))
    trace = []
    stop = None
    for _ in range(count):
        if count > 1:
            regs = _regs()
            trace.append(_code_at(regs, regs["eip"], 1))
        stop = c.step()
        if stop is None:
            return "Step timed out (is the emulator paused from its own UI?)"
    out = _state_report(stop)
    if trace:
        out = "Executed:\n" + "\n".join(trace[-50:]) + "\n\n" + out
    return out


@tool
def step_over(timeout_seconds: float = 10.0) -> str:
    """Execute one instruction, running CALL/INT/LOOP/REP to completion instead of stepping into them."""
    c = client()
    if c.running:
        c.pause()
    regs = _regs()
    bits = dos.code_bits(regs)
    data = c.read_memory(regs["eip"], 15)
    insn = None
    if dos.capstone is not None:
        md = dos.capstone.Cs(dos.capstone.CS_ARCH_X86,
                             dos.capstone.CS_MODE_16 if bits == 16 else dos.capstone.CS_MODE_32)
        insn = next(md.disasm(data, 0), None)
    over = insn is not None and (insn.mnemonic.startswith(("call", "int", "loop")) or
                                 insn.mnemonic.split(" ")[0] in ("rep", "repe", "repne", "repz", "repnz"))
    if not over:
        return _state_report(c.step())
    target = regs["eip"] + insn.size
    return _run_to(target, timeout_seconds)


def _run_to(linear, timeout):
    c = client()
    temp = (BP_HARDWARE, linear) not in _points
    if temp:
        c.set_point(BP_HARDWARE, linear)
    try:
        c.resume()
        stop = c.wait_stop(timeout=timeout)
        if stop is None:
            stop = c.pause()
            return "Target %08X not reached within %.1fs; paused.\n\n%s" % (linear, timeout, _state_report(stop))
        return _state_report(stop)
    finally:
        if temp:
            c.clear_point(BP_HARDWARE, linear)


@tool
def run_until(address: str, timeout_seconds: float = 30.0) -> str:
    """Resume until execution reaches `address` (temporary breakpoint), or pause on timeout.

    The address is resolved now, in the current CPU mode: a protected-mode
    SEL:OFF given while still in real mode is taken as a real-mode segment."""
    c = client()
    if c.running:
        c.pause()
    regs = _regs()
    loc = _resolve(address, regs)
    return "Target %s = %s.\n\n%s" % (address, dos.describe(loc, regs), _run_to(loc.linear, timeout_seconds))


@tool
def hard_reset() -> str:
    """Hard-reset the emulated machine (like pressing the reset button)."""
    client().monitor("r")
    return "Machine reset."


# ---- registers / memory -----------------------------------------------------

@tool
def get_state(instructions: int = 8) -> str:
    """Show registers, CPU mode and the next instructions at CS:IP."""
    return _state_report(code_lines=max(1, min(instructions, 50)))


@tool
def set_register(name: str, value: str) -> str:
    """Set a register (eax..edi, eip/ip, eflags, cs, ss, ds, es, fs, gs). Value is hex.

    eip/ip is the offset within CS, as shown in CS:EIP; "pc" sets the linear
    address instead. Loading a segment register in protected mode loads the
    descriptor for that selector."""
    c = client()
    if c.running:
        return "Pause the CPU first."
    regs = _regs()
    name = name.lower()
    val = dos.parse_term(value, regs)
    if name in ("ip", "eip"):
        c.write_register("eip", (dos.seg_cache(regs, "cs")["base"] + val) & 0xFFFFFFFF)
    elif name == "pc":
        c.write_register("eip", val & 0xFFFFFFFF)
    else:
        c.write_register(name, val)
    return _state_report()


@tool
def read_memory(address: str, length: int = 128, format: str = "hex") -> str:
    """Read guest memory. format: hex (dump), words, dwords, text (CP437)."""
    c = client()
    loc = _resolve(address, _regs())
    linear = loc.linear
    length = max(1, min(length, 65536))
    data = c.read_memory(linear, length)
    if format == "text":
        return data.decode("cp437", errors="replace")
    if format in ("words", "dwords"):
        w = 2 if format == "words" else 4
        vals = [int.from_bytes(data[i:i + w], "little") for i in range(0, len(data) - w + 1, w)]
        fmt = "%04X" if w == 2 else "%08X"
        return "\n".join("%08X: " % (linear + i * 16) + " ".join(fmt % v for v in vals[i * 16 // w:(i + 1) * 16 // w])
                         for i in range((len(vals) * w + 15) // 16))
    return dos.hexdump(data, linear, loc)


@tool
def write_memory(address: str, hex_bytes: str) -> str:
    """Write bytes (hex string, e.g. "90 90" or "EB05") to guest memory."""
    c = client()
    linear = _resolve(address, _regs()).linear
    data = bytes.fromhex(hex_bytes.replace(" ", ""))
    c.write_memory(linear, data)
    return "Wrote %d bytes at linear %08X." % (len(data), linear)


@tool
def search_memory(pattern: str, start: str = "0", end: str = "110000", as_text: bool = False,
                  max_results: int = 64) -> str:
    """Search guest memory for a byte pattern (hex, "??" wildcard) or text.

    Default range covers the first 1MB + HMA, i.e. all real-mode memory."""
    c = client()
    regs = _regs()
    lo, hi = _resolve(start, regs).linear, _resolve(end, regs).linear
    if as_text:
        needle = [b for b in pattern.encode("cp437")]
    else:
        toks = pattern.replace(",", " ").split()
        if len(toks) == 1 and len(toks[0]) > 2:
            toks = [toks[0][i:i + 2] for i in range(0, len(toks[0]), 2)]
        needle = [None if t in ("??", "?") else int(t, 16) for t in toks]
    if not needle:
        return "Empty pattern."
    hits = []
    block = 0x10000
    overlap = len(needle) - 1
    addr = lo
    while addr < hi and len(hits) < max_results:
        n = min(block + overlap, hi - addr)
        data = c.read_memory(addr, n)
        for i in range(0, len(data) - len(needle) + 1):
            if all(b is None or data[i + j] == b for j, b in enumerate(needle)):
                hits.append(addr + i)
                if len(hits) >= max_results:
                    break
        addr += block
    if not hits:
        return "No matches."
    real = dos.segmented(regs)
    seg_note = lambda a: " (%04X:%04X)" % (a >> 4, a & 0xF) if (real and a < 0x110000) else ""
    return "%d match(es):\n" % len(hits) + "\n".join("%08X%s" % (h, seg_note(h)) for h in hits)


@tool
def disassemble(address: str = "cs:eip", count: int = 20, bits: int = 0) -> str:
    """Disassemble `count` instructions at `address`.

    bits: 16, 32, or 0 for auto: the segment's own size for SEG:OFF addresses,
    otherwise the size of the code currently running."""
    regs = _regs()
    loc = _resolve(address, regs)
    count = max(1, min(count, 200))
    data = client().read_memory(loc.linear, count * 15)
    if bits not in (16, 32):
        bits = loc.bits or dos.code_bits(regs)
    return dos.disassemble(data, loc.linear, bits, loc, count)


@tool
def read_stack(entries: int = 16) -> str:
    """Show the top of the stack at SS:(E)SP, as 16- or 32-bit entries to match the stack size."""
    regs = _regs()
    ss = dos.seg_cache(regs, "ss")
    entries = max(1, min(entries, 256))
    if dos.stack_bits(regs) == 16:
        sp, w, mask, fmt = regs["esp"] & 0xFFFF, 2, 0xFFFF, "%04X:%04X  %04X"
    else:
        sp, w, mask, fmt = regs["esp"], 4, 0xFFFFFFFF, "%04X:%08X  %08X"
    data = client().read_memory((ss["base"] + sp) & 0xFFFFFFFF, entries * w)
    return "\n".join(fmt % (ss["sel"], (sp + i * w) & mask, int.from_bytes(data[i * w:(i + 1) * w], "little"))
                     for i in range(entries))


# ---- breakpoints / watchpoints ----------------------------------------------

@tool
def set_breakpoint(address: str) -> str:
    """Set an execution breakpoint (hardware-style; works in ROM and does not modify memory).

    SEG:OFF is resolved to a linear address now, in the current CPU mode."""
    c = client()
    regs = _regs()
    loc = _resolve(address, regs)
    linear = loc.linear
    c.set_point(BP_HARDWARE, linear)
    _points[(BP_HARDWARE, linear)] = {"kind": BP_HARDWARE, "address": linear, "length": 1, "expr": address}
    return "Breakpoint set at %s = %s." % (address, dos.describe(loc, regs))


@tool
def set_watchpoint(address: str, length: int = 1, kind: str = "write") -> str:
    """Stop when memory in [address, address+length) is accessed. kind: write, read or access."""
    c = client()
    k = {"write": WP_WRITE, "read": WP_READ, "access": WP_ACCESS}[kind]
    regs = _regs()
    loc = _resolve(address, regs)
    linear = loc.linear
    c.set_point(k, linear, max(1, length))
    _points[(k, linear)] = {"kind": k, "address": linear, "length": max(1, length), "expr": address}
    return "%s watchpoint on %s, %d byte(s) = %s." % (kind.capitalize(), address, max(1, length), dos.describe(loc, regs))


@tool
def clear_breakpoint(address: str = "all") -> str:
    """Remove the breakpoint/watchpoints at `address`, or every one with "all"."""
    c = client()
    if address == "all":
        targets = list(_points)
    else:
        linear = _resolve(address, _regs()).linear
        targets = [key for key in _points if key[1] == linear]
    for key in targets:
        p = _points.pop(key)
        c.clear_point(p["kind"], p["address"], p["length"])
    return "Removed %d point(s)." % len(targets)


@tool
def list_breakpoints() -> str:
    """List breakpoints and watchpoints set through this server."""
    if not _points:
        return "None."
    return "\n".join("%-12s %08X len=%d  (%s)" % (KIND_NAMES[p["kind"]], p["address"], p["length"], p["expr"])
                     for p in _points.values())


# ---- PC / DOS views ---------------------------------------------------------

@tool
def read_text_screen(include_attributes: bool = False) -> dict:
    """Read the current text-mode screen (from the BIOS video mode and video RAM)."""
    return dos.read_text_screen(client(), include_attributes)


@tool
def read_interrupt_vectors(first: str = "0", count: int = 48) -> str:
    """Show real-mode interrupt vectors (e.g. first="21", count=1 for the DOS handler).

    In protected mode these are still what DOS and reflected interrupts use;
    the protected-mode handlers are in the IDT (read_descriptor_table "idt")."""
    out = dos.read_ivt(client(), dos.parse_number(first), max(1, min(count, 256)))
    if not dos.segmented(_regs()):
        out = ("NOTE: CPU is in protected mode; these are the real-mode vectors. "
               "Protected-mode handlers: read_descriptor_table(\"idt\").\n") + out
    return out


@tool
def get_segments() -> str:
    """Show the segment registers' descriptor caches (base, limit, type, 16/32-bit),
    the CPU's code/stack size and privilege level, and GDTR/IDTR/LDTR/TR."""
    return dos.format_segments(_regs())


@tool
def read_descriptor_table(table: str = "gdt", first: int = 0, count: int = 32) -> str:
    """Decode entries of the GDT, LDT or IDT (table: gdt, ldt or idt).

    first/count are entry indexes (GDT selector = index * 8; IDT index = interrupt number)."""
    table = table.lower()
    if table not in ("gdt", "ldt", "idt"):
        return "table must be gdt, ldt or idt."
    regs = _regs()
    if table == "idt" and dos.cpu_mode(regs) == "real":
        return "CPU is in real mode; use read_interrupt_vectors for the interrupt vector table."
    return dos.read_table(client(), regs, table, max(0, first), max(1, min(count, 256)))


@tool
def io_read(port: str, count: int = 1, width: str = "b") -> str:
    """Read I/O port(s). width: b, w or l. Reading may have side effects on real hardware registers."""
    return client().monitor("i%s %s %d" % (width, port, count)).strip()


@tool
def io_write(port: str, value: str, width: str = "b") -> str:
    """Write a value (hex) to an I/O port. width: b, w or l."""
    out = client().monitor("o%s %s %s" % (width, port, value)).strip()
    return out or "OK"


# ---- screen / input ---------------------------------------------------------

@tool
def screenshot(downscale: int = 1, save_path: str = "") -> list:
    """Capture the emulated display as a PNG (the last completed frame).

    downscale: integer factor to shrink large frames (2 halves each side).
    save_path: optionally also write the PNG to this host path."""
    w, h, seq, rgb = pcinput.grab_frame(client())
    if not w:
        return ["No frame has been displayed since the debugger connected; resume the guest briefly first."]
    w, h, rgb = pcinput.downscale(w, h, rgb, max(1, min(downscale, 8)))
    png = pcinput.encode_png(w, h, rgb)
    if save_path:
        with open(save_path, "wb") as f:
            f.write(png)
    note = "%dx%d frame #%d%s" % (w, h, seq, (" saved to " + save_path) if save_path else "")
    if client().running:
        note += " (guest running; frame is from the moment of capture)"
    return [note, Image(data=png, format="png")]


def _ensure_running():
    c = client()
    if not c.running:
        c.resume()
        return " (CPU was paused; resumed it so the guest receives input)"
    return ""


@tool
def press_keys(keys: str, hold_ms: int = 80, gap_ms: int = 60) -> str:
    """Press keys or chords in sequence, e.g. "enter", "ctrl+c", "alt+f x", "up up right space".

    Separate presses with spaces; join simultaneous keys with "+". Key names:
    a-z 0-9 f1-f12 esc enter space tab backspace up down left right home end
    pageup pagedown insert delete shift ctrl alt rshift rctrl ralt capslock
    numlock scrolllock kp0-kp9 kp+ kp- kp* kp/ kp. kpenter and punctuation."""
    c = client()
    combos = [pcinput.parse_combo(k) for k in keys.split()]
    note = _ensure_running()
    for scans in combos:
        pcinput.press_combo(c, scans, hold_ms / 1000.0)
        time.sleep(gap_ms / 1000.0)
    return "Pressed %d key(s)%s." % (len(combos), note)


@tool
def type_text(text: str, delay_ms: int = 60) -> str:
    """Type text as keystrokes (US layout). Use "\n" for Enter, e.g. "dir\n"."""
    c = client()
    plan = [pcinput.char_keys(ch) for ch in text]
    note = _ensure_running()
    shift = pcinput.KEYS["shift"]
    for needs_shift, scan in plan:
        pcinput.press_combo(c, [shift, scan] if needs_shift else [scan], delay_ms / 2000.0)
        time.sleep(delay_ms / 2000.0)
    return "Typed %d character(s)%s." % (len(plan), note)


@tool
def key_down(key: str) -> str:
    """Hold a key down (e.g. to keep moving in a game) until key_up. Chords with "+" allowed."""
    c = client()
    scans = pcinput.parse_combo(key)
    note = _ensure_running()
    for s in scans:
        pcinput.key_event(c, s, True)
    return "Holding %s%s." % (key, note)


@tool
def key_up(key: str) -> str:
    """Release a key (or "+"-joined keys) held with key_down."""
    c = client()
    for s in reversed(pcinput.parse_combo(key)):
        pcinput.key_event(c, s, False)
    return "Released %s." % key


@tool
def mouse_move(dx: int, dy: int) -> str:
    """Move the mouse by a relative amount in mickeys (positive dy is down).

    Guest drivers scale this; take a screenshot to see where the pointer went."""
    c = client()
    note = _ensure_running()
    # Large jumps are split so drivers that clamp per-sample deltas keep up.
    steps = max(1, (max(abs(dx), abs(dy)) + 63) // 64)
    sent_x = sent_y = 0
    for i in range(1, steps + 1):
        nx, ny = dx * i // steps, dy * i // steps
        pcinput.mouse_move(c, nx - sent_x, ny - sent_y)
        sent_x, sent_y = nx, ny
        time.sleep(0.02)
    return "Moved mouse by (%d, %d)%s." % (dx, dy, note)


@tool
def mouse_click(button: str = "left", double: bool = False, hold_ms: int = 80) -> str:
    """Click a mouse button: left, right or middle."""
    c = client()
    mask = pcinput.BUTTONS[button.lower()]
    note = _ensure_running()
    for _ in range(2 if double else 1):
        pcinput.mouse_buttons(c, mask)
        time.sleep(hold_ms / 1000.0)
        pcinput.mouse_buttons(c, 0)
        time.sleep(0.08)
    return "%s-clicked%s%s." % (button, " twice" if double else "", note)


@tool
def mouse_buttons(left: bool = False, right: bool = False, middle: bool = False) -> str:
    """Set which mouse buttons are held (for dragging: press, mouse_move, release)."""
    c = client()
    note = _ensure_running()
    pcinput.mouse_buttons(c, (1 if left else 0) | (2 if right else 0) | (4 if middle else 0))
    return "Buttons now: left=%s right=%s middle=%s%s." % (left, right, middle, note)


@tool
def mouse_scroll(clicks: int) -> str:
    """Turn the mouse wheel (needs a wheel mouse and driver in the guest); negative is up."""
    c = client()
    note = _ensure_running()
    pcinput.mouse_move(c, 0, 0, clicks)
    return "Scrolled %d%s." % (clicks, note)


if __name__ == "__main__":
    mcp.run()
