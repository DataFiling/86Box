"""x86 / DOS helpers layered on GdbClient: address parsing in real, V86 and
protected mode, descriptor decoding, register formatting, BIOS text-screen
decoding and disassembly."""

import re
from collections import namedtuple

try:
    import capstone
except ImportError:  # disassembly is optional
    capstone = None

SEGS = ("cs", "ss", "ds", "es", "fs", "gs")
REG16 = {"ax": "eax", "cx": "ecx", "dx": "edx", "bx": "ebx", "sp": "esp", "bp": "ebp",
         "si": "esi", "di": "edi"}
REG8 = {"al": ("eax", 0), "ah": ("eax", 8), "bl": ("ebx", 0), "bh": ("ebx", 8),
        "cl": ("ecx", 0), "ch": ("ecx", 8), "dl": ("edx", 0), "dh": ("edx", 8)}
REG32 = ("eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi", "eflags",
         "cs", "ss", "ds", "es", "fs", "gs", "cr0", "cr2", "cr3", "cr4")
FLAG_BITS = [(0, "CF"), (2, "PF"), (4, "AF"), (6, "ZF"), (7, "SF"), (8, "TF"), (9, "IF"),
             (10, "DF"), (11, "OF"), (14, "NT"), (16, "RF"), (17, "VM")]

# A resolved address: its linear address and, when it was given as SEG:OFF,
# the selector, segment base and code size (16/32) used to label it.
Loc = namedtuple("Loc", "linear sel base bits")


def cpu_mode(regs):
    if not regs["cr0"] & 1:
        return "real"
    if regs["eflags"] & (1 << 17):
        return "v86"
    return "protected"


def segmented(regs):
    """True when segment:offset maps to segment * 16 + offset."""
    return cpu_mode(regs) in ("real", "v86")


def seg_cache(regs, name):
    """The descriptor cache of a segment register: sel, base, limit, access, flags.

    Comes from the stub's "sg" command; older 86Box builds without it only
    work in real/V86 mode, where base = selector * 16."""
    if regs.get("seg"):
        return regs["seg"][name]
    if segmented(regs):
        return {"sel": regs[name], "base": regs[name] * 16, "limit": 0xFFFF, "access": 0, "flags": 0}
    raise ValueError("This 86Box build does not report segment bases (no \"sg\" monitor command); "
                     "rebuild it, or use linear addresses.")


def code_bits(regs):
    """Size of the code currently executing: 16 or 32."""
    if segmented(regs):
        return 16
    if regs.get("seg"):
        return 32 if regs["seg"]["cpu"]["use32"] else 16
    return 32


def stack_bits(regs):
    if segmented(regs):
        return 16
    if regs.get("seg"):
        return 32 if regs["seg"]["cpu"]["stack32"] else 16
    return 32


def ip(regs):
    """The offset part of CS:(E)IP. The stub reports the linear PC, so this is
    PC minus CS's cached base, which also covers a 286+ right after reset (CS=F000,
    base FFFF0000). None if the base is unknown (protected mode, old build)."""
    try:
        cs = seg_cache(regs, "cs")
    except ValueError:
        return None
    off = (regs["eip"] - cs["base"]) & 0xFFFFFFFF
    if segmented(regs) and off > 0xFFFF:
        return None
    return off


def reg_value(name, regs):
    name = name.lower()
    if name in ("ip", "eip"):
        if ip(regs) is None:
            raise ValueError("cannot tell IP without CS's base; use the linear address %X" % regs["eip"])
        return ip(regs)
    if name == "pc":
        return regs["eip"]
    if name in REG32:
        return regs[name]
    if name in REG16:
        return regs[REG16[name]] & 0xFFFF
    if name in REG8:
        full, shift = REG8[name]
        return (regs[full] >> shift) & 0xFF
    raise ValueError("unknown register %r" % name)


def is_register(name):
    name = name.lower()
    return name in REG32 or name in REG16 or name in REG8 or name in ("ip", "eip", "pc")


def parse_number(text):
    """Bare numbers are hexadecimal, as in DEBUG.COM; "0x" and "h" are accepted."""
    orig = text
    text = text.strip().lower()
    if text.endswith("h"):
        text = text[:-1]
    try:
        return int(text, 16)
    except ValueError:
        raise ValueError("can't read %r as a register or hex number" % orig) from None


