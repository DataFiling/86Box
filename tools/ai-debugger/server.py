"""MCP server exposing a running 86Box (built with -DGDBSTUB=ON) as debugger
tools for an AI client such as Claude Code or Claude Desktop.

Run:  python3 server.py           (stdio transport)
Env:  BOX86_GDB_HOST (default 127.0.0.1), BOX86_GDB_PORT (default 12345)
"""

import functools
import os
import re
import struct
import threading
import time

import anyio
from mcp.server.fastmcp import FastMCP, Image

import dos
import dosinfo
import memscan
import pcinput
import symbols
import watchdog
from gdb_rsp import (BP_HARDWARE, BP_SOFTWARE, WP_ACCESS, WP_READ, WP_WRITE, GdbClient, GdbError)

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
Find a variable (e.g. lives): scan_memory(value=3), lose a life in the game,
scan_next("decreased") until one address is left, then set_watchpoint on it to
find the code that changes it. snapshot_memory + diff_memory shows everything
one game action changed.

DOS awareness: dos_memory_map lists the programs in memory and which one is
running (PSP, load segment). wait_for_program_start(name, command="GAME\n")
stops at a program's first instruction; protected_mode=true then continues to
a DOS-extender (DOS/4GW...) program's 32-bit entry. log_interrupts +
read_interrupt_log record DOS/DPMI/BIOS/mouse calls with decoded arguments,
file names and results (e.g. a failing file open); catch_interrupt stops on a
chosen call (e.g. INT 21h AH=3Dh opens, INT 10h AH=00h mode sets).
With the program's linker map, load_symbols lets you use names as addresses
and shows name+offset everywhere (after wait_for_program_start for automatic
placement).
watch_program runs a program unattended and says whether it exited, crashed,
hung (and inside which call) or waits for input: a good first step for "it
hangs" or "it crashes" reports.

`screenshot` shows the emulated display (any video mode). The input tools
press_keys, type_text, key_down and mouse_* resume the CPU if it is paused,
since the guest has to run to receive input, and leave it running.
"""

mcp = FastMCP("86box-debugger", instructions=INSTRUCTIONS)

_client = None
_client_lock = threading.Lock()
_points = {}  # (kind, linear) -> {"kind":..., "address":..., "length":..., "expr":...}

KIND_NAMES = {BP_SOFTWARE: "sw-breakpoint", BP_HARDWARE: "breakpoint", WP_WRITE: "write-watch", WP_READ: "read-watch",
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
            # Points can outlive a connection when the stub holds them (run_tool.py).
            for kind, addr, length in c.list_points() or []:
                _points[(kind, addr)] = {"kind": kind, "address": addr, "length": length,
                                         "expr": "set by an earlier connection"}
        return _client


def tool(fn):
    """Register a blocking function as an MCP tool, run off the event loop."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        return await anyio.to_thread.run_sync(functools.partial(_with_symbols, fn, *args, **kwargs))

    return mcp.tool()(wrapper)


# ---- helpers ------------------------------------------------------------

_symcache = {"key": None, "table": None}


def _symbols():
    """The loaded symbol table (load_symbols), kept on disk between run_tool
    commands; None if none is loaded."""
    try:
        d = memscan.state_dir(client().host, client().port)
    except Exception:
        return None
    info = memscan.load(d, "symbols")
    if not info:
        _symcache.update(key=None, table=None)
        return None
    try:
        key = (info["path"], os.path.getmtime(info["path"]), tuple(sorted(info["bases"].items())))
    except OSError:
        return None
    if _symcache["key"] != key:
        _symcache.update(key=key, table=symbols.SymbolTable(symbols.MapFile(info["path"]), info["bases"]))
    return _symcache["table"]


def _with_symbols(fn, *args, **kwargs):
    table = None
    try:
        table = _symbols()
    except (GdbError, OSError, ValueError):
        pass
    dos.namer = (lambda lin, dist: table.name_at(lin, dist)) if table else None
    return fn(*args, **kwargs)


def _sym(linear, dist=0x10000):
    """' (name+off)' for a linear address, or ''."""
    return " (%s)" % dos.namer(linear, dist) if dos.namer and dos.namer(linear, dist) else ""

def _regs():
    return client().read_registers()


def _resolve(address, regs):
    table = _symcache["table"] if dos.namer else None
    if table is not None:
        head = address.strip().split("+")[0].split("-")[0].strip()
        if head and not dos.is_register(head):
            lin = table.lookup(address)
            if lin is not None:
                return dos.Loc(lin, None, None, 32 if table.map.wide else None)
    return dos.resolve(address, regs, client())


def _ds_base(regs):
    try:
        return dos.seg_cache(regs, "ds")["base"]
    except ValueError:
        return None


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
    return dos.disassemble(data, linear, dos.code_bits(regs), _cs_loc(regs, linear), count, _ds_base(regs))


