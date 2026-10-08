"""MCP server exposing a running 86Box (built with -DGDBSTUB=ON) as debugger
tools for an AI client such as Claude Code or Claude Desktop.

Run:  python3 server.py           (stdio transport)
Env:  BOX86_GDB_HOST (default 127.0.0.1), BOX86_GDB_PORT (default 12345)
"""

import collections
import functools
import json
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
import codeanalysis
import libsigs
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
placement). Without a map, identify_functions names the C runtime and other
library functions (printf_, inp_, strtoul_...) from the compiler's libraries,
and every other called function sub_ADDRESS.
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
            # Points can outlive a connection when the stub holds them (run_tool.py);
            # what they were set as is kept in the state directory.
            try:
                with open(os.path.join(memscan.state_dir(host, port), "points.json")) as f:
                    saved = json.load(f)
            except (OSError, ValueError, RuntimeError):
                saved = {}
            for kind, addr, length in c.list_points() or []:
                info = saved.get("%d:%X" % (kind, addr), {})
                _points[(kind, addr)] = {"kind": kind, "address": addr, "length": length,
                                         "expr": info.get("expr", "set by an earlier command"),
                                         "condition": info.get("condition", "")}
        return _client


def _save_points():
    try:
        with open(os.path.join(_state_dir(), "points.json"), "w") as f:
            json.dump({"%d:%X" % key: {"expr": p["expr"], "condition": p.get("condition", "")}
                       for key, p in _points.items()}, f)
    except (OSError, RuntimeError):
        pass


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
    if info and info.get("identified") and \
            (memscan.load(d, "last-start") or {}).get("tsc") != info.get("start_tsc"):
        info = None  # identified names belong to the program started then; another has started
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
    except (GdbError, OSError):
        pass
    except Exception:  # a broken saved symbol state must not break every tool
        try:
            os.remove(os.path.join(_state_dir(), "symbols.pkl"))
        except OSError:
            pass
    dos.namer = (lambda lin, dist: table.name_at(lin, dist)) if table else None
    dos.peek = _peek_bytes
    return fn(*args, **kwargs)


def _peek_bytes(linear, length):
    """Bytes at linear (RAM/ROM, without side effects), or None if any are unreadable."""
    runs = client().peek(linear, length)
    if not runs or any(b is None for _, b in runs):
        return None
    return b"".join(b for _, b in runs)


def _sym(linear, dist=0x10000):
    """' (name+off)' for a linear address, or ''."""
    return " (%s)" % dos.namer(linear, dist) if dos.namer and dos.namer(linear, dist) else ""

def _regs():
    return client().read_registers()


def _resolve(address, regs):
    table = _symcache["table"] if dos.namer else None
    if table is not None:
        head = address.strip().split("+")[0].split("-")[0].strip()
        # Plain hex numbers stay numbers ("beef" is BEEFh even if a symbol is named so).
        if head and not dos.is_register(head) and not re.fullmatch(r"(0x)?[0-9a-fA-F]+h?", head):
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
            and regs["ebp"] >= 0x1000:  # small values: EBP used as a plain register
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
    _mark_resume(c)
    c.resume()
    return "Resumed."


def _mark_resume(c):
    """Remember the stub's stop count at a resume, so a later wait_for_stop
    (in another run_tool command) knows a stop since then is new."""
    try:
        memscan.save(_state_dir(), "resume-mark", {"stops": c.stub_state()["stops"]})
    except (GdbError, OSError):
        pass


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
        mark = memscan.load(_state_dir(), "resume-mark") or {}
        try:
            stops = c.stub_state()["stops"]
        except GdbError:
            stops = None
        if stops is not None and mark.get("stops") is not None and stops > mark["stops"]:
            memscan.save(_state_dir(), "resume-mark", {"stops": stops})  # report it once
            return "Already stopped (it stopped after the last resume, before this wait):\n\n" + _state_report(c.last_stop)
        return ("No new stop: the CPU was already stopped (resume it first).\n\n" + _state_report())
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
    """Hard-reset the emulated machine (like pressing the reset button). Loaded
    symbols, recorded patches and the last program start are forgotten."""
    client().monitor("r")
    _forget_program()
    return "Machine reset (symbols, patches and the last program start forgotten)."