def parse_term(text, regs):
    """A register name or a number, optionally with +/- displacements."""
    total = 0
    for sign, part in re.findall(r"([+-]?)\s*([^+-]+)", text.strip()):
        part = part.strip()
        if regs is not None and is_register(part):
            value = reg_value(part, regs)
        else:
            value = parse_number(part)
        total += -value if sign == "-" else value
    return total


def resolve(expr, regs, client=None):
    """Turn an address expression into a Loc.

    Accepts "1234:0100", "ds:si", "es:di+10", "ss:esp", "cs:eip", "0028:00401000",
    plain linear addresses ("0xB8000", "B8000h", "b8000") and register
    expressions ("esi+4"). Bare numbers are hexadecimal, as in DEBUG.COM.
    Segment registers use their cached bases in every mode; in protected mode a
    numeric selector is looked up in the GDT or LDT (needs `client`).
    """
    expr = expr.strip()
    if ":" not in expr:
        return Loc(parse_term(expr, regs) & 0xFFFFFFFF, None, None, None)
    seg_text, off_text = expr.split(":", 1)
    seg_text = seg_text.strip().lower()
    off = parse_term(off_text, regs)
    if seg_text in SEGS:
        cache = seg_cache(regs, seg_text)
        sel, base = cache["sel"], cache["base"]
        bits = 16 if segmented(regs) else (32 if cache["flags"] & 0x40 else 16)
    else:
        sel = parse_term(seg_text, regs) & 0xFFFF
        if segmented(regs):
            base, bits = sel * 16, 16
        else:
            if client is None:
                raise ValueError("looking up selector %04X needs the debugger connection" % sel)
            desc = read_descriptor(client, regs, sel)
            if not desc.get("segment"):
                raise ValueError("selector %04X is a %s, not a code/data segment" % (sel, desc["kind"]))
            base, bits = desc["base"], desc["bits"]
    return Loc((base + off) & 0xFFFFFFFF, sel, base, bits)


def describe(loc, regs):
    """How an address was resolved, e.g. "linear 00012340 (0018:0000, base 00012340, from the GDT)"."""
    if loc.sel is None:
        return "linear %08X" % loc.linear
    if segmented(regs):
        kind = "real-mode" if cpu_mode(regs) == "real" else "V86"
        if loc.base == loc.sel * 16:
            how = "%s segment, %04X*16" % (kind, loc.sel)
        else:  # e.g. CS=F000 with base FFFF0000 right after a 286+ reset
            how = "%s segment %04X, cached base %08X" % (kind, loc.sel, loc.base)
    else:
        how = "protected-mode selector, base %08X" % loc.base
    return "linear %08X (%s)" % (loc.linear, how)


def parse_address(expr, regs, client=None):
    """Linear address of an address expression (see resolve)."""
    return resolve(expr, regs, client).linear


# ---- descriptors ------------------------------------------------------------

SYSTEM_TYPES = {1: "16-bit TSS (available)", 2: "LDT", 3: "16-bit TSS (busy)", 4: "16-bit call gate",
                5: "task gate", 6: "16-bit interrupt gate", 7: "16-bit trap gate",
                9: "32-bit TSS (available)", 0xB: "32-bit TSS (busy)", 0xC: "32-bit call gate",
                0xE: "32-bit interrupt gate", 0xF: "32-bit trap gate"}
GATES = (4, 5, 6, 7, 0xC, 0xE, 0xF)


