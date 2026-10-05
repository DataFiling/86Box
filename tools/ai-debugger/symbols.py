"""Symbols from Open Watcom linker maps, relocated to where the program is
loaded, so addresses can be given and shown as names.

Three kinds of maps:
- 16-bit DOS .EXE programs: addresses are SEG:OFF with SEG relative to the
  load segment (PSP+10h), so linear = (load_segment + SEG) * 16 + OFF.
- DOS .COM programs: the same, relative to the PSP (code starts at 0100h).
- 32-bit linear executables (DOS/4GW and other LE/LX extenders): addresses
  are OBJECT:OFFSET, so linear = base of that object + OFFSET.
"""

import bisect
import re

_SYM = re.compile(r"^([0-9a-fA-F]{4}):([0-9a-fA-F]{4}|[0-9a-fA-F]{8})([+s* ]*)\s+(\S.*?)\s*$")
_SEG = re.compile(r"^(\S+)\s+(\S+)\s+(\S+)\s+([0-9a-fA-F]{4}):([0-9a-fA-F]{4,8})\s+([0-9a-fA-F]{8})\s*$")
_ENTRY = re.compile(r"^Entry point address:\s+([0-9a-fA-F]{4}):([0-9a-fA-F]{4,8})")
_MODULE = re.compile(r"^Module:\s+(\S+?)(?:\((.*)\))?\s*$")


def _module_name(text):
    base = re.split(r"[\\/]", text)[-1]
    return base.rsplit(".", 1)[0] if "." in base else base


class MapFile:
    def __init__(self, path):
        self.path = path
        self.symbols = []   # (segment or object, offset, name, module)
        self.segments = []  # (name, class, group, seg/object, offset, size)
        self.entry = None
        self.image = ""
        self.wide = False   # 32-bit object:offset addresses
        self.com = False    # .COM program: offsets relative to the PSP
        section, module = "", ""
        with open(path, encoding="latin-1") as f:
            for line in f:
                if line.strip().startswith("|") and line.strip().endswith("|"):
                    section = line.strip(" |\n")
                    continue
                if line.startswith("Executable Image:"):
                    self.image = line.split(":", 1)[1].strip()
                if line.startswith("creating a DOS .COM"):
                    self.com = True
                m = _ENTRY.match(line)
                if m:
                    self.entry = (int(m.group(1), 16), int(m.group(2), 16))
                    continue
                if section == "Segments":
                    m = _SEG.match(line)
                    if m:
                        self.segments.append((m.group(1), m.group(2), m.group(3), int(m.group(4), 16),
                                              int(m.group(5), 16), int(m.group(6), 16)))
                elif section == "Memory Map":
                    m = _MODULE.match(line)
                    if m:
                        module = _module_name(m.group(2) or m.group(1))
                        continue
                    m = _SYM.match(line)
                    if m:
                        if len(m.group(2)) == 8:
                            self.wide = True
                        # C++ names keep their spaces: "int near Foo::bar( int )".
                        self.symbols.append((int(m.group(1), 16), int(m.group(2), 16), m.group(4), module))
        if not self.symbols:
            raise ValueError("%s: no symbols found (is it an Open Watcom map file?)" % path)

    def object_sizes(self):
        """Size of each object (32-bit maps) from the segment table."""
        sizes = {}
        for _, _, _, obj, off, size in self.segments:
            sizes[obj] = max(sizes.get(obj, 0), off + size)
        return sizes

    def code_objects(self):
        return {obj for _, cls, _, obj, _, _ in self.segments if cls.upper() == "CODE"}


