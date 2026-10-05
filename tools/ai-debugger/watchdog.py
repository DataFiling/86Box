"""Unattended run of a DOS program: watch it for a while and decide what
happened (exited, crashed, hung, waiting for input, still running), with the
evidence an AI or a person needs to start debugging.

It works from what the stub provides: the INT call log (DOS, DPMI, BIOS,
mouse calls with results, text written through DOS and the BIOS), samples of
the CPU's position, the DOS memory map and the screen."""

import re
import time

import dos
import dosinfo

LOG_VECTORS = (0x10, 0x16, 0x21, 0x2F, 0x31, 0x33)

# Text that means the program crashed (CPU exception, runtime abort)...
CRASH_PATTERNS = [
    r"invalid opcode", r"general protection", r"page fault", r"exception", r"stack overflow",
    r"divide (by zero|overflow|error)", r"abnormal program termination", r"null pointer",
    r"illegal instruction", r"unhandled", r"stack fault", r"segment not present",
]
# ...and text that reports an error the program noticed itself.
ERROR_PATTERNS = [r"error", r"fatal", r"abort", r"run-?time error", r"not enough memory", r"out of memory",
                  r"can'?t (open|find|load|allocate)", r"cannot", r"not found", r"failed", r"corrupt",
                  r"bad fat", r"chkdsk", r"retry", r"general failure", r"sector not found", r"not ready"]


def blocks_legitimately(c):
    """Calls that may take long without anything being wrong."""
    v, ah = c.vector, c.r("ah")
    if v == 0x21:
        return ah in (0x01, 0x07, 0x08, 0x0A, 0x0C, 0x4B, 0x31) or (ah in (0x3F, 0x40) and c.r("bx") <= 2)
    if v == 0x16:
        return ah in (0x00, 0x10)
    if v == 0x15:
        return ah in (0x86, 0x4F, 0x90)
    if v == 0x2F:
        return c.r("ax") in (0x1680,)
    if v == 0x31:
        return c.r("ax") in (0x0300, 0x0301, 0x0302)  # real-mode calls: their own nested calls are checked
    return False


class Sample:
    """A CPU position sample; activity changes whenever an INT call is logged
    (a new record, or another repeat folded into the last one)."""

    def __init__(self, t, pc, cs_ip, seq, activity):
        self.t, self.pc, self.cs_ip, self.seq, self.activity = t, pc, cs_ip, seq, activity


def cs_ip_text(regs):
    off = dos.ip(regs)
    if off is None:
        return "linear %08X" % regs["eip"]
    return ("%04X:%08X" if dos.code_bits(regs) == 32 else "%04X:%04X") % (regs["cs"], off)


def output_text(calls):
    """Text the program wrote through the BIOS teletype and DOS console calls,
    in order (DOS's own console output goes through INT 10h AH=0Eh, so only
    that and direct BIOS writes are used, which avoids doubles)."""
    out = []
    for c in calls:
        if c.vector != 0x10:
            continue
        ah = c.r("ah")
        if ah == 0x0E or (ah in (0x09, 0x0A) and c.r("cx") == 1):
            ch = c.r("al")
            for _ in range(c.count):
                if ch == 0x08:  # backspace: DOS echoes BS, space, BS when a typed key is deleted
                    if out and out[-1] != "\n":
                        out.pop()
                elif ch not in (0x0D, 0x07):
                    out.append("\n" if ch == 0x0A else chr(ch) if 32 <= ch < 127 else "?")
    return "".join(out)


def matching_lines(text, patterns):
    return [line.strip().rstrip(".") for line in text.splitlines()
            if any(re.search(p, line.lower()) for p in patterns)]


def crash_lines(text):
    return matching_lines(text, CRASH_PATTERNS)


def error_lines(text):
    return matching_lines(text, ERROR_PATTERNS)


def own_calls(calls, shell_ranges):
    """Leave out the shell's own calls (made from its code), except the exec
    that started the program."""
    def from_shell(c):
        a = c.caller_linear()
        return any(lo <= a < hi for lo, hi in shell_ranges)
    return [c for c in calls if not from_shell(c) or (c.vector == 0x21 and c.r("ah") == 0x4B)]