def _forget_program():
    """Drop the state that describes the program in memory."""
    d = _state_dir()
    for name in ("symbols", "patches", "last-start"):
        try:
            os.remove(os.path.join(d, name + ".pkl"))
        except OSError:
            pass
    _symcache.update(key=None, table=None)
    dos.namer = None


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
def search_memory(pattern: str = "", start: str = "", end: str = "", as_text: bool = False,
                  max_results: int = 64, text: str = "") -> str:
    """Search guest memory for a byte pattern (hex, "??" wildcard) or text
    (text=..., or a quoted pattern, or as_text=true).

    Default range: all RAM. Device memory (VGA etc.) is skipped, and searching has no side effects."""
    if text:
        pattern, as_text = text, True
    if not pattern:
        return "Give pattern= (hex bytes) or text=."
    c = client()
    regs = _regs()
    start_loc = _resolve(start, regs) if start else None
    if start or end:
        lo = start_loc.linear if start_loc else 0
        hi = _resolve(end, regs).linear if end else (lo + 0x110000 if dos.segmented(regs) else 0x10000000)
        ranges = [(lo, hi)]
    else:
        # All RAM, also in real mode: a protected-mode program is often caught
        # in real mode (inside DOS or its extender), and its data is above 1 MiB.
        ranges = [(0, 0x10000000)]
    if len(pattern) >= 2 and pattern[0] == pattern[-1] and pattern[0] in "\"'":
        pattern, as_text = pattern[1:-1], True  # a quoted pattern is text
    if as_text:
        rx = re.escape(pattern.encode("cp437"))
    else:
        toks = pattern.replace(",", " ").split()
        if not all(re.fullmatch(r"[0-9a-fA-F]{1,2}|\?\??", t) or re.fullmatch(r"(?:[0-9a-fA-F]{2})+", t)
                   for t in toks):
            return ("The pattern is hex bytes (\"B8 00 4C ?? CD 21\"); for text, quote it or pass "
                    "as_text=true.")
        if len(toks) == 1 and len(toks[0]) > 2:
            toks = [toks[0][i:i + 2] for i in range(0, len(toks[0]), 2)]
        if not toks:
            return "Empty pattern."
        rx = b"".join(b"." if t in ("??", "?") else re.escape(bytes([int(t, 16)])) for t in toks)
    rx = re.compile(rx, re.S)
    hits, searched = [], 0
    for lo, hi in ranges:
        snap = memscan.take(c, [(lo, hi)])
        for a, d in snap.runs:
            searched += len(d)
            for m in rx.finditer(d):
                hits.append(a + m.start())
                if len(hits) >= max_results:
                    break
            if len(hits) >= max_results:
                break
    real = dos.segmented(regs)
    where = "searched %s KiB of RAM/ROM in %s" % (searched // 1024, ", ".join("%08X-%08X" % r for r in ranges))
    if not hits:
        return "No matches (%s)." % where

    def seg_note(a):
        if start_loc is not None and start_loc.sel is not None and 0 <= a - start_loc.base <= 0xFFFFFFFF:
            return " (%s)" % dos._label(start_loc, a)
        if not real:
            return _sym(a, 0x1000)
        if a < 0x100000:
            return " (%04X:%04X)" % (a >> 4, a & 0xF)
        if a < 0x10FFF0:  # the HMA, reachable as FFFF:xxxx
            return " (FFFF:%04X)" % (a - 0xFFFF0)
        return ""
    more = " (stopped at max_results)" if len(hits) >= max_results else ""
    found = set(hits)
    a20_off = None

    def mirror(a):
        # With the A20 gate off, bit 20 of an address is ignored: what is
        # above 1 MB with it set is the first MB again.
        nonlocal a20_off
        if not (a & 0x100000 and (a ^ 0x100000) in found):
            return ""
        if a20_off is None:
            try:
                a20_off = c.read_memory(0x100000, 64) == c.read_memory(0, 64)
            except GdbError:
                a20_off = False
        if a20_off:
            return "  (same place as %08X: A20 is off, so this is the first MB seen again)" % (a ^ 0x100000)
        return "  (same bytes as at %08X: a copy)" % (a ^ 0x100000)
    return "%d match(es)%s, %s:\n" % (len(hits), more, where) + "\n".join(
        "%08X%s%s" % (h, seg_note(h), mirror(h)) for h in hits)


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
        bits = loc.bits or _linear_code_bits(regs, loc.linear)
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
            # Return addresses: the value follows a call instruction (near,
            # in the current code segment; a far return in real mode is
            # IP then CS, checked with the next word).
            if _looks_like_return(c, lin, None, 32 if w == 4 else 16):
                line += "  <- return address (after a call at %08X)" % _call_before(c, lin, w)
            elif w == 2 and dos.segmented(regs) and i + 1 < entries:
                nxt = c.read_memory((ss["base"] + ((sp + (i + 1) * w) & mask)) & 0xFFFFFFFF, 2)
                if len(nxt) == 2:
                    far = int.from_bytes(nxt, "little") * 16 + val
                    if dos.segmented(regs) and 0x500 <= far < 0x100000 and _looks_like_far_return(c, far):
                        line += "  <- far return address %04X:%04X" % (int.from_bytes(nxt, "little"), val)
        lines.append(line)
    return "\n".join(lines)


# ---- breakpoints / watchpoints ----------------------------------------------

_COND_REGS = ("eax", "ebx", "ecx", "edx", "esi", "edi", "ebp", "esp")  # the stub's order
_COND_OPS = {"==": 0, "=": 0, "!=": 1, "<>": 1, "<": 2, ">": 3, "<=": 4, ">=": 5, "&": 6, "changed": 7}


def _cond_value(text, regs):
    """A condition's constant: hex like every number here, "#" for decimal,
    "-" for negative, or a symbol/address."""
    t = text.strip()
    neg = t.startswith("-")
    t = t[1:].strip() if neg else t
    if t.startswith("#"):
        v = int(t[1:])
    else:
        try:
            v = dos.parse_number(t)
        except ValueError:
            v = _resolve(t, regs).linear
    return (-v if neg else v) & 0xFFFFFFFF


def _parse_condition(text, regs, watch=None):
    """"[byte|word|dword] LHS OP VALUE" or "LHS changed" -> ((src, reg, arg, size,
    op, value) for the stub's "bc", notes). LHS is a register, "[reg+disp]" with
    an optional ds:/ss: prefix (EBP/ESP default to SS), or "[address]". For a
    watchpoint (watch = (linear, length)) LHS may be left out: its own memory."""
    t = " ".join(text.strip().lower().split())
    size, notes = None, []
    m = re.match(r"(byte|word|dword)\s+", t)
    if m:
        size = {"byte": 1, "word": 2, "dword": 4}[m.group(1)]
        t = t[m.end():]
    m = re.match(r"(.*?)\s*(==|!=|<>|<=|>=|=|<|>|&|\bchanged$)\s*(.*)$", t)
    if not m:
        raise ValueError("a condition is LHS OP VALUE (OP: == != < > <= >= &) or LHS changed, not %r" % text)
    lhs, op, rhs = m.group(1).strip(), _COND_OPS[m.group(2)], m.group(3).strip()
    if (op == 7) != (rhs == ""):
        raise ValueError("%r needs a value after its operator" % text if rhs == "" else
                         "\"changed\" takes no value (%r)" % text)
    if lhs == "":
        if watch is None:
            raise ValueError("say what the condition tests, e.g. \"eax == 5\" or \"[esi+4] > 0\"")
        src, reg, arg = 1, 0, watch[0]
        size = size or (1 if watch[1] == 1 else 2 if watch[1] < 4 else 4)
        if watch[1] > 4:
            notes.append("the condition tests the first %d bytes of the watched memory" % size)
    elif not lhs.startswith("["):
        if lhs in _COND_REGS:
            reg, rsize = _COND_REGS.index(lhs), 4
        elif lhs in dos.REG16:
            reg, rsize = _COND_REGS.index(dos.REG16[lhs]), 2
        elif lhs in dos.REG8:
            # the stub numbers AH BH CH DH 8-11
            reg, rsize = _COND_REGS.index(dos.REG8[lhs][0]) + (8 if dos.REG8[lhs][1] else 0), 1
        else:
            raise ValueError("%r is not a register this can test (EAX-ESP and their 16/8-bit parts); "
                             "memory goes in brackets: [address]" % lhs)
        src, arg, size = 0, 0, size or rsize
    else:
        if not lhs.endswith("]"):
            raise ValueError("unbalanced [ in %r" % lhs)
        inner = lhs[1:-1].strip()
        seg = None
        if re.match(r"(ds|ss)\s*:", inner) and \
                re.match(r"(ds|ss)\s*:\s*e?(ax|bx|cx|dx|si|di|bp|sp)\b", inner):
            seg, inner = inner[:2], inner.split(":", 1)[1].strip()
        if re.match(r"(cs|es|fs|gs)\s*:\s*e?(ax|bx|cx|dx|si|di|bp|sp)\b", inner):
            raise ValueError("register operands can be DS- or SS-relative only, not %s" % inner[:2].upper())
        rm = re.match(r"(e?(?:ax|bx|cx|dx|si|di|bp|sp))\b\s*(.*)$", inner)
        if rm:
            name = rm.group(1)
            full = name if name in _COND_REGS else dos.REG16[name]
            reg = _COND_REGS.index(full)
            arg = dos.parse_term("0" + rm.group(2), None) & 0xFFFFFFFF if rm.group(2) else 0
            seg = seg or ("ss" if full in ("ebp", "esp") else "ds")
            src = 2 if seg == "ds" else 3
            if name not in _COND_REGS and regs[full] >> 16:
                notes.append("the base is all of %s (%08X now), not just %s" % (full.upper(), regs[full], name.upper()))
        else:
            src, reg, arg = 1, 0, _resolve(inner, regs).linear
        size = size or (4 if dos.code_bits(regs) == 32 else 2)
    value = 0 if op == 7 else _cond_value(rhs, regs)
    return (src, reg, arg, size, op, value), notes


def _apply_condition(c, kind, linear, condition, regs, watch=None):
    """Set (or with "" remove) a point's condition; returns a note for the reply."""
    if not condition.strip():
        try:
            c.set_condition(kind, linear, None)
        except GdbError:
            pass  # an older build without conditions
        return ""
    try:
        cond, notes = _parse_condition(condition, regs, watch)
    except ValueError:
        c.clear_point(kind, linear, watch[1] if watch else 1)
        raise
    try:
        c.set_condition(kind, linear, cond)
    except GdbError as e:
        c.clear_point(kind, linear, watch[1] if watch else 1)
        raise GdbError("could not set the condition (%s); the point was removed" % e)
    return " Stops only when %s.%s" % (condition.strip(), "".join(" Note: %s." % n for n in notes))


def _set_point(c, kind, linear, length):
    """Set a point; one of the same kind already at `linear` is replaced (to
    change its length or condition)."""
    old = _points.pop((kind, linear), None)
    if old is not None:
        try:
            c.clear_point(kind, linear, old["length"])
        except GdbError:
            pass  # gone from the stub already
    c.set_point(kind, linear, length)


@tool
def set_breakpoint(address: str, condition: str = "") -> str:
    """Set an execution breakpoint (hardware-style; works in ROM and does not modify memory).

    SEG:OFF is resolved to a linear address now, in the current CPU mode.

    condition: stop only when it holds when the instruction is reached, e.g.
    "eax == 5", "cx > 100", "byte [esi+3] & 80", "[ss:ebp-8] != 0",
    "word [player_hp] < #10", "[esi] changed" (differs from the last time
    the breakpoint was reached, or from when it was set). Operators: == != < >
    <= >= & (any bit set; comparisons are unsigned), changed. Numbers are hex;
    #10 is decimal, -1 is FFFFFFFF. Registers: EAX-ESP, AX-SP, AL-DH. Memory in
    brackets: [reg+disp] (DS-relative; EBP/ESP are SS-relative; ds:/ss: to
    choose), [linear], [SEG:OFF] or [symbol] (fixed addresses, resolved now);
    byte/word/dword before it sets the size (default: dword in 32-bit code,
    word in 16-bit). Passes where it is false are counted (list_breakpoints).
    Setting a breakpoint where there is one replaces it (and its condition)."""
    c = client()
    regs = _regs()
    loc = _resolve(address, regs)
    linear = loc.linear
    _set_point(c, BP_HARDWARE, linear, 1)
    note = _apply_condition(c, BP_HARDWARE, linear, condition, regs)
    _points[(BP_HARDWARE, linear)] = {"kind": BP_HARDWARE, "address": linear, "length": 1, "expr": address,
                                      "condition": condition.strip()}
    _save_points()
    return "Breakpoint set at %s = %s.%s" % (address, dos.describe(loc, regs), note)


@tool
def set_watchpoint(address: str, length: int = 1, kind: str = "write", cpu_mode: str = "",
                   condition: str = "") -> str:
    """Stop when memory in [address, address+length) is accessed. kind: write, read or access.

    cpu_mode: "protected" or "real" makes ALL watchpoints catch only accesses
    made in that CPU mode ("any" undoes it; empty leaves it as is). E.g. watch
    DOS memory with cpu_mode="protected" to find a DOS-extender program writing
    into it, while DOS's own (real-mode) writes don't stop the CPU.

    condition: stop only when it holds after the access, as in set_breakpoint.
    A condition starting with its operator tests the watched memory itself
    (its first byte/word/dword, by length): "== 0", "> #100", "& 8000",
    "changed" (a write that changes the value), "byte changed". Others can
    test anything, e.g. "eax == 3" or "[esi] < 0"."""
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
    _set_point(c, k, linear, max(1, length))
    note = _apply_condition(c, k, linear, condition, regs, (linear, max(1, length))) + note
    _points[(k, linear)] = {"kind": k, "address": linear, "length": max(1, length), "expr": address,
                            "condition": condition.strip()}
    _save_points()
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
    _save_points()
    if not removed:
        return "Nothing to remove at %s." % address
    return "Removed %d point(s):\n%s" % (len(removed), "\n".join(removed))


@tool
def list_breakpoints() -> str:
    """List breakpoints, watchpoints and interrupt catches."""
    hits, misses = {}, {}
    try:
        client().list_points()
        hits = getattr(client(), "point_hit_counts", {})
        misses = getattr(client(), "point_cond_misses", {})
    except GdbError:
        pass
    lines = []
    for p in _points.values():
        key = (p["kind"], p["address"])
        line = "%-12s %08X len=%d  (%s)" % (KIND_NAMES[p["kind"]], p["address"], p["length"], p["expr"])
        if p.get("condition"):
            line += "  if %s" % p["condition"]
        if key in hits:
            line += "  hits=%d" % hits[key]
        if key in misses:
            line += "  false=%d" % misses[key]
        lines.append(line)
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
def read_text_screen(include_attributes: bool = False, plain: bool = False, format: str = "text"):
    """Read the current text-mode screen (from the BIOS video mode and video RAM).

    Returns a header line (video mode, size, cursor) and the screen text, one
    row per line. plain=true: the text only. format="json" (or
    include_attributes=true, which adds colours): the details as JSON."""
    result = dos.read_text_screen(client(), include_attributes)
    if include_attributes or format.lower() == "json":
        return result
    if result.get("text") is None:
        return result.get("note") or json.dumps(result)
    if plain:
        return result["text"]
    cur = result.get("cursor") or {}
    head = "mode %02Xh, %sx%s, cursor row %s col %s" % (
        result.get("mode", 0), result.get("columns", "?"), result.get("rows", "?"), cur.get("row", "?"),
        cur.get("col", "?"))
    return head + "\n" + result["text"]


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
        _mark_resume(c)
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
    """Type text as keystrokes (US layout). Use "\\n" for Enter, e.g. "dir\\n".
    Typing ahead is fine: it waits while the BIOS keyboard buffer is nearly
    full, so keys aren't lost while a command is still running."""
    c = client()
    plan = [pcinput.char_keys(ch) for ch in text]
    note = _ensure_running()
    shift = pcinput.KEYS["shift"]
    for n, (needs_shift, scan) in enumerate(plan):
        _wait_key_buffer(c)
        pcinput.press_combo(c, [shift, scan] if needs_shift else [scan], delay_ms / 2000.0)
        time.sleep(delay_ms / 2000.0)
        stopped = _stopped_during_input(n + 1, len(plan), "characters")
        if stopped:
            return stopped
    return "Typed %d character(s)%s." % (len(plan), note)


def _wait_key_buffer(c, limit=8, timeout=30.0):
    """Wait while the BIOS keyboard buffer holds `limit` or more keys: a busy
    program (DOS copying a file) doesn't read them, and the BIOS drops keys
    once its 15-key buffer is full. Programs with their own keyboard handler
    don't use the buffer, so it stays empty and this returns at once."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            bda = c.read_memory(0x41A, 4) + c.read_memory(0x480, 4)
        except GdbError:
            return
        if len(bda) < 8:
            return
        head, tail, start, end = (int.from_bytes(bda[i:i + 2], "little") for i in (0, 2, 4, 6))
        if not (0x1E <= start < end <= 0x200 and start <= head < end and start <= tail < end):
            start, end = 0x1E, 0x3E  # BIOS default when 0040:0080/0082 aren't set
            if not (start <= head < end and start <= tail < end):
                return
        if ((tail - head) % (end - start)) // 2 < limit:
            return
        time.sleep(0.1)


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


# DOS extenders' own files, which they open before the program they run.
_EXTENDER_FILES = {"DOS4GW.EXE", "DOS4G.EXE", "DOS32A.EXE", "PMODEW.EXE", "CWSTUB.EXE", "CW32.EXE"}


def _v86_monitor_segments(c):
    """Skips for an EMM386-style V86 monitor's code, from its IDT, if one is
    running DOS: (selector, base) of its handlers' code segments and
    ("range", lo, hi) around the handlers; [] otherwise."""
    regs = _regs()
    # Called before the program starts: protected mode on means a V86 monitor
    # runs DOS (the CPU may be stopped inside the monitor rather than in V86).
    if not regs["cr0"] & 1 or not regs.get("seg"):
        return []
    idt, gdt = regs["seg"]["idt"], regs["seg"]["gdt"]
    try:
        raw = c.read_memory(idt["base"], min(idt["limit"] + 1, 256 * 8))
    except GdbError:
        return []
    gates = [(int.from_bytes(raw[i + 2:i + 4], "little") & ~3,
              int.from_bytes(raw[i:i + 2], "little") | int.from_bytes(raw[i + 6:i + 8], "little") << 16)
             for i in range(0, len(raw) - 7, 8) if raw[i + 5] & 0x80 and raw[i + 5] & 0x0F in (0xE, 0xF)]
    out, bases, handlers = [], {}, []
    for sel in sorted({g[0] for g in gates})[:8]:
        if sel & 4 or not sel or sel + 8 > gdt["limit"] + 1:
            continue
        d = c.read_memory(gdt["base"] + sel, 8)
        if len(d) == 8:
            bases[sel] = d[2] | d[3] << 8 | d[4] << 16 | d[7] << 24
            out.append((sel, bases[sel]))
    handlers = [(bases[sel] + off) & 0xFFFFFFFF for sel, off in gates if sel in bases]
    if handlers:
        # Its code (also the VCPI server, which runs under the client's own
        # selectors) lies around its interrupt handlers.
        out.append(("range", min(handlers) & ~0xFFFF, (max(handlers) | 0xFFFF) + 1 + 0x10000, regs["cr3"]))
    return out


def _extender_code(c, regs):
    """Whether 32-bit code at CS:EIP looks like a DOS extender's own: CPL 3
    with paging on, as in CauseWay's kernel. (DOS/4GW, DOS/32A and PMODE/W
    run the program at CPL 0 without paging.)"""
    return (regs.get("cr0", 0) >> 31) & 1 == 1 and (regs["cs"] & 3) == 3


def _stub_tsc(c):
    try:
        return c.int_status().get("tsc", 0)
    except GdbError:
        return 0


def _last_start(c):
    """The last program start this bridge saw, or {} if the machine has been
    reset or restarted since (its time counter went backwards)."""
    start = memscan.load(_state_dir(), "last-start") or {}
    if start.get("tsc") and _stub_tsc(c) < start["tsc"]:
        return {}
    return start


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
        refl = dosinfo.is_reflection(at_depth.get(x.depth - 1), x)
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
    (tested with DOS/4GW, DOS/32A, PMODE/W and CauseWay): after the program
    file is opened, it stops at the first 32-bit protected-mode instruction or
    when execution enters DPMI memory allocated after the open, passing over an
    extender's own 32-bit code (CauseWay's) until Open Watcom's startup code.
    That is the program's 32-bit entry point. Use it after (or instead of) the
    real-mode start, which for such programs is just the extender stub."""
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
                                                          "load": p.load_segment, "cs": regs["cs"],
                                                          "tsc": _stub_tsc(c)})
                lines.append("PSP %04X; load segment %04X (PSP+10h; add it to segment values from the "
                             "program's linker map or EXE header). DS=ES=PSP at entry." % (p.psp, p.load_segment))
                lines.append("Memory: " + ", ".join("%04X-%04X %s" % (b.start, b.end, b.kind) for b in p.blocks))
            lines.append("If this is a DOS-extender stub (e.g. a DOS/4GW game), call "
                         "wait_for_program_start(protected_mode=true) to continue to its 32-bit code.")
            return "\n".join(lines) + "\n\n" + _entry_report(stop)

        # Protected mode: find the extender opening the program, then DPMI blocks allocated after.
        # With an EMM386-style V86 monitor loaded, every interrupt from V86 mode
        # runs the monitor's 32-bit code: note its code segments now, from its IDT.
        monitor = _v86_monitor_segments(c)
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
            # Extenders open the program file from protected mode (DOS/4GW,
            # DOS/32A) or from real mode before switching (PMODE/W).
            reg = "ds:edx" if dos.code_bits(regs) == 32 and not dos.segmented(regs) else "ds:dx"
            if (regs["eax"] >> 8) & 0xFF == 0x6C:
                reg = reg.replace("dx", "si")
            path = dosinfo.asciiz(c.read_memory(_resolve(reg, regs).linear, 80), 80)
            base = path.replace("/", "\\").rsplit("\\", 1)[-1].upper()
            if _name_matches(name, path) and (name or (base.endswith(".EXE") and base not in _EXTENDER_FILES)):
                opened = path
        c.clear_int_catches()
        c.catch_int(0x31, 0x05, 0x01, "ret")
        c.catch_int(0x31, 0x05, 0x03, "ret")
        # The program's entry is its first 32-bit protected-mode instruction
        # (the extenders' own kernels are 16-bit), or execution entering memory
        # allocated through DPMI after the open, whichever comes first.
        blocks, skipped = [], []
        try:
            c.set_exec_ranges([], pm32=True, skip=monitor)
            pm32 = True
        except GdbError:  # older 86Box build: DPMI blocks only
            pm32 = False
        while True:
            c.resume()
            stop = wait()
            if stop is None:
                return ("%s was opened by the extender, but its code didn't start within %.0fs (CPU still running). "
                        "DPMI blocks seen: %s%s" % (opened, timeout_seconds,
                                                    ", ".join("%08X+%X" % (a, b - a) for a, b in blocks) or "none",
                                                    "; 32-bit code passed over (the extender's own?): " +
                                                    ", ".join("%04X:%08X" % x[:2] for x in skipped) if skipped else ""))
            if stop.reason == "range":
                regs = _regs()
                in_block = any(lo <= regs["eip"] < hi for lo, hi in blocks)
                head = c.read_memory(regs["eip"], 8)
                watcom = len(head) == 8 and head[0] == 0xEB and head[2:8] == b"WATCOM"
                # The first 32-bit code is the program's with DOS/4GW, DOS/32A and
                # PMODE/W; CauseWay runs code of its own first. Watcom-built
                # programs start with "jmp short; WATCOM", which settles it.
                # With a V86 monitor (EMM386) its code and its VCPI server run
                # in 32-bit code too: there only the startup signature counts.
                if watcom or (not monitor and (in_block or not skipped) and not _extender_code(c, regs)):
                    break
                skipped.append((regs["cs"], dos.ip(regs) or 0, dos.seg_cache(regs, "cs")["base"]))
                if len(skipped) + len([m for m in monitor if m[0] != "range"]) >= 16:
                    return ("%s: passed over 16 code selectors of 32-bit code without finding the program's entry "
                            "(no Watcom startup signature): %s.\n\n%s" % (
                                opened, ", ".join("%04X:%08X" % x[:2] for x in skipped), _state_report(stop)))
                # From now on only the 32-bit catch: the extender runs code in
                # blocks it allocated too.
                c.set_exec_ranges([], pm32=True, skip=monitor + [(sel, base) for sel, _, base in skipped])
                continue
            if stop.reason != "catch":
                return other_stop(stop)
            recs = dosinfo.parse_log(c.int_log(stop.int_seq, 1)) if stop.int_seq else []
            if recs and recs[0].seq == stop.int_seq and not recs[0].cf():
                r = recs[0]
                size = (r.r("bx") << 16) | r.r("cx")
                lin = (r.r("bx", True) << 16) | r.r("cx", True)
                if size:
                    blocks = [blk for blk in blocks if blk[0] != lin] + [(lin, lin + size)]  # a resize replaces
                    c.set_exec_ranges([] if skipped else sorted(blocks, key=lambda b: b[0] - b[1])[:8], pm32=pm32,
                                      skip=monitor + [(sel, base) for sel, _, base in skipped])
        regs = _regs()
        inside = [b for b in blocks if b[0] <= regs["eip"] < b[1]]
        memscan.save(_state_dir(), "last-start", {"kind": "protected", "path": opened, "entry": regs["eip"],
                                                  "blocks": blocks, "tsc": _stub_tsc(c)})
        lines = ["32-bit code of %s started: stopped at its first instruction (linear %08X)." % (opened, regs["eip"]),
                 "DPMI memory allocated after the extender opened it: " +
                 (", ".join("%08X-%08X%s" % (a, b, " (code entered here)" if (a, b) in inside else "") for a, b in blocks)
                  or "none") + ("" if inside else " (the code runs outside them: the extender placed it itself)"),
                 "With flat-model extenders (DOS/4GW, DOS/32A, PMODE/W, CauseWay) linear = offset (segment bases 0); "
                 "load_symbols places the program's objects (code, data) from here."]
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
                # The program's output: from its start on (not the shell echoing the command).
                text = watchdog.output_text([x for x in ordered if not execs or x.seq > execs[0].seq])
                bad = watchdog.crash_lines(text)
                exits = [x for x in watchdog.own_calls(ordered, shell_ranges)
                         if x.vector == 0x21 and x.r("ah") in (0x4C, 0x31) and
                         (not execs or x.seq > execs[0].seq)]
                if prog_psp is not None and psp == shell_psp and exits and "exit" in stops:
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
        for x, n in watchdog.collapse(failed)[-8:]:
            what, result = dosinfo.describe(x)
            lines.append("  #%d INT %02Xh %s -> %s  (from %s)%s" % (x.seq, x.vector, what, result, x.caller(),
                                                                 " (x%d)" % n if n > 1 else ""))
    execs = [x for x in ordered if x.vector == 0x21 and x.r("ah") == 0x4B and x.r("al") == 0]
    text = watchdog.output_text([x for x in ordered if not execs or x.seq > execs[0].seq]).rstrip()
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
    for k, v in re.findall(r"(\w+)\s*=\s*(?:0x)?([0-9a-fA-F]+)h?", text):
        bases["load" if k.lower() in ("load", "seg", "segment", "psp") else int(k, 10)] = int(v, 16)
    if not bases:
        raise ValueError("bases look like \"load=240E\" (16-bit) or \"1=174000,2=1FF000\" (objects)")
    return bases