class SymbolTable:
    """A map relocated to linear addresses."""

    def __init__(self, mapfile, bases):
        self.map = mapfile
        self.bases = dict(bases)  # 16-bit: {"load": segment}; 32-bit: {object: base}
        if mapfile.wide:
            if not self.bases or any(not isinstance(k, int) for k in self.bases):
                raise ValueError("a 32-bit map needs object bases, e.g. \"1=174000,2=1FF000\"")
        elif set(self.bases) != {"load"}:
            raise ValueError("a 16-bit map needs a load segment, e.g. \"load=240E\"")
        # Segment ranges, to keep names from reaching past their segment.
        self.seg_ranges = []
        self.code_frames = []  # (lo, hi, linear of the segment's offset 0)
        for name, cls, _, sg, off, size in mapfile.segments:
            lo = self.linear(sg, off)
            if lo is not None and size:
                self.seg_ranges.append((lo, lo + size, name, cls.upper() == "CODE"))
                if cls.upper() == "CODE":
                    self.code_frames.append((lo, lo + size, lo - off))
        self.seg_ranges.sort()
        self.seg_starts = [r[0] for r in self.seg_ranges]
        self.code_ranges = [(lo, hi) for lo, hi, _, code in self.seg_ranges if code]
        self.by_addr, self.code_addrs = [], []
        self.modules = {}
        for seg, off, name, module in mapfile.symbols:
            lin = self.linear(seg, off)
            if lin is None:
                continue
            self.by_addr.append((lin, name))
            self.modules.setdefault(name, []).append((lin, module))
            if self._segment(lin) is not None and self._segment(lin)[3]:
                self.code_addrs.append((lin, name))
        if not self.by_addr:
            raise ValueError("no symbol could be placed with bases %s" % self.bases)
        self.by_addr.sort()
        self.code_addrs.sort()
        self.addrs = [a for a, _ in self.by_addr]
        self.code_keys = [a for a, _ in self.code_addrs]
        # Exact names first, then case-insensitive ones that don't shadow any exact name.
        self.lower = {}
        for n in self.modules:
            self.lower.setdefault(n.lower(), n)

    def linear(self, seg, off):
        if self.map.wide:
            base = self.bases.get(seg)
            return None if base is None else (base + off) & 0xFFFFFFFF
        load = self.bases["load"] - (0x10 if self.map.com else 0)  # .COM offsets count from the PSP
        return ((load + seg) * 16 + off) & 0xFFFFFFFF

    def _segment(self, linear):
        i = bisect.bisect_right(self.seg_starts, linear) - 1
        if i >= 0 and linear < self.seg_ranges[i][1]:
            return self.seg_ranges[i]
        return None

    def _resolve_name(self, name):
        """Linear address of a symbol name ("module!name" picks a duplicated static)."""
        module = None
        if "!" in name:
            module, name = name.split("!", 1)
        for cand in (name, name + "_", "_" + name):
            entries = self.modules.get(cand) or self.modules.get(self.lower.get(cand.lower(), ""))
            if not entries:
                continue
            if module:
                entries = [e for e in entries if e[1].lower() == module.lower()]
                if not entries:
                    raise ValueError("no %s in module %s" % (cand, module))
            if len(entries) > 1:
                raise ValueError("%s is defined in several modules (%s); use module!name" % (
                    cand, ", ".join("%s at %08X" % (m or "?", a) for a, m in entries)))
            return entries[0][0]
        return None

    def lookup(self, name):
        """Linear address of `name`, `name+hex` or `name-hex` (also "module!name");
        None if unknown, ValueError if ambiguous."""
        text = name.strip()
        if text in self.modules:  # exact name, including C++ names with spaces
            return self._resolve_name(text)
        m = re.fullmatch(r"((?:[\w$?@.]+!)?[A-Za-z_$?@][\w$?@.]*)(?:\s*([+-])\s*(?:0x)?([0-9a-fA-F]+)h?)?", text)
        if not m:
            return None
        base = self._resolve_name(m.group(1))
        if base is None:
            return None
        if m.group(2):
            delta = int(m.group(3), 16)
            base = base + delta if m.group(2) == "+" else base - delta
        return base & 0xFFFFFFFF

    def _nearest(self, keys, entries, linear, max_distance):
        i = bisect.bisect_right(keys, linear) - 1
        seg = self._segment(linear)
        if i >= 0:
            a, n = entries[i]
            d = linear - a
            # The symbol must lie in the same segment, so stack, BSS tails and
            # other programs' memory aren't labelled as the last symbol + far.
            if d <= max_distance and seg is not None and seg[0] <= a:
                return n if d == 0 else "%s+%X" % (n, d)
        if seg is not None and max_distance > 0:
            return "%s+%X" % (seg[2], linear - seg[0])  # in a segment, before its first symbol
        return None

    def name_at(self, linear, max_distance=0x10000):
        """'name+off' for the symbol at or before linear within its segment, or
        'SEGMENT+off', or None outside the program."""
        return self._nearest(self.addrs, self.by_addr, linear, max_distance)

    def code_name_at(self, linear, max_distance=0x10000):
        """Like name_at, for code symbols only (return addresses, branch targets)."""
        seg = self._segment(linear)
        if seg is None or not seg[3]:
            return None
        return self._nearest(self.code_keys, self.code_addrs, linear, max_distance)

    def check_code(self, read, max_bytes=0x100000):
        """How well the map fits the code in memory: (calls checked, calls that
        land on a code symbol). Near calls to the same place several times are
        almost always real calls; with the right map and bases they go to
        function entries, which are symbols. read(lo, hi) -> bytes."""
        import codeanalysis
        bits = 32 if self.map.wide else 16
        starts = set(self.code_keys)
        total = hits = 0
        budget = max_bytes
        for lo, hi, frame in sorted(self.code_frames):
            if budget <= 0:
                break
            hi = min(hi, lo + budget)
            budget -= hi - lo
            base = lo if bits == 32 else frame
            data = read(base, hi)
            if not data.strip(b"\0"):
                continue
            for t, n in codeanalysis.call_targets(data, base, bits, lo, hi).items():
                if n >= 2:
                    total += 1
                    hits += t in starts
        return total, hits

    def describe_bases(self):
        if self.map.wide:
            return ", ".join("object %d at %08X" % (o, b) for o, b in sorted(self.bases.items()))
        return "%s segment %04X" % ("PSP" if self.map.com else "load", self.bases["load"] - (0x10 if self.map.com else 0))


def guess_object_bases(mapfile, entry_linear, blocks):
    """Object bases for a 32-bit map from the program's entry point (linear)
    and the memory blocks the extender allocated ([(lo, hi)]): the entry
    object's base follows from the entry offset; each other object goes to the
    unused block whose size is closest to its page-rounded size (ties: lowest
    address)."""
    if mapfile.entry is None:
        raise ValueError("the map has no entry point")
    eobj, eoff = mapfile.entry
    bases = {eobj: (entry_linear - eoff) & 0xFFFFFFFF}
    sizes = mapfile.object_sizes()
    used = {lo for lo, _ in blocks if lo == bases[eobj]}
    for obj in sorted(sizes):
        if obj in bases or obj == 0:
            continue
        want = (sizes[obj] + 0xFFF) & ~0xFFF
        cands = [(lo, hi) for lo, hi in blocks if lo not in used and hi - lo >= sizes[obj] and hi - lo <= want + 0x1000]
        if not cands:
            continue
        lo = min(cands, key=lambda b: (abs((b[1] - b[0]) - want), b[0]))[0]
        bases[obj] = lo
        used.add(lo)
    return bases