def innermost_pending(calls):
    """The deepest call still in progress (what the CPU is inside), and the chain to it."""
    pend = [c for c in calls if c.status == dosinfo.PENDING]
    # A call still in progress only sees deeper calls after it; a later call at
    # its depth or shallower means its return was missed (stale record).
    later_min = {}
    low = 1 << 30
    for c in sorted(calls, key=lambda c: -c.seq):
        later_min[c.seq] = low
        low = min(low, c.depth)
    pend = [c for c in pend if later_min.get(c.seq, 1 << 30) > c.depth]
    if not pend:
        return None, []
    deepest = max(pend, key=lambda c: (c.depth, c.seq))
    return deepest, sorted(pend, key=lambda c: c.depth)


def drop_reflections(calls):
    """Leave out the real-mode copies of protected-mode calls: a DOS extender
    passes a protected-mode INT 21h etc. on to real-mode DOS, which logs the
    same call again one level deeper (its parent in the log)."""
    out, parent = [], {}  # depth -> the latest call at that depth
    for c in sorted(calls, key=lambda c: c.seq):
        p = parent.get(c.depth - 1)
        parent[c.depth] = c
        for d in [d for d in parent if d > c.depth]:
            del parent[d]
        if (p is not None and p.mode >= 2 and c.mode <= 1 and c.vector == p.vector and
                c.r("ah") == p.r("ah")):
            continue
        out.append(c)
    return out


def collapse(calls):
    """[(call, times)] with identical calls (same function, result and caller)
    folded into their last occurrence."""
    import collections
    groups = collections.OrderedDict()
    for c in calls:
        what, result = dosinfo.describe(c)
        k = (c.vector, what, result, c.caller_linear())
        n = groups.pop(k, (None, 0))[1]
        groups[k] = (c, n + c.count)
    return list(groups.values())


def failed_calls(calls):
    out = []
    for c in drop_reflections(calls):
        if not c.returned or not c.cf():
            continue
        if c.vector == 0x21 and dosinfo._dos_uses_cf(c.r("ah")) and c.r("ah") not in (0x4E, 0x4F, 0x71):
            out.append(c)
        elif c.vector == 0x31:
            out.append(c)
    return out