@tool
def load_symbols(map_path: str, bases: str = "auto") -> str:
    """Load an Open Watcom linker map (.map) so that addresses can be given as
    names (set_breakpoint address="TS_ServiceSchedule_", read_memory
    address="_HeadTask") and are shown as name+offset in disassembly, the CPU
    state and stack dumps. map_path is a file on the machine running the bridge;
    map_path="none" unloads the symbols. The map is checked against the code in
    memory (near calls should land on its functions) and refused if it doesn't fit.

    bases: where the program is loaded. "auto" works it out: for a 16-bit
    program from the last wait_for_program_start or DOS's running program
    (load segment = PSP+10h); for a 32-bit DOS/4GW-style program from the last
    wait_for_program_start(protected_mode=true) (entry point and DPMI blocks).
    Or give them: "load=240E" (16-bit) or "1=174000,2=1FF000" (object bases)."""
    d = _state_dir()
    if map_path.strip().lower() in ("", "none", "off"):
        try:
            os.remove(os.path.join(d, "symbols.pkl"))
        except OSError:
            pass
        _symcache.update(key=None, table=None)
        dos.namer = None
        return "Symbols unloaded."
    m = symbols.MapFile(map_path)
    other_program = None
    if bases.strip().lower() not in ("", "auto"):
        b = _parse_bases(bases)
        how = "as given"
    else:
        start = _last_start(client())
        image = os.path.splitext(os.path.basename(m.image or map_path))[0].upper()
        started = os.path.splitext(start.get("path", "").replace("/", "\\").rsplit("\\", 1)[-1])[0].upper()
        if m.wide:
            if start.get("kind") != "protected":
                return ("Can't place a 32-bit map automatically: run wait_for_program_start(name, "
                        "protected_mode=true) first, or give bases like \"1=174000,2=1FF000\".")
            b = symbols.guess_object_bases(m, start["entry"], start["blocks"])
            how = "from the entry point and DPMI blocks of %s" % start["path"]
            # The other objects (data): where the code's absolute addresses
            # say they are, which beats matching block sizes (some extenders
            # put headers in front of an object, or place objects themselves).
            eobj = m.entry[0]
            esize = m.object_sizes().get(eobj, 0)
            if esize:
                code = _region_bytes(b[eobj], b[eobj] + min(esize, 0x400000))
                for obj in sorted(m.object_sizes()):
                    if obj in (eobj, 0):
                        continue
                    found = symbols.infer_object_base(m, obj, code)
                    if found and found[1] >= max(10, found[2] * 0.3):
                        if b.get(obj) != found[0]:
                            how += "; object %d placed by the code's references to it (%d agree)" % (obj, found[1])
                        b[obj] = found[0]
            if image and started and image != started:
                how += " (NOTE: the map is for %s)" % image
                other_program = (image, started)
            missing = sorted(set(m.object_sizes()) - set(b))
            if missing:
                how += "; no block found for object(s) %s" % ", ".join(map(str, missing))
        else:
            if start.get("kind") == "real" and (not image or image == started):
                b, how = {"load": start["load"]}, "from the start of %s" % start["path"]
                if m.com:
                    b["load"] = start["psp"] + 0x10  # SymbolTable counts .COM offsets from load-10h = PSP
            else:
                regs = _regs()
                mem, chain, top, progs = _dos_state(c := client())
                p = next((p for p in progs.values() if p.name.upper() == image), None)
                if p is None:
                    p, _ = _current_program(regs, progs, mem, chain)
                if p is None:
                    return "Can't find the program in DOS memory; give bases like \"load=240E\"."
                b, how = {"load": p.load_segment}, "from %s in DOS memory (PSP %04X)" % (p.name, p.psp)
    table = symbols.SymbolTable(m, b)  # validates the bases before anything is saved
    total, hits = table.check_code(_region_bytes)
    if total >= 20 and hits < total * 0.3:  # right map: 70%+ (static functions aren't in maps); wrong: ~1%
        return ("NOT loaded: this map doesn't fit the code in memory with %s: only %d of %d call "
                "targets found in the code are functions in the map (a different build of the program, "
                "or wrong bases?). Check the map is from the same build, or give other bases." % (
                    table.describe_bases(), hits, total)) + (
                    "\nThe symbols loaded before (%s) are still in use; map_path=\"none\" unloads them."
                    % _symcache["table"].map.path if _symbols() is not None else "")
    if other_program and total < 20:
        return ("NOT loaded: the map is for %s but the last program started was %s, and there were too few "
                "calls in memory to check that it fits. Start the right program with wait_for_program_start, "
                "or give bases." % other_program)
    fit = ("; %d of %d call targets in memory are functions in the map" % (hits, total) if total >= 20
           else "; could not check it against the code in memory (too few calls found)")
    memscan.save(d, "symbols", {"path": os.path.abspath(map_path), "bases": b})
    _symcache["key"] = None
    table = _symbols()
    dos.namer = lambda lin, dist: table.name_at(lin, dist)
    return "Loaded %d symbols from %s; %s (%s)%s." % (len(table.by_addr), map_path, table.describe_bases(),
                                                      how, fit)