def _state_report(stop=None, code_lines=6):
    c = client()
    regs = _regs()
    parts = []
    if stop is not None:
        parts.append("STOP: " + stop.describe())
    elif not c.running and c.last_stop is not None:
        stop = c.last_stop
        parts.append("CPU is stopped. Last stop: " + stop.describe())
    if stop is not None and stop.reason == "catch" and not c.running:
        try:
            parts[-1] += "\nCaught: " + _describe_catch(c, stop, regs)
        except (GdbError, ValueError, KeyError) as e:
            parts[-1] += "\n(could not decode the call: %s)" % e
    parts.append(dos.format_registers(regs))
    if dos.namer and dos.namer(regs["eip"], 0x10000):
        parts[-1] = parts[-1].replace("\n", "   in %s\n" % dos.namer(regs["eip"], 0x10000), 1)
    if not dos.segmented(regs) and dos.stack_bits(regs) == 32 and abs(regs["ebp"] - regs["esp"]) > 0x100000 \
            and regs["ebp"] > 0x10:
        parts[-1] += ("\nNOTE: EBP (%08X) is far from ESP (%08X): the frame pointer may belong to another stack "
                      "(e.g. after a stack switch, EBP-based locals use the new SS)." % (regs["ebp"], regs["esp"]))
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
    words = insn.mnemonic.split() if insn is not None else []
    string_ops = ("movs", "stos", "lods", "cmps", "scas", "ins", "outs")
    over = bool(words) and (words[0] in ("call", "lcall", "int", "int1", "int3", "into") or
                            words[0].startswith("loop") or
                            (words[0] in ("rep", "repe", "repne", "repz", "repnz") and len(words) > 1
                             and words[1].startswith(string_ops)))
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
def step_out(max_steps: int = 20000) -> str:
    """Run until the current function returns: steps through it, running over
    calls and interrupts, and stops after its RET/RETF/IRET. Gives up after
    max_steps instructions (each takes a round trip, about 1 ms)."""
    c = client()
    if c.running:
        c.pause()
    if dos.capstone is None:
        return "step_out needs capstone (pip install capstone)."
    md = dos.capstone.Cs(dos.capstone.CS_ARCH_X86, dos.capstone.CS_MODE_16)
    for n in range(max(1, max_steps)):
        regs = _regs()
        md.mode = dos.capstone.CS_MODE_16 if dos.code_bits(regs) == 16 else dos.capstone.CS_MODE_32
        insn = next(md.disasm(c.read_memory(regs["eip"], 15), 0), None)
        word = insn.mnemonic.split()[0] if insn is not None else ""
        if word in ("ret", "retf", "iret", "iretd", "retn"):
            return "Returned after %d instruction(s).\n\n%s" % (n + 1, _state_report(c.step()))
        if word in ("call", "lcall", "int", "int1", "int3", "into"):
            stop = None
            target = regs["eip"] + insn.size
            temp = (BP_HARDWARE, target) not in _points
            if temp:
                c.set_point(BP_HARDWARE, target)
            try:
                c.resume()
                stop = c.wait_stop(timeout=30)
            finally:
                if stop is None:
                    c.pause()
                if temp:
                    c.clear_point(BP_HARDWARE, target)
            if stop is None or stop.reason != "breakpoint" or _regs()["eip"] != target:
                return "Stopped inside a call before the function returned.\n\n" + _state_report(stop)
        else:
            stop = c.step()
            if stop is None or stop.reason not in ("trap", "pause"):
                return _state_report(stop)
    return "Still inside the function after %d steps.\n\n%s" % (max_steps, _state_report())


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
    if name == "cs" and not dos.segmented(regs):
        return ("Refusing to load CS directly in protected mode: the stub loads it like a data "
                "selector, without switching the code size or checking privilege. Move within CS "
                "with set_register eip/pc, or let the program do a far jump.")
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
    start_loc = _resolve(start, regs)
    lo, hi = start_loc.linear, _resolve(end, regs).linear
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

    def seg_note(a):
        if start_loc.sel is not None and 0 <= a - start_loc.base <= 0xFFFFFFFF:
            return " (%s)" % dos._label(start_loc, a)
        if not real:
            return ""
        if a < 0x100000:
            return " (%04X:%04X)" % (a >> 4, a & 0xF)
        if a < 0x10FFF0:  # the HMA, reachable as FFFF:xxxx
            return " (FFFF:%04X)" % (a - 0xFFFF0)
        return ""
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
    note = ""
    if dos.segmented(regs) and loc.base is not None and loc.linear - loc.base > 0xFFFF:
        note = ("NOTE: the CPU is in %s mode, so %s was taken as segment*16+offset; a protected-mode "
                "selector can only be looked up while the CPU is in protected mode (or pass the linear "
                "address and bits=32).\n" % (dos.cpu_mode(regs), address))
    return note + dos.disassemble(data, loc.linear, bits, loc, count, _ds_base(regs))


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
    c = client()
    lines = []
    for i in range(entries):
        off = (sp + i * w) & mask  # a 16-bit stack wraps within its 64 KiB segment
        data = c.read_memory((ss["base"] + off) & 0xFFFFFFFF, w)
        val = int.from_bytes(data, "little") if len(data) == w else None
        line = fmt % (ss["sel"], off, val) if val is not None else (fmt[:-6] % (ss["sel"], off)) + "  ??"
        if val is not None and val >= 0x100:
            # A 16-bit stack's values are near offsets in CS; 32-bit ones are flat.
            lin = (dos.seg_cache(regs, "cs")["base"] + val) if w == 2 else val
            table = _symcache["table"] if dos.namer else None
            n = table.code_name_at(lin) if table else None
            if n:
                line += " (%s)" % n
        lines.append(line)
    return "\n".join(lines)


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
def set_watchpoint(address: str, length: int = 1, kind: str = "write", cpu_mode: str = "") -> str:
    """Stop when memory in [address, address+length) is accessed. kind: write, read or access.

    cpu_mode: "protected" or "real" makes ALL watchpoints catch only accesses
    made in that CPU mode ("any" undoes it; empty leaves it as is). E.g. watch
    DOS memory with cpu_mode="protected" to find a DOS-extender program writing
    into it, while DOS's own (real-mode) writes don't stop the CPU."""
    c = client()
    k = {"write": WP_WRITE, "read": WP_READ, "access": WP_ACCESS}[kind]
    note = ""
    if cpu_mode:
        mode = {"protected": "pm", "pm": "pm", "real": "rm", "rm": "rm", "any": "any"}.get(cpu_mode.lower())
        if mode is None:
            return "cpu_mode is protected, real or any."
        c.monitor("wf " + mode)
        note = " All watchpoints now catch %s accesses." % {"pm": "protected-mode", "rm": "real/V86-mode",
                                                           "any": "all"}[mode]
    regs = _regs()
    loc = _resolve(address, regs)
    linear = loc.linear
    c.set_point(k, linear, max(1, length))
    _points[(k, linear)] = {"kind": k, "address": linear, "length": max(1, length), "expr": address}
    return "%s watchpoint on %s, %d byte(s) = %s.%s" % (kind.capitalize(), address, max(1, length),
                                                         dos.describe(loc, regs), note)


@tool
def clear_breakpoint(address: str) -> str:
    """Remove the breakpoint/watchpoints at `address`, or every one with address="all".

    Breakpoints persist until cleared (also across run_tool.py commands)."""
    c = client()
    if address == "all":
        targets = list(_points)
    else:
        linear = _resolve(address, _regs()).linear
        targets = [key for key in _points if key[1] == linear]
    removed = []
    for key in targets:
        p = _points.pop(key)
        c.clear_point(p["kind"], p["address"], p["length"])
        removed.append("%s at %08X (%s)" % (KIND_NAMES[p["kind"]], p["address"], p["expr"]))
    if not removed:
        return "Nothing to remove at %s." % address
    return "Removed %d point(s):\n%s" % (len(removed), "\n".join(removed))


@tool
def list_breakpoints() -> str:
    """List breakpoints, watchpoints and interrupt catches."""
    lines = ["%-12s %08X len=%d  (%s)" % (KIND_NAMES[p["kind"]], p["address"], p["length"], p["expr"])
             for p in _points.values()]
    try:
        for v, ah, al, w in client().int_status()["catches"]:
            lines.append("%-12s INT %02Xh%s%s on %s" % ("int-catch", v, "" if ah is None else " AH=%02Xh" % ah,
                                                        "" if al is None else " AL=%02Xh" % al,
                                                        {1: "call", 2: "return", 3: "call and return"}[w]))
    except GdbError:
        pass  # older 86Box build without interrupt catches
    try:
        wf = client().monitor("wf").split()[-1]
        if wf != "any" and _points:
            lines.append("(watchpoints catch %s accesses only)" % {"pm": "protected-mode", "rm": "real/V86-mode"}[wf])
    except (GdbError, KeyError, IndexError):
        pass
    return "\n".join(lines) or "None."


# ---- PC / DOS views ---------------------------------------------------------

@tool
def read_text_screen(include_attributes: bool = False, plain: bool = False):
    """Read the current text-mode screen (from the BIOS video mode and video RAM).

    Returns JSON (mode, size, cursor, text); plain=true returns just the text,
    one screen row per line."""
    result = dos.read_text_screen(client(), include_attributes)
    if plain:
        return result["text"] if result.get("text") is not None else result["note"]
    return result


@tool
def read_interrupt_vectors(first: str = "0", count: int = 48) -> str:
    """Show real-mode interrupt vectors (e.g. first="21", count=1 for the DOS handler).

    In protected mode these are still what DOS and reflected interrupts use;
    the protected-mode handlers are in the IDT (read_descriptor_table "idt")."""
    first = dos.parse_number(first)
    if not 0 <= first <= 0xFF:
        return "first must be an interrupt number from 0 to FF."
    out = dos.read_ivt(client(), first, max(1, min(count, 256 - first)))
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
def read_descriptor_table(table: str = "gdt", first: str = "0", count: int = 32, selector: str = "") -> str:
    """Decode entries of the GDT, LDT or IDT (table: gdt, ldt or idt).

    first: entry index (hex, like other numbers here; GDT index = selector / 8,
    IDT index = interrupt number); count: how many. selector: decode just this
    selector instead (e.g. "00B0"; its TI bit picks GDT or LDT)."""
    if selector:
        sel = dos.parse_number(selector)
        table, first, count = ("ldt" if sel & 4 else "gdt"), sel >> 3, 1
    else:
        first = dos.parse_number(str(first))
    table = table.lower()
    if table not in ("gdt", "ldt", "idt"):
        return "table must be gdt, ldt or idt."
    regs = _regs()
    if table == "idt" and dos.cpu_mode(regs) == "real":
        return "CPU is in real mode; use read_interrupt_vectors for the interrupt vector table."
    return dos.read_table(client(), regs, table, max(0, first), max(1, min(count, 256)))


def _io_width(width):
    width = width.lower()
    if width not in ("b", "w", "l"):
        raise ValueError("width must be b, w or l")
    return width


@tool
def io_read(port: str, count: int = 1, width: str = "b") -> str:
    """Read I/O port(s) (port in hex). width: b, w or l. Reads can have side
    effects on the emulated hardware (e.g. clearing status bits)."""
    # The stub reads a bare leading 0 as octal, so always send explicit hex.
    return client().monitor("i%s 0x%x %d" % (_io_width(width), dos.parse_number(port) & 0xFFFF,
                                             max(1, min(count, 256)))).strip()