def summarize(calls, limit=12):
    """Key events: program starts, video modes, files, failures, exits."""
    ev, seen = [], {}
    for c in drop_reflections(calls):
        what, result = dosinfo.describe(c)
        key = c.vector == 0x21 and c.r("ah") in (0x4B, 0x4C, 0x31, 0x3D, 0x3C, 0x6C, 0x4E, 0x5B)
        key |= c.vector == 0x10 and c.r("ah") == 0x00 or (c.vector == 0x10 and c.r("ax") == 0x4F02)
        key |= c.vector == 0x31 and c.r("ax") in (0x0501, 0x0100)
        key |= c.returned and c.cf() and ((c.vector == 0x21 and dosinfo._dos_uses_cf(c.r("ah")) and
                                           c.r("ah") not in (0x4E, 0x4F, 0x71)) or c.vector == 0x31)
        if key:
            k = (c.vector, what, result, c.caller_linear())
            if k in seen:  # the same event again: count it on its first line
                seen[k][1] += c.count
                continue
            seen[k] = [len(ev), c.count]
            ev.append("#%d INT %02Xh %s -> %s  (from %s%s)" % (c.seq, c.vector, what, result, c.caller(),
                                                            "" if c.mode == 0 else " [%s]" % dosinfo.MODES[c.mode]))
    for i, n in seen.values():
        if n > 1:
            ev[i] += " (x%d)" % n
    if len(ev) > limit:
        ev = ev[:limit // 2] + ["... %d more ..." % (len(ev) - limit)] + ev[-(limit - limit // 2):]
    return ev


def progress_signature(client, ranges, sp_linear):
    """A hash of the program's memory (minus the 1 KiB below the stack pointer,
    which interrupt frames rewrite) and of the frame on screen: unchanged in a
    hang, changing in a game that only spends its time in a wait loop."""
    import hashlib
    import pcinput
    h = hashlib.sha1()
    for lo, hi in ranges:
        for a, d in client.peek(lo, hi - lo):
            if d:
                cut_lo, cut_hi = sp_linear - 1024 - a, sp_linear + 64 - a
                if cut_hi > 0 and cut_lo < len(d):
                    d = d[:max(0, cut_lo)] + d[min(len(d), cut_hi):]
                h.update(d)
    try:
        h.update(pcinput.grab_frame(client)[3])
    except Exception:  # no frame yet; memory alone still tells
        pass
    return h.hexdigest()


def classify(samples, calls, hang_seconds, sigs=(), now_tsc=0, hz=0):
    """-> (kind, detail) from the last hang_seconds of samples. sigs: [(time,
    progress signature)] taken during the run; now_tsc/hz date the calls."""
    if not samples:
        return "unknown", "no samples"
    last = samples[-1]
    window = [s for s in samples if s.t >= last.t - hang_seconds]
    if len(window) < 3 or window[-1].t - window[0].t < hang_seconds * 0.8:
        return "running", ""
    pcs = [s.pc for s in window]
    spread = max(pcs) - min(pcs)
    active = window[0].activity != window[-1].activity
    new_calls = [c for c in calls if c.seq >= window[0].seq - 1] if active else []
    meaningful = [c for c in new_calls if not dosinfo.is_polling(c)]
    inner, _ = innermost_pending(calls)
    sig_window = [sg for t, sg in sigs if t >= last.t - hang_seconds]
    _, chain = innermost_pending(calls)
    reading = [c for c in chain if (c.vector == 0x16 and c.r("ah") in (0x00, 0x10)) or
               (c.vector == 0x21 and (c.r("ah") in (0x01, 0x07, 0x08, 0x0A, 0x0C) or
                                      (c.r("ah") == 0x3F and c.r("bx") == 0)))]
    stuck = [c for c in chain if not blocks_legitimately(c) and hz and (now_tsc - c.tsc) / hz > hang_seconds]
    if reading and stuck and reading[-1].depth > stuck[-1].depth:
        stuck = []  # e.g. a DOS call waiting at an Abort/Retry/Fail prompt: waiting for a key, not hung
    if stuck and window[-1].t - window[0].t >= hang_seconds * 0.8:
        s = stuck[-1]
        return "hung", ("stuck inside INT %02Xh %s (called from %s%s) for %.0f emulated seconds; a call like "
                        "that should return at once" % (s.vector, dosinfo.describe(s)[0], s.caller(),
                                                        "" if s.mode == 0 else " [%s]" % dosinfo.MODES[s.mode],
                                                        (now_tsc - s.tsc) / hz))
    if reading and not meaningful:
        r = reading[-1]
        return "waiting", "waiting for keyboard input (inside INT %02Xh %s, called from %s)" % (
            r.vector, dosinfo.describe(r)[0], r.caller())
    progressing = len(set(sig_window)) > 1
    if spread <= 0x400 and progressing and not meaningful:
        return "running", ("spends its time in a small loop at %08X-%08X (a wait loop: memory or screen keeps "
                           "changing)" % (min(pcs), max(pcs)))
    if spread <= 0x400:
        if len(sig_window) < 2:
            return "running", "possible hang at %08X-%08X, still checking" % (min(pcs), max(pcs))
        if inner is not None and inner.vector == 0x16 and inner.r("ah") in (0x00, 0x10):
            return "waiting", "waiting for a key (inside INT 16h read key, called from %s)" % inner.caller()
        if not new_calls:
            return "hung", ("the CPU stayed within %d bytes (%08X-%08X) for %.0fs, made no INT calls, and "
                            "neither the program's memory nor the screen changed" % (
                                spread + 1, min(pcs), max(pcs), window[-1].t - window[0].t))
        if not meaningful:
            kinds = sorted({"INT %02Xh %s" % (c.vector, dosinfo.describe(c)[0]) for c in new_calls})
            if all(c.vector == 0x16 or (c.vector == 0x21 and c.r("ah") in (0x06, 0x0B)) for c in new_calls):
                return "waiting", "polling the keyboard in a small loop: " + "; ".join(kinds[:3])
            return "hung", ("the CPU stayed within %d bytes (%08X-%08X) for %.0fs, only polling: %s"
                            % (spread + 1, min(pcs), max(pcs), window[-1].t - window[0].t, "; ".join(kinds[:3])))
    return "running", "CPU moving over %d KB of code, %s INT calls in the last %.0fs" % (
        spread // 1024 + 1, "making" if active else "no", window[-1].t - window[0].t)