def _watcom_libraries(spec, bits):
    """Library/object files from "path;path" (files or directories), or Open
    Watcom's DOS libraries ($WATCOM, else found from wcl386 on the PATH)."""
    paths = []
    parts = [p.strip() for p in re.split(r"[;\n]", spec) if p.strip()]
    if parts and any(p.lower() == "watcom" for p in parts):
        parts = [p for p in parts if p.lower() != "watcom"]
        paths = _watcom_libraries("", bits)
    for part in parts:
        if os.path.isdir(part):
            paths += [os.path.join(part, n) for n in sorted(os.listdir(part)) if n.lower().endswith((".lib", ".obj"))]
        elif os.path.isfile(part):
            paths.append(part)
        else:
            raise ValueError("no such library file or directory: %s" % part)
    if spec.strip():
        return paths
    if not os.environ.get("WATCOM"):
        import shutil
        exe = shutil.which("wcl386") or shutil.which("wcl")
        if exe:  # $WATCOM/binl64/wcl386 -> $WATCOM
            os.environ["WATCOM"] = os.path.dirname(os.path.dirname(os.path.realpath(exe)))
    return libsigs.default_libraries(bits)


def _program_code(c, regs):
    """[(lo, hi, frame)], bits, description: the code of the program started
    last (32-bit: its extender block holding the entry point; 16-bit: its DOS
    memory, one 64 KiB frame per code segment)."""
    start = _last_start(c)
    if start.get("kind") == "protected":
        entry = start["entry"]
        block = next(((a, b) for a, b in start["blocks"] if a <= entry < b), None)
        if block is None and entry < 0xA0000:  # placed in DOS memory (PMODE/W): that DOS block
            mem, chain, top, progs = _dos_state(c)
            block = next(((b.start * 16, b.end * 16) for p in progs.values() for b in p.blocks
                          if b.start * 16 <= entry < b.end * 16), None)
        if block is None:
            lo, hi, _ = _code_region(regs, entry)
            block = (lo, hi)
        return [(block[0], block[1], 0)], 32, "%s, 32-bit code in %08X-%08X" % (start["path"], block[0], block[1])
    if start.get("kind") == "real":
        load, cs, name = start["load"], start.get("cs", start["load"]), start["path"]
        psp = start["psp"]
    else:
        mem, chain, top, progs = _dos_state(c)
        p, _ = _current_program(regs, progs, mem, chain)
        if p is None:
            raise ValueError("no program to look at: start one with wait_for_program_start")
        load, cs, name, psp = p.load_segment, p.load_segment, p.name or "the running program", p.psp
    mem, chain, top, progs = _dos_state(c)
    p = progs.get(psp)
    end = max((b.end for b in p.blocks if b.kind != "environment"), default=load + 0x1000) if p else load + 0x1000
    lo, hi = load * 16, min(end * 16, 0xA0000)
    # Code segments: the entry point's, and those far calls go to (medium/large models).
    data = _region_bytes(lo, hi)
    counts = collections.Counter()
    for m in re.finditer(b"\x9A", data):  # far calls: 9A offset segment
        if m.start() + 5 <= len(data):
            counts[struct.unpack_from("<H", data, m.start() + 3)[0]] += 1
    # Segments called more than once (a lone match is likely other bytes).
    segs = sorted({cs} | {sg for sg, n in counts.items() if n >= 2 and lo <= sg * 16 < hi})
    frames = [(sg * 16, min(sg * 16 + 0x10000, hi), sg * 16) for sg in segs]
    return frames, 16, "%s, 16-bit code segment(s) %s" % (name, ", ".join("%04X" % sg for sg in segs))


