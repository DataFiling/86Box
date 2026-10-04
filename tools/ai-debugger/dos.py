"""x86 real-mode / DOS helpers layered on GdbClient: address parsing,
register formatting, BIOS text-screen decoding and disassembly."""

import re

try:
    import capstone
except ImportError:  # disassembly is optional
    capstone = None

SEGS = ("cs", "ss", "ds", "es", "fs", "gs")
REG16 = {"ax": "eax", "cx": "ecx", "dx": "edx", "bx": "ebx", "sp": "esp", "bp": "ebp",
         "si": "esi", "di": "edi"}
REG8 = {"al": ("eax", 0), "ah": ("eax", 8), "bl": ("ebx", 0), "bh": ("ebx", 8),
        "cl": ("ecx", 0), "ch": ("ecx", 8), "dl": ("edx", 0), "dh": ("edx", 8)}
FLAG_BITS = [(0, "CF"), (2, "PF"), (4, "AF"), (6, "ZF"), (7, "SF"), (8, "TF"), (9, "IF"),
             (10, "DF"), (11, "OF"), (14, "NT"), (16, "RF"), (17, "VM")]


def cpu_mode(regs):
    if not regs["cr0"] & 1:
        return "real"
    if regs["eflags"] & (1 << 17):
        return "v86"
    return "protected"


def segmented(regs):
    """True when segment:offset maps to segment * 16 + offset."""
    return cpu_mode(regs) in ("real", "v86")


def ip(regs):
    """The offset part of CS:IP. The stub only reports the linear PC, so this
    is None when CS's hidden base is not CS * 16 (e.g. right after a 286+
    reset, where CS=F000 has base FFFF0000 until the first far jump)."""
    if segmented(regs):
        off = regs["eip"] - regs["cs"] * 16
        if 0 <= off <= 0xFFFF:
            return off
    return None


def reg_value(name, regs):
    name = name.lower()
    if name in ("ip", "eip") and segmented(regs):
        if ip(regs) is None:
            raise ValueError("CS base is not CS*16 (just after reset?); use the linear address %X" % regs["eip"])
        return ip(regs)
    if name == "pc":
        return regs["eip"]
    if name in regs:
        return regs[name]
    if name in REG16:
        return regs[REG16[name]] & 0xFFFF
    if name in REG8:
        full, shift = REG8[name]
        return (regs[full] >> shift) & 0xFF
    raise ValueError("unknown register %r" % name)


def parse_number(text):
    """Bare numbers are hexadecimal, as in DEBUG.COM; "0x" and "h" are accepted."""
    text = text.strip().lower()
    if text.endswith("h"):
        text = text[:-1]
    return int(text, 16)


def parse_term(text, regs):
    """A register name or a number, optionally with +/- displacements."""
    total = 0
    for sign, part in re.findall(r"([+-]?)\s*([^+-]+)", text.strip()):
        part = part.strip()
        value = None
        if regs is not None and (part.lower() in regs or part.lower() in REG16 or part.lower() in REG8
                                 or part.lower() in ("ip", "pc")):
            value = reg_value(part, regs)
        if value is None:
            value = parse_number(part)
        total += -value if sign == "-" else value
    return total


def parse_address(expr, regs):
    """Turn an address expression into a linear address.

    Accepts "1234:0100", "ds:si", "es:di+10", "ss:sp", "cs:ip", plain linear
    addresses ("0xB8000", "B8000h", "b8000") and register expressions
    ("esi+4"). Bare numbers are hexadecimal, as in DEBUG.COM.
    """
    expr = expr.strip()
    if ":" in expr:
        seg_text, off_text = expr.split(":", 1)
        seg = parse_term(seg_text, regs)
        off = parse_term(off_text, regs)
        if regs is not None and not segmented(regs):
            raise ValueError(
                "CPU is in protected mode; segment bases are not exposed by the stub. "
                "Use a linear address instead (for flat DOS extenders, linear == offset).")
        return (seg * 16 + off) & 0xFFFFFFFF
    return parse_term(expr, regs) & 0xFFFFFFFF


def format_registers(regs):
    mode = cpu_mode(regs)
    flags = " ".join(n for bit, n in FLAG_BITS if regs["eflags"] & (1 << bit))
    iopl = (regs["eflags"] >> 12) & 3
    lines = []
    if ip(regs) is not None:
        lines.append("CS:IP = %04X:%04X  (linear %05X)   mode: %s" % (regs["cs"], ip(regs), regs["eip"], mode))
    else:
        lines.append("PC (linear) = %08X   CS = %04X   mode: %s" % (regs["eip"], regs["cs"], mode))
    lines.append("EAX=%08X EBX=%08X ECX=%08X EDX=%08X" % (regs["eax"], regs["ebx"], regs["ecx"], regs["edx"]))
    lines.append("ESI=%08X EDI=%08X EBP=%08X ESP=%08X" % (regs["esi"], regs["edi"], regs["ebp"], regs["esp"]))
    lines.append("DS=%04X ES=%04X SS=%04X FS=%04X GS=%04X" % (regs["ds"], regs["es"], regs["ss"], regs["fs"], regs["gs"]))
    lines.append("EFLAGS=%08X [%s] IOPL=%d" % (regs["eflags"], flags, iopl))
    lines.append("CR0=%08X CR2=%08X CR3=%08X CR4=%08X" % (regs["cr0"], regs["cr2"], regs["cr3"], regs["cr4"]))
    return "\n".join(lines)


def hexdump(data, base, seg=None):
    lines = []
    for off in range(0, len(data), 16):
        row = data[off:off + 16]
        if seg is not None:
            addr = "%04X:%04X" % (seg, (base - seg * 16 + off) & 0xFFFF)
        else:
            addr = "%08X" % (base + off)
        hexpart = " ".join("%02X" % b for b in row)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        lines.append("%s  %-47s  %s" % (addr, hexpart, text))
    return "\n".join(lines)


def disassemble(data, linear, bits=16, seg=None, count=None):
    if capstone is None:
        return "capstone is not installed (pip install capstone); raw bytes:\n" + hexdump(data, linear, seg)
    mode = {16: capstone.CS_MODE_16, 32: capstone.CS_MODE_32}[bits]
    md = capstone.Cs(capstone.CS_ARCH_X86, mode)
    # In 16-bit code capstone needs the offset, not the linear address, so
    # that relative jump targets come out as offsets within CS.
    start = (linear - seg * 16) if seg is not None else linear
    lines = []
    for insn in md.disasm(data, start):
        if seg is not None:
            addr = "%04X:%04X" % (seg, insn.address & 0xFFFF)
        else:
            addr = "%08X" % insn.address
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


def read_ivt(client, first=0, count=256):
    data = client.read_memory(first * 4, count * 4)
    lines = []
    for i in range(count):
        off = int.from_bytes(data[i * 4:i * 4 + 2], "little")
        seg = int.from_bytes(data[i * 4 + 2:i * 4 + 4], "little")
        lines.append("INT %02Xh -> %04X:%04X" % (first + i, seg, off))
    return "\n".join(lines)
