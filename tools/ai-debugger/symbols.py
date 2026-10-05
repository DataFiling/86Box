"""Symbols from Open Watcom linker maps, relocated to where the program is
loaded, so addresses can be given and shown as names.

Two kinds of maps:
- 16-bit DOS programs: addresses are SEG:OFF with SEG relative to the load
  segment (PSP+10h), so linear = (load_segment + SEG) * 16 + OFF.
- 32-bit linear executables (DOS/4GW and other LE/LX extenders): addresses
  are OBJECT:OFFSET, so linear = base of that object + OFFSET.
"""

import bisect
import re

_SYM = re.compile(r"^([0-9a-fA-F]{4}):([0-9a-fA-F]{4}|[0-9a-fA-F]{8})([+s* ]*)\s+(\S.*?)\s*$")
_SEG = re.compile(r"^(\S+)\s+(\S+)\s+(\S+)\s+([0-9a-fA-F]{4}):([0-9a-fA-F]{4,8})\s+([0-9a-fA-F]{8})\s*$")
_ENTRY = re.compile(r"^Entry point address:\s+([0-9a-fA-F]{4}):([0-9a-fA-F]{4,8})")


class MapFile:
    def __init__(self, path):
        self.path = path
        self.symbols = []   # (segment or object, offset, name)
        self.segments = []  # (name, class, group, seg/object, offset, size)
        self.entry = None
        self.image = ""
        self.wide = False   # 32-bit object:offset addresses
        section = ""
        with open(path, encoding="latin-1") as f:
            for line in f:
                if "|" in line and line.strip().startswith("|"):
                    section = line.strip(" |\n")
                    continue
                if line.startswith("Executable Image:"):
                    self.image = line.split(":", 1)[1].strip()
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
                    m = _SYM.match(line)
                    if m:
                        name = m.group(4)
                        if " " in name:  # "Module: ..." headers and the like
                            continue
                        if len(m.group(2)) == 8:
                            self.wide = True
                        self.symbols.append((int(m.group(1), 16), int(m.group(2), 16), name))
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
        self.by_addr = []
        code = mapfile.code_objects()
        code_segs = sorted((sg, off, size) for _, cls, _, sg, off, size in mapfile.segments if cls.upper() == "CODE")
        self.code_addrs = []
        for seg, off, name in mapfile.symbols:
            lin = self.linear(seg, off)
            if lin is None:
                continue
            self.by_addr.append((lin, name))
            if mapfile.wide:
                is_code = seg in code
            else:
                is_code = any(sg * 16 + so <= seg * 16 + off < sg * 16 + so + size for sg, so, size in code_segs)
            if is_code:
                self.code_addrs.append((lin, name))
        self.by_addr.sort()
        self.code_addrs.sort()
        self.addrs = [a for a, _ in self.by_addr]
        self.code_keys = [a for a, _ in self.code_addrs]
        # Linear ranges of code, to keep code lookups inside them.
        self.code_ranges = []
        for _, cls, _, sg, off, size in mapfile.segments:
            if cls.upper() == "CODE" and size:
                lo = self.linear(sg, off)
                if lo is not None:
                    self.code_ranges.append((lo, lo + size))
        self.by_name = {}
        for a, n in self.by_addr:
            self.by_name.setdefault(n, a)
            self.by_name.setdefault(n.lower(), a)
        lo = self.addrs[0] if self.addrs else 0
        hi = self.addrs[-1] if self.addrs else 0
        self.span = (lo, hi + 0x10000)

    def linear(self, seg, off):
        if self.map.wide:
            base = self.bases.get(seg)
            return None if base is None else (base + off) & 0xFFFFFFFF
        return ((self.bases["load"] + seg) * 16 + off) & 0xFFFFFFFF

    def lookup(self, name):
        """Linear address of `name`, `name+hex` or `name-hex`; None if unknown."""
        m = re.fullmatch(r"([A-Za-z_$?@][\w$?@.]*)(?:\s*([+-])\s*(?:0x)?([0-9a-fA-F]+)h?)?", name.strip())
        if not m:
            return None
        base = self.by_name.get(m.group(1))
        if base is None:
            base = self.by_name.get(m.group(1) + "_")  # Watcom appends _ to register-call C functions
        if base is None:
            base = self.by_name.get("_" + m.group(1))  # ...and prefixes data with _
        if base is None:
            return None
        if m.group(2):
            delta = int(m.group(3), 16)
            base = base + delta if m.group(2) == "+" else base - delta
        return base & 0xFFFFFFFF

    def name_at(self, linear, max_distance=0x10000):
        """'name+off' for the symbol at or before linear, or None."""
        i = bisect.bisect_right(self.addrs, linear) - 1
        if i < 0:
            return None
        a, n = self.by_addr[i]
        d = linear - a
        if d > max_distance:
            return None
        return n if d == 0 else "%s+%X" % (n, d)

    def code_name_at(self, linear, max_distance=0x10000):
        """Like name_at, for code symbols only (return addresses, branch targets)."""
        if not any(lo <= linear < hi for lo, hi in self.code_ranges):
            return None
        i = bisect.bisect_right(self.code_keys, linear) - 1
        if i < 0:
            return None
        a, n = self.code_addrs[i]
        d = linear - a
        if d > max_distance:
            return None
        return n if d == 0 else "%s+%X" % (n, d)

    def describe_bases(self):
        if self.map.wide:
            return ", ".join("object %d at %08X" % (o, b) for o, b in sorted(self.bases.items()))
        return "load segment %04X" % self.bases["load"]


def guess_object_bases(mapfile, entry_linear, blocks):
    """Object bases for a 32-bit map from the program's entry point (linear)
    and the memory blocks the extender allocated ([(lo, hi)]): the entry
    object's base follows from the entry offset; other objects go to the block
    whose size matches theirs (page-rounded), nearest after the previous one."""
    if mapfile.entry is None:
        raise ValueError("the map has no entry point")
    eobj, eoff = mapfile.entry
    bases = {eobj: (entry_linear - eoff) & 0xFFFFFFFF}
    sizes = mapfile.object_sizes()
    used = {lo for lo, _ in blocks if lo == bases[eobj]}
    for obj in sorted(sizes):
        if obj in bases:
            continue
        want = (sizes[obj] + 0xFFF) & ~0xFFF
        cands = [(lo, hi) for lo, hi in blocks if lo not in used and hi - lo >= sizes[obj] and hi - lo <= want + 0x1000]
        if not cands:
            continue
        lo = min(cands, key=lambda b: (abs((b[1] - b[0]) - want), b[0]))[0]
        bases[obj] = lo
        used.add(lo)
    return bases