def _write_identified_map(path, image, bits, frames, found, data_syms, blocks):
    """A map file in Open Watcom's format with the identified names, which
    load_symbols' machinery then reads like any map."""
    lines = ["Executable Image: %s" % image, "", "|   Segments   |", "",
             "Segment                Class          Group          Address         Size"]
    if bits == 32:
        for lo, hi, _ in frames:
            lines.append("_TEXT                  CODE           AUTO           0001:%08x       %08x" % (lo, hi - lo))
        for lo, hi in blocks:
            lines.append("DATA                   DATA           AUTO           0001:%08x       %08x" % (lo, hi - lo))
    else:
        for lo, hi, frame in frames:
            lines.append("_TEXT                  CODE           AUTO           %04x:0000       %08x" % (frame >> 4, hi - lo))
    lines += ["", "|   Memory Map   |", "", "Module: identified"]
    # Aliases first: of the names at one address, the last one is shown.
    names = {a: [n] for a, n in data_syms.items()}
    names.update((a, f.get("alts", [])[::-1] + [f["name"]]) for a, f in found.items())
    for a in sorted(names):
        for n in names[a]:
            if bits == 32:
                lines.append("0001:%08x  %s" % (a, n))
            else:
                frame = next((fr for lo, hi, fr in frames if lo <= a < hi), None)
                if frame is not None:
                    lines.append("%04x:%04x  %s" % (frame >> 4, a - frame, n))
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


