"""Memory snapshots, diffs and value scans, for finding a game's variables
(lives, score, position...) the way cheat finders do: scan for a value or for
"changed/decreased" across snapshots until few addresses are left.

Snapshots and scan state are kept on disk (per debugger host and port), so
that they survive between run_tool.py commands, which each run in a new
process. Memory is read with the stub's side-effect-free reader, which skips
device memory (VGA etc.) and unmapped pages."""

import array
import bisect
import os
import pickle
import re
import struct
import tempfile

MAX_CANDIDATES = 4_000_000


class Snapshot:
    """Readable memory runs: sorted list of (linear address, bytes)."""

    def __init__(self, runs, label=""):
        self.runs = runs
        self.starts = [a for a, _ in runs]
        self.label = label

    @property
    def size(self):
        return sum(len(d) for _, d in self.runs)

    def read(self, addr, n):
        i = bisect.bisect_right(self.starts, addr) - 1
        if i < 0:
            return None
        a, d = self.runs[i]
        if addr + n > a + len(d):
            return None
        return d[addr - a:addr - a + n]


def take(client, ranges):
    pieces = []  # [start, [chunks], end] per contiguous run
    for lo, hi in ranges:
        for a, d in client.peek(lo, hi - lo):
            if d:
                if pieces and pieces[-1][2] == a:
                    pieces[-1][1].append(d)
                    pieces[-1][2] += len(d)
                else:
                    pieces.append([a, [d], a + len(d)])
    return Snapshot([(a, b"".join(chunks)) for a, chunks, _ in pieces])


def state_dir(host, port):
    path = os.path.join(tempfile.gettempdir(), "86box-debugger-%s-%d" % (host.replace(":", "_"), port))
    os.makedirs(path, exist_ok=True)
    return path


def _safe(name):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", name):
        raise ValueError("snapshot names use letters, digits, '_', '.' and '-' (got %r)" % name)
    return name


def save(dirpath, name, obj):
    tmp = os.path.join(dirpath, _safe(name) + ".tmp")
    with open(tmp, "wb") as f:
        pickle.dump(obj, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, os.path.join(dirpath, _safe(name) + ".pkl"))


def load(dirpath, name):
    path = os.path.join(dirpath, _safe(name) + ".pkl")
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return pickle.load(f)


def list_saved(dirpath):
    return sorted(f[:-4] for f in os.listdir(dirpath) if f.endswith(".pkl"))


# ---- diff -----------------------------------------------------------------------

def changed_runs(old, new, merge_gap=4):
    """[(address, length)] of bytes that differ between two snapshots (only
    where both have the memory), merging runs closer than merge_gap."""
    out = []
    for a, d in new.runs:
        i = 0
        while i < len(d):
            # Find the old data covering a+i.
            j = bisect.bisect_right(old.starts, a + i) - 1
            if j < 0 or a + i >= old.starts[j] + len(old.runs[j][1]):
                nxt = bisect.bisect_right(old.starts, a + i)
                if nxt >= len(old.starts) or old.starts[nxt] >= a + len(d):
                    break
                i = old.starts[nxt] - a
                continue
            oa, od = old.runs[j]
            end = min(len(d), oa + len(od) - a)
            _diff_span(od, oa, d, a, i, end, out, merge_gap)
            i = end
    return out


def _diff_span(od, oa, d, a, i, end, out, gap):
    CH = 4096
    while i < end:
        n = min(CH, end - i)
        if d[i:i + n] != od[a + i - oa:a + i - oa + n]:
            for k in range(i, i + n):
                if d[k] != od[a + k - oa]:
                    addr = a + k
                    if out and addr - (out[-1][0] + out[-1][1]) <= gap:
                        out[-1] = (out[-1][0], addr + 1 - out[-1][0])
                    else:
                        out.append((addr, 1))
        i += n


# ---- value scans ------------------------------------------------------------------

def _fmt(size, signed):
    return {1: "b", 2: "h", 4: "i"}[size] if signed else {1: "B", 2: "H", 4: "I"}[size]


def value_at(snap, addr, size, signed):
    d = snap.read(addr, size)
    return None if d is None else struct.unpack("<" + _fmt(size, signed), d)[0]


def parse_number(s):
    s = s.strip()
    neg = s.startswith("-")
    if neg:
        s = s[1:]
    v = int(s, 16) if s.lower().startswith("0x") else (int(s[:-1], 16) if s.lower().endswith("h") else int(s, 10))
    return -v if neg else v