def decode_descriptor(raw):
    """Decode an 8-byte GDT/LDT/IDT entry into a dict."""
    lo = int.from_bytes(raw[0:4], "little")
    hi = int.from_bytes(raw[4:8], "little")
    if lo == 0 and hi == 0:
        return {"kind": "null", "present": False}
    access = (hi >> 8) & 0xFF
    typ = access & 0xF
    d = {"access": access, "present": bool(access & 0x80), "dpl": (access >> 5) & 3}
    base = (lo >> 16) | ((hi & 0xFF) << 16) | (hi & 0xFF000000)
    limit = (lo & 0xFFFF) | (hi & 0xF0000)
    if hi & 0x800000:  # granularity: 4 KiB units
        limit = (limit << 12) | 0xFFF
    if access & 0x10:
        if typ & 8:
            kind = "code" + (" conforming" if typ & 4 else "") + (" readable" if typ & 2 else " exec-only")
        else:
            kind = "data" + (" expand-down" if typ & 4 else "") + (" writable" if typ & 2 else " read-only")
        d.update(kind=kind, segment=True, base=base, limit=limit, bits=32 if hi & 0x400000 else 16)
    elif typ in GATES:
        d.update(kind=SYSTEM_TYPES[typ], selector=(lo >> 16) & 0xFFFF,
                 offset=(lo & 0xFFFF) | ((hi & 0xFFFF0000) if typ & 8 else 0))
    else:
        d.update(kind=SYSTEM_TYPES.get(typ, "reserved system type %X" % typ), base=base, limit=limit)
    return d


def read_descriptor(client, regs, sel):
    """Read and decode the GDT/LDT descriptor for a selector."""
    seg = regs.get("seg")
    if not seg:
        raise ValueError("This 86Box build does not report descriptor tables; rebuild it.")
    table = seg["ldt"] if sel & 4 else seg["gdt"]
    index = sel >> 3
    if index * 8 + 7 > table["limit"]:
        raise ValueError("selector %04X is outside the %s (limit %X)" % (sel, "LDT" if sel & 4 else "GDT", table["limit"]))
    if not sel & 4 and index == 0:
        return {"kind": "null", "present": False}
    return decode_descriptor(client.read_memory(table["base"] + index * 8, 8))


def format_descriptor(d):
    if d["kind"] == "null":
        return "null"
    flags = ("" if d["present"] else "NOT PRESENT, ") + "DPL%d" % d["dpl"]
    if d.get("segment"):
        return "%-28s base=%08X limit=%08X %d-bit  %s" % (d["kind"], d["base"], d["limit"], d["bits"], flags)
    if "selector" in d:
        if d["kind"] == "task gate":
            return "%-28s TSS selector=%04X  %s" % (d["kind"], d["selector"], flags)
        return "%-28s -> %04X:%08X  %s" % (d["kind"], d["selector"], d["offset"], flags)
    return "%-28s base=%08X limit=%08X  %s" % (d["kind"], d["base"], d["limit"], flags)


def read_table(client, regs, table, first, count):
    """Decoded entries of the GDT, LDT or IDT."""
    seg = regs.get("seg")
    if not seg:
        raise ValueError("This 86Box build does not report descriptor tables; rebuild it.")
    t = seg[{"gdt": "gdt", "ldt": "ldt", "idt": "idt"}[table]]
    n = (t["limit"] + 1) // 8
    if n == 0:
        return "%s is empty (base=%08X limit=%X)." % (table.upper(), t["base"], t["limit"])
    first = min(first, n - 1)
    count = max(1, min(count, n - first))
    raw = client.read_memory(t["base"] + first * 8, count * 8)
    lines = ["%s base=%08X limit=%X (%d entries)" % (table.upper(), t["base"], t["limit"], n)]
    for i in range(count):
        idx = first + i
        label = ("INT %02Xh" % idx) if table == "idt" else ("%04X" % (idx * 8 + (4 if table == "ldt" else 0)))
        lines.append("%-8s %s" % (label, format_descriptor(decode_descriptor(raw[i * 8:i * 8 + 8]))))
    return "\n".join(lines)