@tool
def identify_functions(libraries: str = "", max_listed: int = 80) -> str:
    """Name library functions (the C runtime's printf_, inp_, outp_, strtoul_,
    malloc_, and any other library's) in the program started last, without
    its linker map: their code is matched against signatures read from OMF
    libraries (bytes the linker fills in are ignored). Functions found also
    name the functions they call, and in 32-bit programs the runtime's
    variables they use (__psp, ___iob...). Every other called function is
    named sub_ADDRESS. The names then work like a map's: as addresses, and in
    disassembly, stacks and logs (load_symbols map_path=none removes them).

    libraries: library or object files, or directories of them, separated by
    ";"; the word watcom adds Open Watcom's DOS libraries, e.g.
    "watcom;/sdk/awe32/pawe32.lib" for a game also linked with a sound SDK.
    Default: Open Watcom's DOS libraries ($WATCOM, or found from wcl386 on
    the PATH). Use the libraries of the compiler version that built the
    program: another version's code differs, and fewer functions are found.
    Run it after wait_for_program_start (protected_mode=true for 32-bit
    programs)."""
    c = client()
    regs = _regs()
    if _symbols() is not None and not memscan.load(_state_dir(), "symbols").get("identified"):
        return ("A linker map is loaded (%s); its names are better than identified ones. "
                "load_symbols map_path=none unloads it." % _symcache["table"].map.path)
    try:
        frames, bits, what = _program_code(c, regs)
        paths = _watcom_libraries(libraries, bits)
    except ValueError as e:
        return str(e)
    if not paths:
        return ("No libraries to read: set WATCOM to Open Watcom's directory (or put wcl386 on the PATH), "
                "or give libraries=\"path/to/clib3r.lib;...\".")
    sigs = libsigs.load(paths, _state_dir())
    if not sigs.by_bits[bits]:
        return "No %d-bit functions in %s." % (bits, ", ".join(paths[:5]))
    found, data_syms = {}, {}
    called = set()
    for lo, hi, frame in frames:
        base = lo if bits == 32 else frame
        data = _region_bytes(base, hi)
        targets = codeanalysis.call_targets(data, base, bits, lo, hi)
        f, d = libsigs.identify(data, base, bits, sigs, targets, frame)
        # Memory holding more than the program's code (PMODE/W keeps the
        # program file it loaded in the same DOS block): keep the code that
        # the entry point's calls reach.
        entry = _last_start(c).get("entry")
        span = libsigs.reachable_span(data, base, bits, set(f) | set(targets), entry) \
            if bits == 32 and entry is not None and lo <= entry < hi else None
        inside = {x["name"] for a, x in f.items() if span and span[0] <= a < span[1]}
        if span and any(x["name"] in inside for a, x in f.items() if not span[0] <= a < span[1]):
            f = {a: x for a, x in f.items() if span[0] <= a < span[1]}
            targets = {a: n for a, n in targets.items() if span[0] <= a < span[1]}
            frames = [(span[0], span[1], frame) if (fl, fh) == (lo, hi) else (fl, fh, ff) for fl, fh, ff in frames]
            what = "%s, 32-bit code in %08X-%08X (the code reached from the entry point in %08X-%08X)" % (
                what.split(",")[0], span[0], span[1], lo, hi)
        called |= set(targets)
        found.update(f)
        data_syms.update(d)
    if not found:
        return ("No library function found in %s with %d signatures from %d file(s). Is it built with "
                "these libraries (another compiler or version)?" % (what, len(sigs), len(sigs.sources)))
    # Names for the rest: called functions, and code right after an identified
    # function (functions are laid out back to back).
    sub = {}
    for t in called:
        if t not in found:
            sub[t] = "sub_%08X" % t if bits == 32 else "sub_%05X" % t
    starts = set(found) | set(sub)
    for a, f in list(found.items()):
        end = a + f["length"]
        if f["sig"].get("code") and end not in starts and any(lo <= end < hi for lo, hi, _ in frames):
            sub[end] = "sub_%08X" % end if bits == 32 else "sub_%05X" % end
    names = {a: {"name": n} for a, n in sub.items()}
    names.update(found)
    blocks = []
    if bits == 32:
        blocks = [b for b in _last_start(c).get("blocks", []) if any(b[0] <= a < b[1] for a in data_syms)
                  and not any(b[0] == lo for lo, _, _ in frames)]
        data_syms = {a: n for a, n in data_syms.items() if any(lo <= a < hi for lo, hi in blocks)}
    d = _state_dir()
    path = os.path.join(d, "identified.map")
    _write_identified_map(path, what.split(",")[0], bits, frames, names, data_syms, blocks)
    memscan.save(d, "symbols", {"path": path, "bases": {1: 0} if bits == 32 else {"load": 0}, "identified": True,
                                "start_tsc": (memscan.load(d, "last-start") or {}).get("tsc")})
    _symcache["key"] = None
    table = _symbols()
    dos.namer = lambda lin, dist: table.name_at(lin, dist)
    by_sig = sum(1 for f in found.values() if f["how"] == "signature" or f["how"].startswith("next to"))
    lines = ["%s: %d library function(s) identified (%d by signature, %d through calls and addresses in them), "
             "%d other function(s) named sub_ADDRESS%s. Signatures: %d from %d file(s)." % (
                 what, len(found), by_sig, len(found) - by_sig, len(sub),
                 ", %d runtime variable(s)" % len(data_syms) if data_syms else "", len(sigs), len(sigs.sources))]
    lib_called = sorted(a for a in found if a in called)
    lines.append("Called library functions (address, name, source module):")
    for a in lib_called[:max_listed]:
        f = found[a]
        mod = f["sig"].get("module", "")
        lines.append("  %08X  %s%s%s%s" % (a, f["name"], "  [%s]" % mod if mod else "",
                                            "  (or %s)" % ", ".join(f["alts"][:3]) if f["alts"] else "",
                                            "" if f["how"] == "signature" or f["how"].startswith("next to")
                                            else "  (%s)" % f["how"]))
    if len(lib_called) > max_listed:
        lines.append("  ... %d more (lookup_symbol \"*\" lists all names)" % (len(lib_called) - max_listed))
    others = len(found) - len(lib_called)
    if others:
        lines.append("%d more identified function(s) are only called indirectly or by the library itself." % others)
    lines.append("The names last until another program starts (wait_for_program_start), hard_reset or "
                 "load_symbols map_path=none; after this program exits its memory may hold other code.")
    if data_syms:
        lines.append("Runtime variables: " + ", ".join("%s=%08X" % (n, a) for a, n in sorted(data_syms.items())[:12]) +
                     (" ..." if len(data_syms) > 12 else ""))
    return "\n".join(lines)


@tool
def lookup_symbol(query: str, limit: int = 30) -> str:
    """Find symbols: a name ("HeadTask", "_HeadTask+8"), a pattern with * ("TS_*",
    "*score*"), or an address (shows the symbol it falls in)."""
    table = _symbols()
    if table is None:
        return "No symbols loaded; use load_symbols (a linker map) or identify_functions (library functions, no map needed)."
    q = query.strip()
    if "*" in q or "?" in q:
        rx = re.compile("^" + re.escape(q).replace(r"\*", ".*").replace(r"\?", ".") + "$", re.I)
        hits = [(a, n) for a, n in table.by_addr if rx.match(n)]
        lines = ["%08X  %s" % h for h in hits[:limit]]
        if len(hits) > limit:
            lines.append("... %d more" % (len(hits) - limit))
        return "\n".join(lines) or "No match."
    try:
        lin = table.lookup(q)
    except ValueError as e:
        return str(e)
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
    start = _last_start(client())
    for i, (lo, hi) in enumerate(start.get("blocks", [])):
        if lo <= lin < hi:
            out.append("DPMI block #%d %08X-%08X allocated after %s was opened, offset %X" % (
                i + 1, lo, hi, start.get("path", "the program"), lin - lo))
    in_blocks = any(lo <= lin < hi for lo, hi in start.get("blocks", []))
    if table is not None and table.name_at(lin, 0x10000) is None and lin >= 0x110000 and not in_blocks:
        out.append("outside the program's code and data: memory it allocated later (heap), or another "
                   "program's")
    # Guess a function only where code can be: not when the symbols already
    # say, and not in memory outside the program (heap objects aren't code).
    if table is None and (in_blocks or lin < 0x110000):
        try:
            lo, hi, bits = _code_region(regs, lin)
            if lo <= lin < hi and hi - lo <= 0x200000:
                f = codeanalysis.function_start(codeanalysis.call_targets(_region_bytes(lo, hi), lo, bits), lin)
                if f is not None:
                    out.append("if this is code: probably in the function starting at %08X (+%X), a call "
                               "target" % (f, lin - f))
        except (GdbError, ValueError):
            pass
    if len(out) == 1:
        out.append("nothing known about it (heap, stack, unmapped or another program's memory)")
    return "\n".join(out)



# ---- code analysis and patching -------------------------------------------------------

def _linear_code_bits(regs, linear):
    """Code size for a bare linear address: the program's (from its extender
    blocks or loaded symbols) if it is in there; 32 above the real-mode 1 MiB
    (+HMA) while the CPU happens to be in real or V86 mode (the program was
    interrupted by DOS or the extender's real-mode code); else the current size."""
    start = _last_start(client())
    if any(lo <= linear < hi for lo, hi in start.get("blocks", [])):
        return 32
    table = _symcache["table"] if dos.namer else None
    if table is not None and any(lo <= linear < hi for lo, hi in table.code_ranges):
        return 32 if table.map.wide else 16
    if dos.segmented(regs) and linear >= 0x110000:
        return 32
    return dos.code_bits(regs)


def _code_region(regs, linear):
    """(lo, hi, bits) of the code to analyse around `linear`: the extender's
    memory block holding it (from wait_for_program_start), the DOS program
    block, or the 64 KiB code segment in real mode; else 1 MiB around it."""
    start = _last_start(client())
    for lo, hi in start.get("blocks", []):
        if lo <= linear < hi:
            return lo, hi, 32
    table = _symcache["table"] if dos.namer else None
    if table is not None:
        for lo, hi in table.code_ranges:
            if lo <= linear < hi:
                return lo, hi, 32 if table.map.wide else 16
    if dos.segmented(regs) and linear < 0x110000:
        cs = dos.seg_cache(regs, "cs")["base"]
        if cs <= linear < cs + 0x10000:
            return cs, cs + 0x10000, 16
        seg = linear & ~0xF
        return seg, seg + 0x10000, 16
    return max(0, linear - 0x80000), linear + 0x80000, _linear_code_bits(regs, linear)


def _region_bytes(lo, hi):
    snap = memscan.take(client(), [(lo, hi)])
    data = bytearray(hi - lo)
    for a, d in snap.runs:
        data[a - lo:a - lo + len(d)] = d
    return bytes(data)