COND_HELP = ('a value ("3", "=3", "!=0", ">100", "<=5"; decimal unless 0x or h), "changed", "unchanged", '
             '"increased", "decreased", "+N" (increased by N) or "-N" (decreased by N)')


def parse_condition(text):
    """-> (kind, number) where kind is one of = != > < >= <= changed unchanged increased decreased inc_by dec_by."""
    t = text.strip().lower().replace(" ", "")
    if t in ("changed", "unchanged", "increased", "decreased", "same"):
        return ("unchanged" if t == "same" else t), None
    num = r"(0x[0-9a-f]+|[0-9a-f]+h|\d+)"
    m = re.fullmatch(r"([+-])" + num, t)
    if m:
        return ("inc_by" if m.group(1) == "+" else "dec_by"), parse_number(m.group(2))
    m = re.fullmatch(r"(=|==|!=|>=|<=|>|<)?(-?" + num + ")", t)
    if m:
        op = m.group(1) or "="
        return ("=" if op == "==" else op), parse_number(m.group(2))
    raise ValueError("unknown condition %r; use %s" % (text, COND_HELP))


def check(kind, n, old, new, size):
    if new is None:
        return False
    mask = (1 << (8 * size)) - 1
    if kind == "=":
        return new == n
    if kind == "!=":
        return new != n
    if kind == ">":
        return new > n
    if kind == "<":
        return new < n
    if kind == ">=":
        return new >= n
    if kind == "<=":
        return new <= n
    if old is None:
        return False
    if kind == "changed":
        return new != old
    if kind == "unchanged":
        return new == old
    if kind == "increased":
        return new > old
    if kind == "decreased":
        return new < old
    if kind == "inc_by":
        return (new - old) & mask == n & mask
    if kind == "dec_by":
        return (old - new) & mask == n & mask
    return False


def find_value(snap, value, size, signed, limit=MAX_CANDIDATES):
    """Every address (any alignment) holding value; None if over limit."""
    try:
        pat = struct.pack("<" + _fmt(size, signed), value)
    except struct.error:
        pat = struct.pack("<" + _fmt(size, not signed), value)
    out = array.array("I")
    for a, d in snap.runs:
        i = d.find(pat)
        while i >= 0:
            out.append(a + i)
            if len(out) > limit:
                return None
            i = d.find(pat, i + 1)
    return out


def touched(runs, size):
    """Start addresses of size-byte values overlapping any changed run."""
    s = set()
    for a, n in runs:
        s.update(range(a - size + 1, a + n))
    return s


class Scan:
    """A value scan: candidate addresses (explicit array, or every address in
    the scanned memory minus `excluded` while no value has been given yet)."""

    def __init__(self, ranges, size, signed, region_desc):
        self.ranges = ranges
        self.size = size
        self.signed = signed
        self.region = region_desc
        self.cands = None
        self.excluded = set()
        self.steps = []
        self.total = 0

    def count(self):
        return len(self.cands) if self.cands is not None else max(0, self.total - len(self.excluded))

    def start(self, snap, value):
        self.total = snap.size
        if value is None:
            self.cands = None
            self.steps.append("start: unknown value, %d bytes of memory" % snap.size)
            return
        found = find_value(snap, value, self.size, self.signed)
        if found is None:
            raise ValueError("more than %d addresses hold %d; use a larger value size or a smaller region"
                             % (MAX_CANDIDATES, value))
        self.cands = found
        self.steps.append("start: = %d" % value)

    def next(self, old, new, kind, n):
        size, signed = self.size, self.signed
        if self.cands is None:
            if kind in ("=",):
                found = find_value(new, n, size, signed)
                if found is None:
                    raise ValueError("more than %d addresses hold %d" % (MAX_CANDIDATES, n))
                self.cands = array.array("I", (a for a in found if a not in self.excluded))
            elif kind == "unchanged":
                self.excluded |= touched(changed_runs(old, new), size)
            elif kind in ("changed", "increased", "decreased", "inc_by", "dec_by"):
                pos = sorted(touched(changed_runs(old, new), size) - self.excluded)
                self.cands = array.array("I", (a for a in pos if check(kind, n, value_at(old, a, size, signed),
                                                                       value_at(new, a, size, signed), size)))
            else:
                raise ValueError("with no starting value, use a value (=N), changed, unchanged, increased, "
                                 "decreased, +N or -N first")
        else:
            self.cands = array.array("I", (a for a in self.cands if check(
                kind, n, value_at(old, a, size, signed), value_at(new, a, size, signed), size)))
        self.steps.append("%s%s" % (kind, "" if n is None else " %d" % n))