def format_registers(regs):
    mode = cpu_mode(regs)
    flags = " ".join(n for bit, n in FLAG_BITS if regs["eflags"] & (1 << bit))
    iopl = (regs["eflags"] >> 12) & 3
    off = ip(regs)
    lines = []
    if segmented(regs):
        if off is not None:
            lines.append("CS:IP = %04X:%04X  (linear %05X)   mode: %s" % (regs["cs"], off, regs["eip"], mode))
        else:
            lines.append("PC (linear) = %08X   CS = %04X   mode: %s" % (regs["eip"], regs["cs"], mode))
    else:
        desc = "mode: protected, %d-bit code, %d-bit stack" % (code_bits(regs), stack_bits(regs))
        if regs.get("seg"):
            desc += ", CPL %d" % regs["seg"]["cpu"]["cpl"]
        if regs["cr0"] & 0x80000000:
            desc += ", paging on"
        if off is None:
            lines.append("PC (linear) = %08X   CS = %04X   %s" % (regs["eip"], regs["cs"], desc))
        else:
            fmt = "CS:EIP = %04X:%08X" if code_bits(regs) == 32 else "CS:IP = %04X:%04X"
            lines.append((fmt + "  (linear %08X)   %s") % (regs["cs"], off, regs["eip"], desc))
    lines.append("EAX=%08X EBX=%08X ECX=%08X EDX=%08X" % (regs["eax"], regs["ebx"], regs["ecx"], regs["edx"]))
    lines.append("ESI=%08X EDI=%08X EBP=%08X ESP=%08X" % (regs["esi"], regs["edi"], regs["ebp"], regs["esp"]))
    lines.append("DS=%04X ES=%04X SS=%04X FS=%04X GS=%04X" % (regs["ds"], regs["es"], regs["ss"], regs["fs"], regs["gs"]))
    seg = regs.get("seg")
    if seg and not segmented(regs):
        lines.append("Bases: " + " ".join("%s=%08X" % (n.upper(), seg[n]["base"]) for n in SEGS))
    elif seg and mode == "real":
        big = [n.upper() for n in SEGS if seg[n]["limit"] > 0xFFFF]
        if big:
            lines.append("Unreal mode: %s keep limits above 64 KiB from protected mode" % ", ".join(big))
    lines.append("EFLAGS=%08X [%s] IOPL=%d" % (regs["eflags"], flags, iopl))
    lines.append("CR0=%08X CR2=%08X CR3=%08X CR4=%08X" % (regs["cr0"], regs["cr2"], regs["cr3"], regs["cr4"]))
    return "\n".join(lines)