@tool
def find_references(target: str, code: str = "", max_results: int = 50) -> str:
    """Find the instructions that refer to an address: 32-bit absolute operands
    (e.g. mov eax,[target], push offset target), in 16-bit code [ofs] operands
    and immediates equal to its offset (mov ax,ofs / push ofs: near pointers),
    and near call/jmp/jcc to it.
    Use it to find every place that uses a variable, or every caller of a
    function. code: the address of some code in the program to search (default:
    the code around target if it is code, else the code at CS:EIP); the search
    covers that code's memory block or segment (by default the code at CS:EIP,
    so pause in the program first)."""
    regs = _regs()
    tloc = _resolve(target, regs)
    where = _resolve(code, regs).linear if code else regs["eip"]
    lo, hi, bits = _code_region(regs, where)
    data = _region_bytes(lo, hi)
    off16 = None
    if bits == 16:
        # 16-bit code refers to data by its offset in the segment: the one
        # given (SEG:OFF), else DS's.
        seg_base = tloc.base if tloc.base is not None else dos.seg_cache(regs, "ds")["base"]
        if 0 <= tloc.linear - seg_base <= 0xFFFF:
            off16 = tloc.linear - seg_base
    refs = codeanalysis.references(data, lo, bits, tloc.linear, max_results, off16)
    head = "References to %s (linear %08X%s) in %08X-%08X (%d-bit code):" % (
        target, tloc.linear, ", offset %04X" % off16 if off16 is not None else "", lo, hi, bits)
    if off16 is not None and refs:
        head += ("\n(\"offset value\" = an immediate equal to the offset: usually a near pointer being "
                 "passed, e.g. mov ax,ofs / push ofs, but it can be a constant that happens to match)")
    if not refs:
        return head + "\nNone found (data reached through registers or tables isn't visible to this search)."
    targets = codeanalysis.call_targets(data, lo, bits)
    lines = [head]
    for a, kind, text in refs:
        fn = dos.namer(a, 0x10000) if dos.namer else None
        if not fn:
            f = codeanalysis.function_start(targets, a)
            fn = "function at %08X+%X" % (f, a - f) if f is not None else ""
        lines.append("  %08X  %-12s %-36s %s" % (a, kind, text, fn))
    return "\n".join(lines)


_HIT_FMT = struct.Struct("<4I8II6H4III")  # one logpoint record of the stub's "ll" output
_HIT_IRQ_FMT = struct.Struct("<4I")  # ...followed in newer builds by the last IRQ: info, linear, CS, age
_HIT_REGS = ("eax", "ebx", "ecx", "edx", "esi", "edi", "ebp", "esp")


def _parse_hits(raw, size):
    out = []
    for k in range(0, len(raw) - size + 1, size):
        f = _HIT_FMT.unpack_from(raw, k)
        r = dict(zip(_HIT_REGS, f[4:12]))
        r["eflags"] = f[12]
        r.update(zip(dos.SEGS, f[13:19]))
        out.append({"seq": f[0], "addr": f[1], "tsc": f[2] | (f[3] << 32), "regs": r,
                    "stack": f[19:23] if f[24] & 1 else None, "mem": f[23] if f[24] & 2 else None,
                    "mode": (f[24] >> 8) & 3, "irq": None})
        if size >= _HIT_FMT.size + _HIT_IRQ_FMT.size:  # newer builds: the last hardware interrupt
            info, lin, sel, age = _HIT_IRQ_FMT.unpack_from(raw, k + _HIT_FMT.size)
            if info & 0x10000:
                out[-1]["irq"] = {"vector": info & 0xFF, "mode": (info >> 8) & 3, "linear": lin, "cs": sel,
                                  "age": age}
    return out