@tool
def io_write(port: str, value: str, width: str = "b") -> str:
    """Write a value to an I/O port (both hex). width: b, w or l."""
    out = client().monitor("o%s 0x%x 0x%x" % (_io_width(width), dos.parse_number(port) & 0xFFFF,
                                             dos.parse_number(value))).strip()
    return out or "OK"


# ---- screen / input ---------------------------------------------------------

@tool
def screenshot(downscale: int = 1, save_path: str = "") -> list:
    """Capture the emulated display as a PNG (the last completed frame).

    This is the last frame the video card scanned out, so pixels drawn since
    the last vertical retrace (e.g. just before a breakpoint) only show after
    the guest runs a moment longer (run_for 0.1). Games that redraw without
    double buffering can also be caught mid-redraw (flicker, as on real
    hardware): if something you expect is missing, take another screenshot.
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
        last = c.last_stop
        c.resume()
        if last is not None and last.reason not in ("pause", "trap"):
            return (" (NOTE: the CPU was stopped by: %s; resumed it so the guest receives input)"
                    % last.describe())
        return " (CPU was paused; resumed it so the guest receives input)"
    return ""


def _stopped_during_input(done, total, what):
    """If a breakpoint/watchpoint stopped the guest while we were injecting
    input, say so (the rest of the input is not sent)."""
    c = client()
    if c.running:
        return None
    return ("Sent %d of %d %s, then the CPU stopped, so the rest was not sent.\n\n%s"
            % (done, total, what, _state_report(c.last_stop)))


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
    for n, scans in enumerate(combos):
        pcinput.press_combo(c, scans, hold_ms / 1000.0)
        time.sleep(gap_ms / 1000.0)
        stopped = _stopped_during_input(n + 1, len(combos), "key presses")
        if stopped:
            return stopped
    return "Pressed %d key(s)%s." % (len(combos), note)


@tool
def type_text(text: str, delay_ms: int = 60) -> str:
    """Type text as keystrokes (US layout). Use "\\n" for Enter, e.g. "dir\\n"."""
    c = client()
    plan = [pcinput.char_keys(ch) for ch in text]
    note = _ensure_running()
    shift = pcinput.KEYS["shift"]
    for n, (needs_shift, scan) in enumerate(plan):
        pcinput.press_combo(c, [shift, scan] if needs_shift else [scan], delay_ms / 2000.0)
        time.sleep(delay_ms / 2000.0)
        stopped = _stopped_during_input(n + 1, len(plan), "characters")
        if stopped:
            return stopped
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
    """Move the mouse by a relative amount (positive dy is down), in the same
    units as host mouse motion: 86Box applies its mouse sensitivity and the
    guest driver its own scaling. Take a screenshot to see where the pointer went."""
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
    stopped = _stopped_during_input(1, 1, "clicks")
    if stopped:
        return stopped
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
    """Turn the mouse wheel by whole clicks; positive turns it away from you (scroll up).
    Needs a wheel mouse and a wheel-aware driver in the guest."""
    c = client()
    note = _ensure_running()
    pcinput.mouse_move(c, 0, 0, clicks)
    return "Scrolled %d%s." % (clicks, note)


# ---- DOS awareness ------------------------------------------------------------

def _mem_image(c):
    """The first MiB of memory (RAM/ROM only; zeros where unreadable)."""
    mem = bytearray(0x100000)
    for a, d in c.peek(0, 0x100000):
        if d:
            mem[a:a + len(d)] = d
    return mem


def _dos_state(c):
    mem = _mem_image(c)
    chain, top = dosinfo.find_mcb_chain(mem)
    progs = dosinfo.analyze(mem, chain) if chain else {}
    return mem, chain, top, progs


def _call_mode(regs):
    m = dos.cpu_mode(regs)
    return 0 if m == "real" else 1 if m == "v86" else (3 if dos.code_bits(regs) == 32 else 2)


def _current_program(regs, progs, mem=None, chain=None):
    """(program, reason) for the DOS program that is running."""
    if mem is not None:
        psp, _ = dosinfo.current_psp(mem, chain, progs)
        if psp is not None:
            p = progs[psp]
            if not dos.segmented(regs):
                return p, "DOS's current PSP; the CPU is in protected mode, so this is likely a DOS extender running its program"
            return p, "DOS's current PSP"
    if dos.segmented(regs):
        p = dosinfo.program_at(progs, regs["eip"])
        if p:
            return p, "CS:IP is inside it"
        p = dosinfo.program_at(progs, dos.seg_cache(regs, "ds")["base"])
        if p:
            return p, "DS points into it"
    p = dosinfo.newest_program(progs)
    if p is None:
        return None, ""
    if not dos.segmented(regs):
        return p, "most recently loaded; the CPU is in protected mode, so this is likely the DOS extender"
    return p, "most recently loaded (CS:IP is outside DOS program memory)"


def _name_matches(want, path):
    if not want:
        return True
    base = path.replace("/", "\\").rsplit("\\", 1)[-1].upper()
    want = want.upper()
    return base == want or ("." not in want and base.split(".")[0] == want)


@tool
def dos_memory_map() -> str:
    """Show DOS memory: the memory control block (MCB) chain, the programs in
    memory (PSP, parent, path, PSP command tail, which DOS reuses as the default
    disk transfer area, so it can be stale) and which one is running.

    A real-mode program's code and data are in its blocks; an .EXE is loaded
    at segment PSP+10h, so a linker map's segment values are relative to that."""
    c = client()
    regs = _regs()
    mem, chain, top, progs = _dos_state(c)
    if not chain:
        return "No DOS memory control block chain found (is DOS running? top of memory %04X)." % top
    lines = ["Conventional memory: %d KB (top segment %04X). MCB chain starts at %04X." % (top // 64, top, chain[0].mcb),
             "", "MCB   Data  Size    Owner  Type"]
    for b in chain:
        owner = ("%04X" % b.owner) if b.owner else "----"
        what = b.kind + (" of " + b.program if b.kind in ("environment", "data") and b.program else "")
        if b.kind == "program":
            what = "program " + b.program
        lines.append("%04X  %04X  %-6s  %s   %s" % (b.mcb, b.start, dosinfo.format_size(b.size), owner, what))
    cur, why = _current_program(regs, progs, mem, chain)
    lines += ["", "Programs (an .EXE's load segment is PSP+10h):"]
    for p in sorted(progs.values(), key=lambda p: p.psp):
        if not any(b.kind == "program" for b in p.blocks):
            continue
        parent = progs.get(p.parent)
        lines.append("  PSP %04X  %-8s parent %04X%s  %s%s%s" % (
            p.psp, p.name, p.parent, (" (%s)" % parent.name) if parent and parent is not p else
            (" (itself: the shell)" if p.parent == p.psp else ""),
            p.path or "", (" tail %r" % p.args) if p.args else "", "   <-- running" if p is cur else ""))
    if cur:
        lines += ["", "Running: %s (PSP %04X): %s." % (cur.name or "?", cur.psp, why)]
    return "\n".join(lines)


def _parse_vectors(text):
    out = []
    for w in text.replace(",", " ").split():
        v = int(w.rstrip("hH"), 16)
        if not 0 <= v <= 0xFF:
            raise ValueError("vector %s is out of range" % w)
        out.append(v)
    return out


@tool
def log_interrupts(vectors: str = "21 31 33", clear: bool = True) -> str:
    """Start logging software interrupt calls (INT n) to these vectors, inside the
    emulator: each call's registers, file names/strings it points to, and its
    results when it returns. vectors: hex vectors, e.g. "21 31 33" (21h DOS,
    31h DPMI, 33h mouse, 10h video, 16h keyboard, 13h disk), or "off".

    Logging costs nothing noticeable and keeps the last 4096 calls (identical
    back-to-back calls are folded into one). Read it with read_interrupt_log.
    Great for: which files a game opens (and whether that fails), video mode
    changes, mouse calls, DOS extender (DPMI) setup, the last calls before a hang."""
    c = client()
    if vectors.strip().lower() in ("off", "none", ""):
        c.int_trace([])
        return "Interrupt logging stopped (the log is kept)."
    vs = _parse_vectors(vectors)
    c.int_trace(vs)
    if clear:
        c.int_log_clear()
    names = ", ".join("%02Xh%s" % (v, (" " + dosinfo.VECTOR_INFO[v][0]) if v in dosinfo.VECTOR_INFO else "")
                      for v in vs)
    return "Logging INT %s%s. The CPU is %s." % (names, " (log cleared)" if clear else "",
                                                 "running" if c.running else "paused; resume to collect calls")


def _is_reflection(parent, cur):
    """A DOS extender passing a protected-mode call down to real mode makes the
    same call again, nested inside it (several times when it splits a transfer)."""
    return (parent is not None and cur.vector == parent.vector and cur.mode != parent.mode and
            dosinfo.function_number(cur) == dosinfo.function_number(parent))


@tool
def read_interrupt_log(filter: str = "", since: int = 0, limit: int = 60, collapse: bool = True,
                       show_polling: bool = False, show_reflections: bool = False) -> str:
    """Show logged INT calls with decoded function names, arguments and results,
    most recent last (see log_interrupts).

    filter: vectors, optionally with function numbers (AH, or AX for 31h/33h/2Fh),
      e.g. "21", "21:3D/3F/4B 10:00", "31:0501".
    since: show calls from this sequence number on, oldest first (without it:
      the most recent `limit` calls). The footer gives the value for "only calls
      after this read".
    collapse: list identical calls (same caller, arguments and result) once with
      a count, which keeps game loops readable; false shows every call in order.
    show_polling: include keyboard-status polls with no key, idle calls, etc.
    show_reflections: include the copies DOS extenders make when they pass a
      protected-mode call down to real-mode DOS (shown nested under the original)."""
    c = client()
    st = c.int_status()
    if not st["vectors"] and st["next"] == st["first"]:
        return "Nothing logged. Start with log_interrupts (e.g. vectors=\"21 31 33\")."
    try:
        flt = dosinfo.parse_filter(filter)
    except ValueError:
        return "Bad filter %r; use e.g. \"21:3D/3F 31\" (hex)." % filter
    first = max(st["first"], since or 0)
    calls = dosinfo.parse_log(c.int_log(first))
    hz = st.get("hz") or 1
    now = st.get("tsc", 0)
    shown, hidden_poll, hidden_refl, at_depth = [], 0, 0, {}
    for x in calls:
        refl = _is_reflection(at_depth.get(x.depth - 1), x)
        at_depth[x.depth] = x  # the latest call at each nesting depth encloses deeper ones
        if not dosinfo.matches_filter(x, flt):
            continue
        if refl and not show_reflections:
            hidden_refl += 1
            continue
        if not show_polling and dosinfo.is_polling(x):
            hidden_poll += x.count
            continue
        shown.append(x)
    groups = []
    if collapse:
        index = {}
        for x in shown:
            what, result = dosinfo.describe(x)
            key = (x.vector, x.caller_linear(), what, result)
            if key in index:
                g = index[key]
                g["n"] += x.count
                g["last"] = x
            else:
                g = index[key] = {"n": x.count, "first": x, "last": x, "what": what, "result": result}
                groups.append(g)
        groups.sort(key=lambda g: g["last"].seq)
    else:
        for x in shown:
            what, result = dosinfo.describe(x)
            groups.append({"n": x.count, "first": x, "last": x, "what": what, "result": result})
    total = len(groups)
    # Without `since`, the most recent calls matter (what led up to now);
    # with it, the caller is reading forward from a point.
    groups = groups[:max(1, limit)] if since else groups[-max(1, limit):]
    base_depth = min((g["last"].depth for g in groups), default=0)
    lines = []
    for g in groups:
        x = g["last"]
        ago = (x.tsc - now) / hz if now else 0
        rep = ""
        if g["n"] > 1:
            rep = " (x%d%s)" % (g["n"], ", first #%d" % g["first"].seq if g["first"] is not x else "")
        mode = "" if x.mode == 0 else " [%s]" % dosinfo.MODES.get(x.mode, "?")
        lines.append("#%-6d %8.3fs %sINT %02Xh %s -> %s%s  (from %s%s%s)" % (
            x.seq, ago, "  " * min(x.depth - base_depth, 6), x.vector, g["what"], g["result"], rep, x.caller(),
            _sym(x.caller_linear()), mode))
    head = ("INT log: logging %s; %d call record(s) read (#%d..#%d); times are seconds before now (emulated); "
            "(xN, first #M) = N identical calls shown once." % (
        " ".join("%02Xh" % v for v in st["vectors"]) or "nothing (stopped)", len(calls), first, st["next"] - 1))
    notes = []
    if total > len(groups):
        if since:
            notes.append("%d later entr%s not shown (raise limit, or since=%d to continue)" % (
                total - len(groups), "y" if total - len(groups) == 1 else "ies", groups[-1]["last"].seq + 1))
        else:
            notes.append("%d earlier entr%s not shown (raise limit or use since)" % (
                total - len(groups), "y" if total - len(groups) == 1 else "ies"))
    if hidden_poll:
        notes.append("%d polling call(s) hidden (show_polling=true)" % hidden_poll)
    if hidden_refl:
        notes.append("%d DOS-extender reflection(s) hidden (show_reflections=true)" % hidden_refl)
    if st.get("pending"):
        notes.append("%d call(s) still in progress (shown as still running)" % st["pending"])
    foot = "Newer calls only: since=%d." % st["next"]
    if since and since < st["first"]:
        foot = "NOTE: calls before #%d were overwritten (the log keeps 4096). " % st["first"] + foot
    return "\n".join([head] + (lines or ["(no matching calls)"]) + [""] + ["; ".join(notes)] * bool(notes) + [foot])


def _parse_function(function):
    f = function.strip().lower().rstrip("h")
    if not f or f in ("*", "any"):
        return None, None
    if len(f) <= 2:
        return int(f, 16), None
    if len(f) == 4:
        return int(f[:2], 16), int(f[2:], 16)
    raise ValueError("function is AH (2 hex digits) or AH and AL (4 hex digits, e.g. 4B00 or 0501)")


@tool
def catch_interrupt(vector: str, function: str = "", when: str = "call") -> str:
    """Stop when the guest calls INT `vector` (hex), optionally only for one
    function: AH as 2 hex digits ("3D") or AH+AL as 4 ("4B00", "0501" for INT 31h).

    when="call" stops AT the INT instruction, before it runs, with its arguments
    in the registers (resume runs it); "return" stops at the instruction after
    it with the results; "both" does both. Catches stay until
    clear_interrupt_catches. Examples: file opens: vector="21" function="3D";
    program exit: "21" "4C"; video mode set: "10" "00"; DPMI allocations
    returning: "31" "0501" when="return"."""
    c = client()
    v = _parse_vectors(vector)
    if len(v) != 1:
        return "Give one vector, e.g. vector=\"21\"."
    ah, al = _parse_function(function)
    w = when.lower()
    if w not in ("call", "return", "both"):
        return "when must be call, return or both."
    if w != "call":
        st = c.int_status()
        if v[0] not in st["vectors"]:
            c.int_trace(sorted(set(st["vectors"]) | {v[0]}))  # so the stop can show the call's arguments
    c.catch_int(v[0], ah, al, {"call": "call", "return": "ret", "both": "both"}[w])
    what = "INT %02Xh" % v[0] + ("" if ah is None else " AH=%02Xh" % ah) + ("" if al is None else " AL=%02Xh" % al)
    return "Will stop on %s %s. The CPU is %s." % (what, {"call": "calls (before the INT runs)",
                                                          "return": "returns", "both": "calls and returns"}[w],
                                                   "running" if c.running else "paused")


@tool
def clear_interrupt_catches() -> str:
    """Remove all interrupt catches (catch_interrupt)."""
    client().clear_int_catches()
    return "Interrupt catches removed."


def _describe_catch(c, stop, regs):
    if stop.int_seq:
        recs = dosinfo.parse_log(c.int_log(stop.int_seq, 1))
        if recs and recs[0].seq == stop.int_seq:
            what, result = dosinfo.describe(recs[0])
            return "INT %02Xh %s -> %s  (called from %s)" % (recs[0].vector, what, result, recs[0].caller())
    if stop.int_return:
        return "INT %02Xh returned (log this vector for its arguments)" % stop.int_vector
    mode = _call_mode(regs)
    wide = mode == 3
    data = {}
    for key, seg, reg in (("dx", "ds", "edx"), ("si", "ds", "esi"), ("di", "es", "edi")):
        try:
            base = dos.seg_cache(regs, seg)["base"]
        except ValueError:
            continue
        lin = (base + (regs[reg] if wide else regs[reg] & 0xFFFF)) & 0xFFFFFFFF
        runs = c.peek(lin, 64)
        data[key] = runs[0][1] if runs and runs[0][1] else b""
    r = dict(regs)
    r["eip_off"] = dos.ip(regs) or 0
    what, _ = dosinfo.describe(dosinfo.call_from_regs(stop.int_vector, r, mode, data))
    return "INT %02Xh %s (not executed yet; resume runs it)" % (stop.int_vector, what)


def _type_command(c, text):
    """Type text (to start a program) while a wait is armed; stops early if the CPU stops."""
    if not text:
        return
    if not c.running:
        c.resume()
    shift = pcinput.KEYS["shift"]
    for ch in text:
        needs_shift, scan = pcinput.char_keys(ch)
        pcinput.press_combo(c, [shift, scan] if needs_shift else [scan], 0.03)
        time.sleep(0.03)
        if not c.running:
            return


def _entry_report(stop):
    """State report for a program-start stop, without the internal range-catch wording."""
    rep = _state_report(stop)
    return rep.replace("STOP: " + stop.describe(), "STOP: program entry point", 1)


def _restore_catches(c, saved):
    c.clear_int_catches()
    for v, ah, al, w in saved["catches"]:
        c.catch_int(v, ah, al, {1: "call", 2: "ret", 3: "both"}[w])


@tool
def wait_for_program_start(name: str = "", command: str = "", timeout_seconds: float = 60.0,
                           protected_mode: bool = False) -> str:
    """Run until a DOS program starts and stop at its first instruction.

    name: program file name to wait for (e.g. "GAME" or "GAME.EXE"); empty = the
    next program started. command: text to type once the watch is set up, to
    start it, e.g. "GAME" plus a newline (Enter) at the DOS prompt; leave it
    empty if something else starts the program.

    protected_mode=true waits instead for a DOS-extender program's 32-bit code
    (DOS/4GW and other DPMI-based extenders): it watches the extender open the
    program file and allocate DPMI memory, and stops when execution first enters
    that memory, at the program's 32-bit entry point. Use it after (or instead
    of) the real-mode start, which for such programs is just the extender stub."""
    c = client()
    saved = c.int_status()
    deadline = time.time() + max(1.0, timeout_seconds)
    traced = list(saved["vectors"])

    def wait():
        return c.wait_stop(timeout=max(0.05, deadline - time.time()))

    def other_stop(stop):
        return ("Stopped for another reason (%s) before the program started:\n\n" % stop.describe()) + _state_report(stop)

    def next_stop():
        # A stop that came in while typing is still queued: take it rather than resume past it.
        if not c.running and c.stops.empty():
            c.resume()
        return wait()

    try:
        c.clear_int_catches()
        c.set_exec_ranges([])
        if not protected_mode:
            c.catch_int(0x21, 0x4B, 0x00, "call")
            _type_command(c, command)
            while True:
                stop = next_stop()
                if stop is None:
                    return "No program was started within %.0fs (CPU still running)." % timeout_seconds
                if stop.reason != "catch":
                    return other_stop(stop)
                regs = _regs()
                path = dosinfo.asciiz(c.read_memory(_resolve("ds:dx", regs).linear, 80), 80)
                if _name_matches(name, path):
                    break
            mem, chain, top, progs = _dos_state(c)
            free = sorted(((b.start * 16, b.end * 16) for b in chain or [] if b.kind == "free"),
                          key=lambda r: r[0] - r[1])[:8]
            if not free:
                return "Caught the exec of %s, but found no free DOS memory to watch." % path
            c.clear_int_catches()
            c.catch_int(0x21, 0x4B, 0x00, "ret")  # a failed exec returns at once
            c.set_exec_ranges(free)
            c.resume()
            stop = wait()
            if stop is None:
                return "%s was exec'd but didn't start within %.0fs (CPU still running)." % (path, timeout_seconds)
            if stop.reason == "catch" and stop.int_return:
                regs = _regs()
                if regs["eflags"] & 1:
                    code = regs["eax"] & 0xFFFF
                    return "Exec of %s failed: error %02Xh (%s).\n\n%s" % (
                        path, code, dosinfo.DOS_ERRORS.get(code, "unknown"), _state_report(stop))
                return "%s ran and exited before its start was detected.\n\n%s" % (path, _state_report(stop))
            if stop.reason != "range":
                return other_stop(stop)
            regs = _regs()
            mem, chain, top, progs = _dos_state(c)
            p = dosinfo.program_at(progs, regs["eip"])
            lines = ["Program %s started: stopped at its first instruction." % path]
            if p:
                memscan.save(_state_dir(), "last-start", {"kind": "real", "path": path, "psp": p.psp,
                                                          "load": p.load_segment})
                lines.append("PSP %04X; load segment %04X (PSP+10h; add it to segment values from the "
                             "program's linker map or EXE header). DS=ES=PSP at entry." % (p.psp, p.load_segment))
                lines.append("Memory: " + ", ".join("%04X-%04X %s" % (b.start, b.end, b.kind) for b in p.blocks))
            lines.append("If this is a DOS-extender stub (e.g. a DOS/4GW game), call "
                         "wait_for_program_start(protected_mode=true) to continue to its 32-bit code.")
            return "\n".join(lines) + "\n\n" + _entry_report(stop)

        # Protected mode: find the extender opening the program, then DPMI blocks allocated after.
        if not {0x21, 0x31} <= set(traced):
            c.int_trace(sorted(set(traced) | {0x21, 0x31}))
        c.catch_int(0x21, 0x3D, None, "call")
        c.catch_int(0x21, 0x6C, None, "call")
        _type_command(c, command)
        opened = None
        while opened is None:
            stop = next_stop()
            if stop is None:
                return "No protected-mode open of %s within %.0fs (CPU still running)." % (name or "an .EXE", timeout_seconds)
            if stop.reason != "catch":
                return other_stop(stop)
            regs = _regs()
            if dos.segmented(regs):
                continue
            reg = "ds:edx" if dos.code_bits(regs) == 32 else "ds:dx"
            if (regs["eax"] >> 8) & 0xFF == 0x6C:
                reg = reg.replace("dx", "si")
            path = dosinfo.asciiz(c.read_memory(_resolve(reg, regs).linear, 80), 80)
            if _name_matches(name, path) and (name or path.upper().endswith(".EXE")):
                opened = path
        c.clear_int_catches()
        c.catch_int(0x31, 0x05, 0x01, "ret")
        c.catch_int(0x31, 0x05, 0x03, "ret")
        blocks = []
        while True:
            c.resume()
            stop = wait()
            if stop is None:
                return ("%s was opened by the extender, but its code didn't start within %.0fs (CPU still running). "
                        "DPMI blocks seen: %s" % (opened, timeout_seconds,
                                                  ", ".join("%08X+%X" % (a, b - a) for a, b in blocks) or "none"))
            if stop.reason == "range":
                break
            if stop.reason != "catch":
                return other_stop(stop)
            recs = dosinfo.parse_log(c.int_log(stop.int_seq, 1)) if stop.int_seq else []
            if recs and recs[0].seq == stop.int_seq and not recs[0].cf():
                r = recs[0]
                size = (r.r("bx") << 16) | r.r("cx")
                lin = (r.r("bx", True) << 16) | r.r("cx", True)
                if size:
                    blocks.append((lin, lin + size))
                    c.set_exec_ranges(sorted(blocks, key=lambda b: b[0] - b[1])[:8])
        regs = _regs()
        inside = [b for b in blocks if b[0] <= regs["eip"] < b[1]]
        memscan.save(_state_dir(), "last-start", {"kind": "protected", "path": opened, "entry": regs["eip"],
                                                  "blocks": blocks})
        lines = ["32-bit code of %s started: stopped at its first instruction (linear %08X)." % (opened, regs["eip"]),
                 "DPMI memory allocated after the extender opened it: " +
                 ", ".join("%08X-%08X%s" % (a, b, " (code entered here)" if (a, b) in inside else "") for a, b in blocks),
                 "Under DOS/4GW-style flat models, linear = offset (segment bases 0); the program's objects "
                 "(code, data) are in these blocks."]
        return "\n".join(lines) + "\n\n" + _entry_report(stop)
    finally:
        try:
            c.set_exec_ranges([])
            _restore_catches(c, saved)
            if set(traced) != set(c.int_status()["vectors"]):
                c.int_trace(traced)
        except (GdbError, OSError):
            pass


# ---- memory snapshots, diffs and value scans ---------------------------------------

def _state_dir():
    c = client()
    return memscan.state_dir(c.host, c.port)


def _regions(spec, regs):
    """-> (ranges, description, ds_base for labels or None)."""
    c = client()
    s = spec.strip().lower()
    if s in ("auto", "program"):
        if dos.segmented(regs):
            mem, chain, top, progs = _dos_state(c)
            p, why = _current_program(regs, progs, mem, chain)
            if p:
                ranges = sorted((b.start * 16, b.end * 16) for b in p.blocks if b.kind != "environment")
                return ranges, "memory of %s (PSP %04X; %s)" % (p.name or "program", p.psp, why), regs["ds"] * 16
            if s == "program":
                raise ValueError("no DOS program found; give an address range instead")
            return [(0, 0xA0000)], "conventional memory", regs["ds"] * 16
        if s == "program":
            raise ValueError("the CPU is in protected mode; use region=\"all\" or an address range")
        return [(0, 0xA0000), (0x100000, 0x10000000)], "all RAM (protected mode)", dos.seg_cache(regs, "ds")["base"]
    if s == "conventional":
        return [(0, 0xA0000)], "conventional memory", None
    if s == "all":
        return [(0, 0x10000000)], "all memory", None
    for sep in ("-", "+"):
        if sep in s:
            a, b = s.split(sep, 1)
            lo = _resolve(a, regs).linear
            hi = _resolve(b, regs).linear if sep == "-" else lo + dos.parse_number(b)
            if hi <= lo:
                raise ValueError("empty range %r" % spec)
            return [(lo, hi)], "%08X-%08X" % (lo, hi), None
    raise ValueError("region is auto, program, conventional, all, START-END or START+LENGTH (hex/SEG:OFF)")


def _label(regs, a, ds_base):
    if dos.segmented(regs) and a < 0x100000:
        if ds_base is not None and 0 <= a - ds_base <= 0xFFFF:
            return "%08X (DS:%04X)" % (a, a - ds_base)
        return "%08X (%04X:%04X)" % (a, a >> 4, a & 0xF)
    if ds_base and 0 <= a - ds_base:
        return "%08X (DS:%08X)" % (a, a - ds_base)
    return "%08X" % a


@tool
def snapshot_memory(name: str = "a", region: str = "auto") -> str:
    """Save a copy of memory under `name`, to compare later with diff_memory.

    region: "auto" (the running DOS program's memory in real mode; all RAM in
    protected mode), "program", "conventional", "all", or "START-END" /
    "START+LENGTH" (hex or SEG:OFF). Device memory (VGA etc.) is skipped.
    Snapshots persist on disk between commands."""
    c = client()
    regs = _regs()
    ranges, desc, ds_base = _regions(region, regs)
    snap = memscan.take(c, ranges)
    snap.label = desc
    snap.ranges = ranges
    snap.ds_base = ds_base
    memscan.save(_state_dir(), "snap-" + name, snap)
    return "Snapshot %r: %d bytes of %s%s." % (name, snap.size, desc, "" if c.running else " (CPU paused)")


@tool
def diff_memory(before: str = "a", after: str = "now", max_run: int = 16, limit: int = 40) -> str:
    """List memory that differs between two snapshots ("now" = current memory,
    read from the same region as `before`).

    max_run: hide changed runs longer than this many bytes (buffers, screens);
    0 shows all. Typical use: snapshot, do one thing in the game (lose a life,
    press fire), diff_memory: the variables that changed are in the list."""
    c = client()
    d = _state_dir()
    old = memscan.load(d, "snap-" + before)
    if old is None:
        return "No snapshot %r (have: %s)." % (before, ", ".join(n[5:] for n in memscan.list_saved(d) if n.startswith("snap-")) or "none")
    if after == "now":
        new = memscan.take(c, old.ranges)
    else:
        new = memscan.load(d, "snap-" + after)
        if new is None:
            return "No snapshot %r." % after
    regs = _regs()
    runs = memscan.changed_runs(old, new)
    total = sum(n for _, n in runs)
    shown = [r for r in runs if not max_run or r[1] <= max_run]
    lines = ["%d changed byte(s) in %d run(s) between %r and %r (%s)." % (total, len(runs), before, after, old.label)]
    if len(shown) < len(runs):
        lines.append("%d run(s) longer than %d bytes hidden (max_run=0 shows them)." % (len(runs) - len(shown), max_run))
    for a, n in shown[:max(1, limit)]:
        ob, nb = old.read(a, min(n, 16)), new.read(a, min(n, 16))
        vals = ""
        if n <= 4:
            fmt = {1: "<B", 2: "<H", 3: None, 4: "<I"}[n]
            if fmt:
                vals = "  %d -> %d" % (struct.unpack(fmt, ob)[0], struct.unpack(fmt, nb)[0])
        lines.append("%s  %3d byte(s): %s -> %s%s" % (_label(regs, a, getattr(old, "ds_base", None)), n,
                                                      ob.hex(" ").upper(), nb.hex(" ").upper(), vals))
    if len(shown) > limit:
        lines.append("... %d more (raise limit)." % (len(shown) - limit))
    return "\n".join(lines)


def _scan_report(scan, snap, regs, prev=None, limit=20):
    n = scan.count()
    lines = ["%d candidate address(es) for a %d-byte %svalue in %s. Steps: %s." % (
        n, scan.size, "signed " if scan.signed else "", scan.region, " | ".join(scan.steps))]
    if scan.cands is not None and n:
        for a in scan.cands[:limit]:
            v = memscan.value_at(snap, a, scan.size, scan.signed)
            pv = memscan.value_at(prev, a, scan.size, scan.signed) if prev is not None else None
            lines.append("  %s = %s%s" % (_label(regs, a, scan.ds_base), v, "" if pv is None or pv == v else " (was %d)" % pv))
        if n > limit:
            lines.append("  ... %d more." % (n - limit))
    if n > 1:
        lines.append("Change the value in the game, then scan_next (e.g. condition=\"decreased\" or the new value).")
    elif n == 1:
        lines.append("Found it. set_watchpoint on it to catch the code that changes it.")
    else:
        lines.append("No candidates left: the value may be stored differently (size, signed, BCD, x2), or "
                     "elsewhere than this region; start again with scan_memory.")
    return "\n".join(lines)


@tool
def scan_memory(value: str = "", size: int = 1, region: str = "auto", signed: bool = False,
                aligned: bool = False) -> str:
    """Start a value scan to find where the game keeps a number (lives, score,
    ammo...). value: the number shown now (decimal; 0x.. or ..h for hex), or
    empty if unknown. size: 1, 2 or 4 bytes. region: as for snapshot_memory.
    aligned: only addresses that are a multiple of size (fewer false hits for
    32-bit games; 16-bit games often pack variables at odd addresses).
    A negative value makes the scan signed.

    Then change it in the game and call scan_next until one address is left."""
    if size not in (1, 2, 4):
        return "size must be 1, 2 or 4."
    c = client()
    regs = _regs()
    ranges, desc, ds_base = _regions(region, regs)
    number = memscan.parse_number(value) if value.strip() else None
    if number is not None and number < 0:
        signed = True
    snap = memscan.take(c, ranges)
    scan = memscan.Scan(ranges, size, signed, desc, aligned)
    scan.ds_base = ds_base
    scan.start(snap, number)
    d = _state_dir()
    memscan.save(d, "scan-mem", snap)
    memscan.save(d, "scan", scan)
    return _scan_report(scan, snap, regs)


@tool
def scan_next(condition: str, limit: int = 20) -> str:
    """Narrow the value scan (scan_memory) by comparing memory now with the
    previous step. condition: a value ("3", "=3", "!=0", ">100", "<=5"; decimal
    unless 0x or h), "changed", "unchanged", "increased", "decreased", "+N"
    (increased by N) or "-N" (decreased by N)."""
    c = client()
    d = _state_dir()
    scan, old = memscan.load(d, "scan"), memscan.load(d, "scan-mem")
    if scan is None or old is None:
        return "No scan in progress; start one with scan_memory."
    kind, n = memscan.parse_condition(condition)
    new = memscan.take(c, scan.ranges)
    scan.next(old, new, kind, n)
    memscan.save(d, "scan-mem", new)
    memscan.save(d, "scan", scan)
    return _scan_report(scan, new, _regs(), old, limit)


@tool
def restore_memory(snapshot: str, address: str, length: int) -> str:
    """Write bytes back from a snapshot (snapshot_memory) to undo changes, e.g.
    to restore a variable or a patched routine."""
    c = client()
    snap = memscan.load(_state_dir(), "snap-" + snapshot)
    if snap is None:
        return "No snapshot %r." % snapshot
    regs = _regs()
    loc = _resolve(address, regs)
    data = snap.read(loc.linear, length)
    if data is None:
        return "Snapshot %r doesn't cover %d bytes at %s." % (snapshot, length, dos.describe(loc, regs))
    c.write_memory(loc.linear, data)
    return "Restored %d byte(s) at %s from %r: %s" % (length, dos.describe(loc, regs), snapshot,
                                                     data[:32].hex(" ").upper() + (" ..." if length > 32 else ""))


# ---- unattended runs ---------------------------------------------------------------

@tool
def watch_program(command: str = "", seconds: float = 60.0, hang_seconds: float = 8.0,
                  stop_when: str = "exit,crash,hang") -> str:
    """Run a program unattended and report what happened: it exited (and with
    which code), crashed (error/exception text, e.g. "Invalid Opcode" or a DOS
    extender's exception dump), hung (the CPU stuck in one small loop, and which
    DOS/BIOS call it is stuck inside), is waiting for a key, or still runs.

    command: typed first to start it (e.g. "GAME" plus newline); empty watches
    whatever runs now. seconds: how long to watch at most. hang_seconds: how
    long the CPU must stay in one small loop to count as hung. stop_when: which
    outcomes end the watch early (exit, crash, hang, waiting); the CPU is
    paused when a hang or crash ends it, so it can be inspected.

    The report has the outcome with evidence (CPU position, the calls in
    progress, disassembly), key events from the INT log (program starts, video
    modes, files, failed calls), and the text the program printed."""
    c = client()
    st0 = c.int_status()
    traced = list(st0["vectors"])
    want = sorted(set(traced) | set(watchdog.LOG_VECTORS))
    if want != sorted(traced):
        c.int_trace(want)
    start_seq = st0["next"]
    stops = {w.strip() for w in stop_when.lower().split(",") if w.strip()}
    _type_command(c, command)
    if not c.running and c.stops.empty():
        c.resume()
    t0 = time.time()
    samples, calls, outcome, detail, sigs = [], {}, None, "", []
    shell_psp, prog_psp, prog_name, last_check, shell_ranges = None, None, "", 0.0, []
    try:
        while True:
            now = time.time() - t0
            if not c.running:
                stop = c.last_stop
                outcome, detail = "stopped", "the CPU stopped: %s" % (stop.describe() if stop else "paused")
                break
            regs = _regs()
            st = c.int_status()
            tail = dosinfo.parse_log(c.int_log(st["next"] - 1, 1)) if st["next"] > st["first"] else []
            activity = (st["next"], tail[0].count if tail else 0)
            samples.append(watchdog.Sample(now, regs["eip"], watchdog.cs_ip_text(regs), st["next"], activity))
            if now - last_check >= 2.0 or now >= seconds:
                last_check = now
                # The whole log: calls still in progress may have started before the watch.
                for x in dosinfo.parse_log(c.int_log(st["first"])):
                    calls[x.seq] = x
                ordered = [calls[k] for k in sorted(calls) if k >= start_seq]
                mem, chain, top, progs = _dos_state(c)
                psp, _ = dosinfo.current_psp(mem, chain, progs)
                if shell_psp is None:
                    shell_psp = next((p.psp for p in progs.values() if p.parent == p.psp), None)
                    if shell_psp is not None:
                        shell_ranges = [(b.start * 16, b.end * 16) for b in progs[shell_psp].blocks]
                execs = [x for x in ordered if x.vector == 0x21 and x.r("ah") == 0x4B and x.r("al") == 0]
                if psp is not None and psp != shell_psp and prog_psp is None:
                    prog_psp, prog_name = psp, progs[psp].name
                elif execs and prog_psp is None:
                    prog_psp = -1  # started and maybe already ended between two checks
                    prog_name = dosinfo.asciiz(execs[0].data["dx"]).replace("/", "\\").rsplit("\\", 1)[-1]
                if psp is not None and psp in progs and dos.segmented(regs):
                    region = [(b.start * 16, b.end * 16) for b in progs[psp].blocks if b.kind != "environment"]
                else:
                    region = [(0, 0xA0000), (0x100000, 0x10000000)]
                ss_base = dos.seg_cache(regs, "ss")["base"]
                sp = regs["esp"] if dos.stack_bits(regs) == 32 else regs["esp"] & 0xFFFF
                sigs.append((now, watchdog.progress_signature(c, region, ss_base + sp)))
                text = watchdog.output_text(ordered)
                bad = watchdog.crash_lines(text)
                exits = [x for x in watchdog.own_calls(ordered, shell_ranges)
                         if x.vector == 0x21 and x.r("ah") in (0x4C, 0x31) and
                         (not execs or x.seq > execs[0].seq)]
                if prog_psp is not None and psp == shell_psp and exits:
                    code = exits[-1].r("al")
                    errs = watchdog.error_lines(text)
                    outcome = "crashed" if bad else ("exited with error" if code or errs else "exited")
                    detail = "%s ended (INT 21h AH=%02Xh) with exit code %d" % (
                        prog_name or "the program", exits[-1].r("ah"), code)
                    if bad or errs:
                        detail += '; it printed: "%s"' % '" | "'.join((bad or errs)[-3:])
                    break
                if bad and "crash" in stops:
                    outcome, detail = "crashed", 'it printed: "%s"' % '" | "'.join(bad[-3:])
                    break
            kind, why = watchdog.classify(samples, [calls[k] for k in sorted(calls)], hang_seconds, sigs,
                                          st.get("tsc", 0), st.get("hz", 0))
            if kind in ("hung", "waiting") and kind.replace("hung", "hang") in stops:
                outcome, detail = kind, why
                break
            if now >= seconds:
                outcome, detail = kind if kind != "unknown" else "running", why
                break
            time.sleep(0.5)
    finally:
        if want != sorted(traced):
            try:
                c.int_trace(traced)
            except (GdbError, OSError):
                pass
    for x in dosinfo.parse_log(c.int_log(c.int_status()["first"])):
        calls[x.seq] = x
    everything = [calls[k] for k in sorted(calls)]
    ordered = watchdog.own_calls([x for x in everything if x.seq >= start_seq], shell_ranges)
    if outcome in ("hung", "crashed") and c.running:
        c.pause()
    regs = _regs()
    lines = ["OUTCOME: %s after %.0fs: %s." % (outcome.upper(), time.time() - t0, detail)]
    inner, chain = watchdog.innermost_pending(everything)
    if outcome in ("hung", "waiting", "running", "stopped"):
        lines.append("CPU at %s%s (%s mode)." % (watchdog.cs_ip_text(regs), _sym(regs["eip"]), dos.cpu_mode(regs)))
        if chain:
            lines.append("Calls in progress (outermost first):")
            for x in chain[-6:]:
                what, _ = dosinfo.describe(x)
                lines.append("  #%d INT %02Xh %s  (from %s%s%s)" % (x.seq, x.vector, what, x.caller(), _sym(x.caller_linear()),
                                                                "" if x.mode == 0 else " [%s]" % dosinfo.MODES[x.mode]))
    ev = watchdog.summarize(ordered)
    if ev:
        lines += ["", "Key events (INT log):"] + ["  " + e for e in ev]
    failed = watchdog.failed_calls(ordered)
    if failed:
        lines += ["", "Failed calls:"]
        for x in failed[-8:]:
            what, result = dosinfo.describe(x)
            lines.append("  #%d INT %02Xh %s -> %s  (from %s)" % (x.seq, x.vector, what, result, x.caller()))
    text = watchdog.output_text(ordered).rstrip()
    if text:
        tail = text.splitlines()[-12:]
        lines += ["", "Text it printed through DOS/BIOS (last lines):"] + ["  | " + t for t in tail]
    try:
        scr = dos.read_text_screen(c, False)
        if scr.get("text"):
            rows = [r.rstrip() for r in scr["text"].splitlines() if r.strip()]
            lines += ["", "Screen (text mode, last rows):"] + ["  | " + r for r in rows[-8:]]
        else:
            lines += ["", "Screen: video mode %02Xh (graphics); use screenshot to see it." % scr.get("mode", 0)]
    except (GdbError, ValueError, KeyError):
        pass
    if outcome in ("hung", "crashed", "stopped"):
        lines += ["", "CPU state (paused for inspection):", _state_report(None, code_lines=10)]
    else:
        lines += ["", "The CPU is %s." % ("running" if c.running else "paused")]
    return "\n".join(lines)


# ---- symbols from linker maps --------------------------------------------------------

def _parse_bases(text):
    bases = {}
    for item in text.replace(",", " ").split():
        k, v = item.split("=")
        bases["load" if k.lower() in ("load", "seg", "segment") else int(k, 0)] = int(v.rstrip("hH"), 16)
    return bases


@tool
def load_symbols(map_path: str, bases: str = "auto") -> str:
    """Load an Open Watcom linker map (.map) so that addresses can be given as
    names (set_breakpoint address="TS_ServiceSchedule_", read_memory
    address="_HeadTask") and are shown as name+offset in disassembly, the CPU
    state and stack dumps. map_path is a file on the machine running the bridge.

    bases: where the program is loaded. "auto" works it out: for a 16-bit
    program from the last wait_for_program_start or DOS's running program
    (load segment = PSP+10h); for a 32-bit DOS/4GW-style program from the last
    wait_for_program_start(protected_mode=true) (entry point and DPMI blocks).
    Or give them: "load=240E" (16-bit) or "1=174000,2=1FF000" (object bases)."""
    m = symbols.MapFile(map_path)
    d = _state_dir()
    if bases.strip().lower() not in ("", "auto"):
        b = _parse_bases(bases)
        how = "as given"
    else:
        start = memscan.load(d, "last-start") or {}
        image = os.path.splitext(os.path.basename(m.image or map_path))[0].upper()
        started = os.path.splitext(start.get("path", "").replace("/", "\\").rsplit("\\", 1)[-1])[0].upper()
        if m.wide:
            if start.get("kind") != "protected":
                return ("Can't place a 32-bit map automatically: run wait_for_program_start(name, "
                        "protected_mode=true) first, or give bases like \"1=174000,2=1FF000\".")
            b = symbols.guess_object_bases(m, start["entry"], start["blocks"])
            how = "from the entry point and DPMI blocks of %s" % start["path"]
            if image and started and image != started:
                how += " (NOTE: the map is for %s)" % image
            missing = sorted(set(m.object_sizes()) - set(b))
            if missing:
                how += "; no block found for object(s) %s" % ", ".join(map(str, missing))
        else:
            if start.get("kind") == "real" and (not image or image == started):
                b, how = {"load": start["load"]}, "from the start of %s" % start["path"]
            else:
                regs = _regs()
                mem, chain, top, progs = _dos_state(c := client())
                p = next((p for p in progs.values() if p.name.upper() == image), None)
                if p is None:
                    p, _ = _current_program(regs, progs, mem, chain)
                if p is None:
                    return "Can't find the program in DOS memory; give bases like \"load=240E\"."
                b, how = {"load": p.load_segment}, "from %s in DOS memory (PSP %04X)" % (p.name, p.psp)
    memscan.save(d, "symbols", {"path": os.path.abspath(map_path), "bases": b})
    _symcache["key"] = None
    table = _symbols()
    dos.namer = lambda lin, dist: table.name_at(lin, dist)
    return "Loaded %d symbols from %s; %s (%s)." % (len(table.by_addr), map_path, table.describe_bases(), how)


@tool
def lookup_symbol(query: str, limit: int = 30) -> str:
    """Find symbols: a name ("HeadTask", "_HeadTask+8"), a pattern with * ("TS_*",
    "*score*"), or an address (shows the symbol it falls in)."""
    table = _symbols()
    if table is None:
        return "No symbols loaded; use load_symbols."
    q = query.strip()
    if "*" in q or "?" in q:
        rx = re.compile("^" + re.escape(q).replace(r"\*", ".*").replace(r"\?", ".") + "$", re.I)
        hits = [(a, n) for a, n in table.by_addr if rx.match(n)]
        lines = ["%08X  %s" % h for h in hits[:limit]]
        if len(hits) > limit:
            lines.append("... %d more" % (len(hits) - limit))
        return "\n".join(lines) or "No match."
    lin = table.lookup(q)
    if lin is not None:
        return "%s = linear %08X" % (q, lin)
    try:
        loc = dos.resolve(q, _regs(), client())
    except ValueError as e:
        return "Not a known symbol or address: %s" % e
    n = table.name_at(loc.linear, 0x10000)
    return "%08X is %s" % (loc.linear, n or "not within 64 KiB after any symbol (heap, stack or another program)")



_AREAS = [(0x0, 0x400, "real-mode interrupt vector table"), (0x400, 0x500, "BIOS data area"),
          (0x500, 0x600, "DOS communication area"), (0xA0000, 0xC0000, "video memory"),
          (0xC0000, 0xC8000, "video BIOS ROM"), (0xC8000, 0xF0000, "adapter ROM / upper memory"),
          (0xF0000, 0x100000, "system BIOS ROM"), (0x100000, 0x10FFF0, "high memory area (HMA)")]


@tool
def what_is(address: str) -> str:
    """Say what an address is: the symbol it belongs to (load_symbols), the PC
    memory area (vector table, BIOS data, video memory, ROM, HMA), the DOS memory
    block and its owner (program, environment, DOS system data/code, free), and
    for DOS-extender programs the DPMI block it lies in. Handy after a watchpoint
    or a crash address."""
    regs = _regs()
    loc = _resolve(address, regs)
    lin = loc.linear
    out = ["%s = linear %08X" % (address, lin)]
    table = _symcache["table"] if dos.namer else None
    if table is not None:
        n = table.name_at(lin, 0x10000)
        if n:
            out.append("symbol: %s" % n)
    for lo, hi, what in _AREAS:
        if lo <= lin < hi:
            extra = " (INT %02Xh vector)" % (lin // 4) if hi == 0x400 else ""
            out.append("area: %s%s, offset %X" % (what, extra, lin - lo))
    if lin < 0xA0000 or 0xC0000 <= lin < 0x100000:
        mem, chain, top, progs = _dos_state(client())
        for b in chain or []:
            if b.contains_linear(lin):
                owner = ("program %s (PSP %04X)" % (b.program, b.owner)) if b.program else ("owner %04X" % b.owner)
                out.append("DOS memory: %s block %04X-%04X, %s, offset %X into it" % (
                    b.kind, b.start, b.end, owner if b.owner not in (0, 8) else ("DOS" if b.owner == 8 else "free"),
                    lin - b.start * 16))
                break
        else:
            if chain and lin < chain[0].mcb * 16:
                out.append("DOS memory: below the first memory block (DOS kernel data, buffers, device drivers)")
    start = memscan.load(_state_dir(), "last-start") or {}
    for i, (lo, hi) in enumerate(start.get("blocks", [])):
        if lo <= lin < hi:
            out.append("DPMI block #%d %08X-%08X allocated after %s was opened, offset %X" % (
                i + 1, lo, hi, start.get("path", "the program"), lin - lo))
    if len(out) == 1:
        out.append("nothing known about it (heap, stack, unmapped or another program's memory)")
    return "\n".join(out)


if __name__ == "__main__":
    mcp.run()