def format_segments(regs):
    """Segment descriptor caches and descriptor table registers."""
    seg = regs.get("seg")
    if not seg:
        return "This 86Box build does not report segment descriptors; rebuild it."
    mode = cpu_mode(regs)
    lines = ["CPU: %s mode, %d-bit code, %d-bit stack, CPL %d%s" % (
        mode, code_bits(regs), stack_bits(regs), seg["cpu"]["cpl"],
        ", paging on" if regs["cr0"] & 0x80000000 else "")]
    lines.append("      sel   base      limit     description")
    for n in SEGS:
        c = seg[n]
        if segmented(regs):
            what = "real-mode segment" if c["limit"] <= 0xFFFF else "real-mode segment with a big (unreal) limit"
        elif c["sel"] & 0xFFFC == 0:
            what = "null selector"
        else:
            # Rebuild the descriptor's access and flags bytes to reuse the decoder.
            d = decode_descriptor(bytes(5) + bytes([c["access"], c["flags"], 0]))
            what = "%s, %d-bit, DPL%d%s" % (d["kind"], d.get("bits", 16), d["dpl"], "" if d["present"] else ", not present")
        lines.append("%-4s  %04X  %08X  %08X  %s" % (n.upper(), c["sel"], c["base"], c["limit"], what))
    lines.append("GDTR  base=%08X limit=%04X (%d entries)" % (seg["gdt"]["base"], seg["gdt"]["limit"], (seg["gdt"]["limit"] + 1) // 8))
    lines.append("IDTR  base=%08X limit=%04X" % (seg["idt"]["base"], seg["idt"]["limit"]))
    lines.append("LDTR  sel=%04X base=%08X limit=%08X" % (seg["ldt"]["sel"], seg["ldt"]["base"], seg["ldt"]["limit"]))
    lines.append("TR    sel=%04X base=%08X limit=%08X" % (seg["tr"]["sel"], seg["tr"]["base"], seg["tr"]["limit"]))
    return "\n".join(lines)


def _label(loc, linear):
    """SEL:OFF for addresses given as SEG:OFF, else the linear address."""
    if loc is None or loc.sel is None:
        return "%08X" % linear
    off = (linear - loc.base) & 0xFFFFFFFF
    return ("%04X:%04X" if loc.bits == 16 and off <= 0xFFFF else "%04X:%08X") % (loc.sel, off)


def hexdump(data, linear, loc=None):
    lines = []
    for off in range(0, len(data), 16):
        row = data[off:off + 16]
        hexpart = " ".join("%02X" % b for b in row)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        lines.append("%s  %-47s  %s" % (_label(loc, linear + off), hexpart, text))
    return "\n".join(lines)


def disassemble(data, linear, bits=16, loc=None, count=None):
    if capstone is None:
        return "capstone is not installed (pip install capstone); raw bytes:\n" + hexdump(data, linear, loc)
    mode = {16: capstone.CS_MODE_16, 32: capstone.CS_MODE_32}[bits]
    md = capstone.Cs(capstone.CS_ARCH_X86, mode)
    # Capstone gets the offset within the segment, not the linear address,
    # so that relative jump targets come out as offsets too.
    seg_base = loc.base if (loc is not None and loc.sel is not None) else 0
    lines = []
    for insn in md.disasm(data, (linear - seg_base) & 0xFFFFFFFF):
        addr = _label(loc, seg_base + insn.address)
        lines.append("%s  %-20s %s %s" % (addr, insn.bytes.hex().upper(), insn.mnemonic, insn.op_str))
        if count and len(lines) >= count:
            break
    return "\n".join(lines) if lines else "(could not decode)"


# ---- BIOS data area / text screen ------------------------------------------

BDA = 0x400


def read_text_screen(client, include_attributes=False):
    bda = client.read_memory(BDA, 0x100)
    mode = bda[0x49]
    cols = int.from_bytes(bda[0x4A:0x4C], "little") or 80
    page_start = int.from_bytes(bda[0x4E:0x50], "little")
    page = bda[0x62]
    rows = (bda[0x84] + 1) if bda[0x84] else 25
    cursor = int.from_bytes(bda[0x50 + page * 2:0x52 + page * 2], "little")
    cur_col, cur_row = cursor & 0xFF, cursor >> 8

    if mode == 7:
        base = 0xB0000
    elif mode in (0, 1, 2, 3):
        base = 0xB8000
    else:
        # Other modes (SVGA/VESA text such as 132 columns, or graphics): ask
        # the VGA graphics controller whether it is in text mode and where
        # its memory map is, restoring the controller's index afterwards.
        base = vga_text_base(client)
        if base is None:
            return {"mode": mode, "text": None,
                    "note": "Video mode %02Xh is a graphics mode; text is not in memory. "
                            "Use the screenshot tool." % mode}

    if not (1 <= cols <= 132 and 1 <= rows <= 60):
        cols, rows = 80, 25
    raw = client.read_memory(base + page_start, cols * rows * 2)
    out_lines, attr_lines = [], []
    for r in range(rows):
        row = raw[r * cols * 2:(r + 1) * cols * 2]
        chars = bytes(row[0::2]).decode("cp437", errors="replace").replace("\x00", " ")
        out_lines.append(chars.rstrip())
        if include_attributes:
            attr_lines.append("".join("%02X" % a for a in row[1::2]))
    result = {"mode": mode, "columns": cols, "rows": rows, "active_page": page,
              "cursor": {"row": cur_row, "col": cur_col}, "text": "\n".join(out_lines)}
    if include_attributes:
        result["attributes"] = attr_lines
    return result


def vga_text_base(client):
    """Base of text memory if the VGA graphics controller is in text mode,
    else None. Reads GC register 6 (Miscellaneous) through 3CEh/3CFh."""
    def inb(port):
        return int(client.monitor("ib 0x%x 1" % port).split()[1], 16)

    index = inb(0x3CE)
    client.monitor("ob 0x3ce 0x6")
    misc = inb(0x3CF)
    client.monitor("ob 0x3ce 0x%x" % index)
    if misc & 1:  # graphics
        return None
    return {0: 0xA0000, 1: 0xA0000, 2: 0xB0000, 3: 0xB8000}[(misc >> 2) & 3]


def read_ivt(client, first=0, count=256):
    data = client.read_memory(first * 4, count * 4)
    lines = []
    for i in range(count):
        off = int.from_bytes(data[i * 4:i * 4 + 2], "little")
        seg = int.from_bytes(data[i * 4 + 2:i * 4 + 4], "little")
        lines.append("INT %02Xh -> %04X:%04X" % (first + i, seg, off))
    return "\n".join(lines)