def _interrupted(hits, hz):
    """When the hits look like an interrupt handler's (nearly all come just after
    a hardware interrupt with the same vector), (vector, [interrupted linear
    address or None per hit]); else None."""
    near = max(1, hz // 2000)  # within 0.5 ms of emulated time
    recent = [h["irq"] if h["irq"] and h["irq"]["age"] < near else None for h in hits]
    vectors = collections.Counter(i["vector"] for i in recent if i)
    if not vectors or len(hits) < 2:
        return None
    vector, n = vectors.most_common(1)[0]
    if n * 5 < len(hits) * 4:
        return None
    return vector, [i["linear"] if i and i["vector"] == vector else None for i in recent]


def _logpoint_memory(spec, regs):
    """(reg, disp) for the stub: "esi+2C", "ds:si", "bx-4" (DS-relative),
    "ebp-8", "ss:esi+4" (SS-relative: reg | 10h), or a linear address / symbol
    (reg 8)."""
    t = spec.strip().lower().replace(" ", "")
    seg = None
    if t[:3] in ("ds:", "ss:"):
        seg, t = t[:2], t[3:]
    m = re.fullmatch(r"\[?(e?[abcd]x|e?[sd]i|e?[bs]p)(?:([+-])(?:0x)?([0-9a-f]+)h?)?\]?", t)
    if m:
        reg = m.group(1) if m.group(1).startswith("e") else dos.REG16[m.group(1)]
        disp = int(m.group(3), 16) if m.group(3) else 0
        # As the CPU does: [ebp+x] and [esp+x] are in SS, the rest in DS.
        if seg is None:
            seg = "ss" if reg in ("ebp", "esp") else "ds"
        return _HIT_REGS.index(reg) | (0x10 if seg == "ss" else 0), (-disp if m.group(2) == "-" else disp) & 0xFFFFFFFF
    if re.match(r"[a-z]s:", t):
        raise ValueError("memory= can be relative to DS or SS (e.g. \"esi+2C\", \"ss:ebp-8\"), or an address")
    return 8, _resolve(spec, regs).linear


def _looks_like_return(c, ret_lin, target, bits):
    """Whether ret_lin follows a call (direct to target, any direct call if
    target is None, or indirect)."""
    if not 7 <= ret_lin < 0xFFFFFFF0:
        return False
    try:
        b = c.read_memory(ret_lin - 7, 7)
    except GdbError:
        return False
    if len(b) < 7:
        return False
    n = 5 if bits == 32 else 3
    if b[7 - n] == 0xE8:
        rel = int.from_bytes(b[8 - n:], "little", signed=True)
        if target is None:
            return True
        if bits == 32:
            return (ret_lin + rel) & 0xFFFFFFFF == target
        return (ret_lin + rel - target) & 0xFFFF == 0  # same code segment: offsets agree mod 64K
    # Indirect: FF /2 (call r/m) with a 0-, 1-, 4- or 2-byte operand after the ModRM byte.
    return any(b[7 - k] == 0xFF and (b[8 - k] >> 3) & 7 == 2 for k in (2, 3, 6, 4))


def _call_before(c, ret_lin, w):
    """Linear address of the call instruction that ret_lin returns after."""
    b = c.read_memory(ret_lin - 7, 7)
    n = 5 if w == 4 else 3
    if len(b) == 7 and b[7 - n] == 0xE8:
        return ret_lin - n
    for k in (2, 3, 4, 6):
        if len(b) == 7 and b[7 - k] == 0xFF and (b[8 - k] >> 3) & 7 == 2:
            return ret_lin - k
    return ret_lin


def _looks_like_far_return(c, ret_lin):
    """Whether ret_lin follows a far call (9A ptr16:16, or FF /3)."""
    try:
        b = c.read_memory(ret_lin - 5, 5)
    except GdbError:
        return False
    return len(b) == 5 and (b[0] == 0x9A or any(b[5 - k] == 0xFF and (b[6 - k] >> 3) & 7 == 3 for k in (2, 3, 4)))


@tool
def log_hits(address: str, seconds: float = 10.0, max_hits: int = 200, registers: str = "eax ebx ecx edx",
             memory: str = "") -> str:
    """Record each time execution reaches `address`, without stopping: the
    emulator logs every hit itself (the chosen registers, the return address
    when `address` is a function entry, and optionally the dword at `memory`:
    an address or symbol, or relative to DS like "esi+2C" or "ds:si") while
    the program runs at full speed, for `seconds` of real time or until
    `max_hits`, or until the program exits (it then stays stopped at its exit
    call). Shows the first and last hits and the values seen. Times are
    emulated seconds since the first hit. For an interrupt handler it also
    shows where each interrupt came in: the program code it interrupted.
    log_hits on a function entry also shows its callers."""
    c = client()
    regs = _regs()
    loc = _resolve(address, regs)
    names = [r.lower() for r in registers.replace(",", " ").split()]
    mem = None
    if memory:
        try:
            mem = _logpoint_memory(memory, regs)
        except ValueError as e:
            return "memory=%s: %s" % (memory, e)
    st = c.logpoints("%x" % loc.linear + (" %x %x" % mem if mem else ""))
    start = st["next"]
    t_end = time.time() + max(0.1, seconds)
    was_running = c.running
    # Stop with the program: catch its exit (INT 21h AH=4Ch) while logging.
    saved = c.int_status()
    exit_catch = not any(v == 0x21 and ah in (None, 0x4C) for v, ah, _, _ in saved["catches"])
    if exit_catch:
        c.catch_int(0x21, 0x4C, None, "call")
    exited = False
    try:
        if not c.running:
            c.resume()
        while time.time() < t_end:
            time.sleep(min(0.25, max(0.0, t_end - time.time())))
            st = c.logpoints()
            if st["next"] - start >= max_hits:
                break
            if not c.running:  # stopped: the program's exit, or a breakpoint or watchpoint
                break
        stopped = None if c.running else c.last_stop
        if stopped is not None and stopped.reason == "catch" and getattr(stopped, "int_vector", None) == 0x21 \
                and exit_catch:
            exited, stopped = True, None
        if c.running and not was_running:
            c.pause()
    finally:
        st = c.logpoints("- %x" % loc.linear)
        if exit_catch:
            _restore_catches(c, saved)
    total = next((h for a, _, _, h in st["points"] if a == loc.linear), None)
    hits = [h for h in _parse_hits(c.hit_log(start), st["record"]) if h["addr"] == loc.linear][:max_hits]
    lost = max(0, st["first"] - start)
    hz = st.get("hz") or 1
    head = "%d hit(s) of %s (linear %08X%s) in %.1fs of real time" % (
        len(hits), address, loc.linear, _sym(loc.linear), seconds - max(0.0, t_end - time.time()))
    if hits:
        head += ", %.2f emulated seconds" % ((hits[-1]["tsc"] - hits[0]["tsc"]) / hz)
    head += " (max_hits reached)." if len(hits) >= max_hits else "."
    lines = [head]
    if lost:
        lines.append("(the oldest %d hit(s) were overwritten in the stub's buffer)" % lost)
    # Return addresses, when the address is a function entry (the stack top
    # then holds a return address just after a call).
    rets = []
    for h in hits:
        if h["stack"] is None:
            rets.append(None)
            continue
        r = h["regs"]
        if h["mode"] == 3 or (h["mode"] == 2 and dos.stack_bits(regs) == 32):
            rets.append(h["stack"][0])
        else:
            ip = h["stack"][0] & 0xFFFF
            cs_base = r["cs"] * 16 if h["mode"] < 2 else dos.seg_cache(regs, "cs")["base"]
            rets.append(cs_base + ip)
    sample = [(a, h) for a, h in zip(rets, hits) if a is not None][:20]
    bits = _linear_code_bits(regs, loc.linear)
    entry = bool(sample) and sum(_looks_like_return(c, a, loc.linear, bits) for a, _ in sample) * 2 > len(sample)
    if entry:
        callers = {}
        for a in rets:
            if a is not None:
                callers[a] = callers.get(a, 0) + 1
        lines.append("Callers (return addresses on the stack):")
        for a, n in sorted(callers.items(), key=lambda kv: -kv[1])[:10]:
            lines.append("  %08X%s  x%d" % (a, _sym(a), n))
    # In an interrupt handler: what each interrupt interrupted (where the
    # program was), e.g. to find the code a race with the handler hits.
    irq = _interrupted(hits, hz)
    if irq:
        where = collections.Counter(a for a in irq[1] if a is not None)
        lines.append("This is an interrupt handler: each hit follows an INT %02Xh hardware interrupt. "
                     "It interrupted (top %d of %d addresses):" % (irq[0], min(12, len(where)), len(where)))
        for a, n in where.most_common(12):
            lines.append("  %08X%s  x%d" % (a, _sym(a), n))
    t0 = hits[0]["tsc"] if hits else 0
    out = []
    for i, (h, a) in enumerate(zip(hits, rets)):
        vals = []
        for n in names:
            try:
                width = 2 if n in dos.REG8 else 4 if n in dos.SEGS or n in dos.REG16 else 8
                vals.append("%s=%0*X" % (n.upper(), width, dos.reg_value(n, h["regs"])))
            except (KeyError, ValueError):
                vals.append("%s=?" % n)
        if mem:
            vals.append("[%s]=%s" % (memory, "%08X" % h["mem"] if h["mem"] is not None else "?"))
        out.append("%.4fs  %s%s%s" % ((h["tsc"] - t0) / hz, " ".join(vals),
                                      ("  ret %08X%s" % (a, _sym(a))) if entry and a is not None else "",
                                      ("  interrupted %08X%s" % (irq[1][i], _sym(irq[1][i])))
                                      if irq and irq[1][i] is not None else ""))
    # The values seen, per register / memory dword: few distinct ones are
    # listed with counts (a counter, a flag, a handful of callers...).
    fields = [n.upper() for n in names] + (["[%s]" % memory] if mem else [])
    for f in fields:
        vals = collections.Counter()
        for h in hits:
            if f.startswith("["):
                vals[h["mem"]] += 1
            else:
                try:
                    vals[dos.reg_value(f.lower(), h["regs"])] += 1
                except (KeyError, ValueError):
                    pass
        if not vals or len(hits) < 2:
            continue
        if len(vals) <= 8:
            lines.append("%s: %s" % (f, ", ".join("%s x%d" % ("?" if v is None else "%X" % v, n)
                                                  for v, n in vals.most_common())))
        else:
            known = [v for v in vals if v is not None]
            lines.append("%s: %d different values, %X..%X" % (f, len(vals), min(known), max(known)) if known
                         else "%s: unreadable" % f)
    # The first and the last hits: how it starts and how it ends.
    if len(out) > 60:
        lines += out[:20] + ["... %d hits not shown ..." % (len(out) - 60)] + out[-40:]
    else:
        lines += out
    if total is not None and total > len(hits) + lost:
        lines.append("(%d hits in all while the logpoint was set)" % total)
    if exited:
        lines += ["", "The program exited (INT 21h AH=4Ch); stopped at its exit call, so its memory can "
                  "still be read. resume lets DOS finish."]
    if stopped is not None:
        lines += ["", "Stopped for another reason:", _state_report(stopped)]
    return "\n".join(lines)


@tool
def patch_code(address: str, action: str, target: str = "", length: int = 0) -> str:
    """Patch code in memory: action "jmp" or "call" writes a branch to `target`
    (offsets computed for you; a short jmp when it fits), "nop" fills `length`
    bytes with NOPs, "ret" writes a RET, and "undo" puts back the bytes of the
    last patch at `address`. A patch that ends inside an instruction is padded
    with NOPs to its end. Shows the code before and after; patches are kept so
    they can be undone later (also across run_tool commands). Patch whole
    logical steps: skipping a push without its pop crashes the program. The
    CPU may keep running; the bytes are written between two instructions."""
    c = client()
    regs = _regs()
    loc = _resolve(address, regs)
    at = loc.linear
    bits = loc.bits or _linear_code_bits(regs, at)
    d = _state_dir()
    patches = memscan.load(d, "patches") or {}
    action = action.lower()
    if action == "undo":
        if at not in patches:
            return "No patch recorded at %08X (have: %s)." % (at, ", ".join("%08X" % a for a in patches) or "none")
        old = patches.pop(at)
        if isinstance(old, tuple):  # (original bytes, code size at patch time)
            old, bits = old
        c.write_memory(at, old)
        memscan.save(d, "patches", patches)
        return "Restored %d byte(s) at %08X.\n%s" % (len(old), at, dos.disassemble(c.read_memory(at, 16), at, bits, None, 3))
    if action in ("jmp", "call"):
        if not target:
            return "Give target= for %s." % action
        new = codeanalysis.encode_branch(action, at, _resolve(target, regs).linear, bits)
    elif action == "nop":
        if length < 1:
            return "Give length= (bytes) for nop."
        new = b"\x90" * length
    elif action == "ret":
        new = b"\xC3"
    else:
        return "action is jmp, call, nop, ret or undo."
    before_bytes = c.read_memory(at, max(32, len(new) + 16))
    # Keep instructions whole: pad up to the end of the last instruction the
    # patch touches with NOPs, so no half instruction is left to execute.
    end, note = codeanalysis.instruction_end(before_bytes, at, bits, len(new)), ""
    if end is not None and end > len(new):
        note = " (+%d NOP byte(s) to the end of the instruction it overwrote)" % (end - len(new))
        new += b"\x90" * (end - len(new))
    before = dos.disassemble(before_bytes, at, bits, None, 4)
    if at not in patches:
        patches[at] = (before_bytes[:len(new)], bits)
        memscan.save(d, "patches", patches)
    c.write_memory(at, new)
    after = dos.disassemble(c.read_memory(at, 16), at, bits, None, 3)
    warn = ""
    if action in ("nop", "jmp") and any(i.mnemonic.startswith(("push", "pop", "enter", "leave"))
                                         for i in codeanalysis.instructions(before_bytes[:len(new)], at, bits)):
        warn = ("\nWARNING: this removes stack instructions (push/pop/enter/leave); unless the code you skip "
                "to undoes exactly the same, the stack ends up unbalanced and the program will crash.")
    return "Patched %d byte(s) at %08X (%s)%s; undo with action=undo.\nBefore:\n%s\nAfter:\n%s%s" % (
        len(new), at, new.hex(" ").upper(), note, before, after, warn)


if __name__ == "__main__":
    mcp.run()
