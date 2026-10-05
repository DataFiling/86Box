"""DOS knowledge for the bridge: decoding the stub's INT call log (DOS, DPMI,
BIOS and mouse functions with their arguments and results) and walking the
DOS memory control block (MCB) chain to find the programs in memory."""

import struct

# ---- INT log records (layout of gdbstub_intlog_t in src/gdbstub.c) ---------

RECORD_SIZE = 448
_NBYTES = 64
MODES = {0: "real", 1: "V86", 2: "PM16", 3: "PM32"}
PENDING, RETURNED, NO_RETURN = 0, 1, 2
REGS_IN = ("eax", "ebx", "ecx", "edx", "esi", "edi")
REGS_OUT = REGS_IN + ("eflags", "ds", "es")


class IntCall:
    """One logged INT call (or a run of identical consecutive ones)."""

    def __init__(self, raw):
        u = struct.unpack_from("<32I", raw)
        self.seq = u[0]
        self.vector = u[1] & 0xFF
        self.mode = (u[1] >> 8) & 0xFF
        self.depth = (u[1] >> 16) & 0xFF
        self.status = u[1] >> 24
        self.tsc = u[2] | (u[3] << 32)
        self.cs, self.eip, self.ss, self.esp, self.ds, self.es = u[4:10]
        self.inp = dict(zip(REGS_IN, u[10:16]))
        self.out = dict(zip(REGS_OUT, u[16:25]))
        self.lin_dx, self.lin_si, self.lin_di = u[25:28]
        lens, lens2 = u[28], u[29]
        self.cs_base = u[30]
        self.count = u[31]
        b = 128

        def chunk(i, n):
            return bytes(raw[b + i * _NBYTES:b + i * _NBYTES + n])

        self.data = {"dx": chunk(0, lens & 0xFF), "si": chunk(1, (lens >> 8) & 0xFF),
                     "di": chunk(2, (lens >> 16) & 0xFF), "dx_out": chunk(3, (lens >> 24) & 0xFF),
                     "di_out": chunk(4, lens2 & 0xFF)}

    @property
    def returned(self):
        return self.status == RETURNED

    @property
    def wide(self):
        return self.mode == 3

    def r(self, name, out=False):
        """Register value by name (eax/ax/ah/al...), at the call or on return."""
        regs = self.out if out else self.inp
        if name in regs:
            return regs[name]
        full = regs["e" + name[0] + ("x" if name[1] in "hl" else name[1])]
        if name[1] == "h":
            return (full >> 8) & 0xFF
        if name[1] == "l" and name[0] in "abcd":
            return full & 0xFF
        return full & 0xFFFF

    def cf(self):
        return bool(self.out["eflags"] & 1)

    def zf(self):
        return bool(self.out["eflags"] & 0x40)

    def off(self, name, out=False):
        """An offset register at its addressing width (EDX in 32-bit code, else DX)."""
        v = self.r("e" + name, out)
        return v if self.wide else v & 0xFFFF

    def caller(self):
        if self.mode in (0, 1):
            return "%04X:%04X" % (self.cs, self.eip)
        return "%04X:%08X" % (self.cs, self.eip) if self.wide else "%04X:%04X" % (self.cs, self.eip)

    def caller_linear(self):
        return (self.cs_base + self.eip) & 0xFFFFFFFF


def parse_log(raw):
    return [IntCall(raw[i:i + RECORD_SIZE]) for i in range(0, len(raw) - RECORD_SIZE + 1, RECORD_SIZE)]


def asciiz(data, limit=64):
    s = data.split(b"\0", 1)[0][:limit]
    return s.decode("cp437", "replace")


def quoted(data, limit=64):
    return '"%s"' % asciiz(data, limit)


def text_preview(data, n):
    """Readable preview of n bytes of data (a buffer read or written)."""
    d = data[:max(0, min(n, len(data)))]
    printable = sum(32 <= c < 127 or c in (9, 10, 13) for c in d)
    if d and printable >= len(d) * 0.8:
        s = d.decode("cp437", "replace").replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
        return '"%s"%s' % (s, "..." if n > len(d) else "")
    return " ".join("%02X" % c for c in d[:24]) + (" ..." if n > min(len(d), 24) else "")


# ---- DOS (INT 21h) ----------------------------------------------------------

DOS_ERRORS = {
    0x01: "invalid function", 0x02: "file not found", 0x03: "path not found", 0x04: "too many open files",
    0x05: "access denied", 0x06: "invalid handle", 0x07: "memory control blocks destroyed",
    0x08: "insufficient memory", 0x09: "invalid memory block", 0x0A: "invalid environment",
    0x0B: "invalid format", 0x0C: "invalid access code", 0x0D: "invalid data", 0x0F: "invalid drive",
    0x10: "can't remove current directory", 0x11: "not same device", 0x12: "no more files",
    0x13: "write protected", 0x14: "unknown unit", 0x15: "drive not ready", 0x17: "CRC error",
    0x19: "seek error", 0x1A: "unknown media", 0x1B: "sector not found", 0x1D: "write fault",
    0x1E: "read fault", 0x1F: "general failure", 0x20: "sharing violation", 0x21: "lock violation",
    0x50: "file exists", 0x52: "cannot make directory",
}

DOS_NAMES = {
    0x00: "terminate", 0x01: "read char with echo", 0x02: "write char", 0x03: "aux input", 0x04: "aux output",
    0x05: "printer output", 0x06: "direct console I/O", 0x07: "read char raw", 0x08: "read char",
    0x09: "print string", 0x0A: "buffered input", 0x0B: "input status", 0x0C: "flush input and read",
    0x0D: "disk reset", 0x0E: "select drive", 0x0F: "FCB open", 0x10: "FCB close", 0x11: "FCB find first",
    0x12: "FCB find next", 0x13: "FCB delete", 0x14: "FCB read", 0x15: "FCB write", 0x16: "FCB create",
    0x17: "FCB rename", 0x19: "get drive", 0x1A: "set DTA", 0x1B: "FAT info", 0x1C: "FAT info for drive",
    0x1F: "get default DPB", 0x21: "FCB random read", 0x22: "FCB random write", 0x23: "FCB file size",
    0x24: "FCB set record", 0x25: "set interrupt vector", 0x26: "create PSP", 0x27: "FCB block read",
    0x28: "FCB block write", 0x29: "parse filename", 0x2A: "get date", 0x2B: "set date", 0x2C: "get time",
    0x2D: "set time", 0x2E: "set verify", 0x2F: "get DTA", 0x30: "get DOS version",
    0x31: "terminate and stay resident", 0x32: "get DPB", 0x33: "Ctrl-Break / boot drive",
    0x34: "get InDOS address", 0x35: "get interrupt vector", 0x36: "get free disk space",
    0x37: "switch character", 0x38: "country info", 0x39: "make directory", 0x3A: "remove directory",
    0x3B: "change directory", 0x3C: "create file", 0x3D: "open file", 0x3E: "close file", 0x3F: "read file",
    0x40: "write file", 0x41: "delete file", 0x42: "seek", 0x43: "file attributes", 0x44: "IOCTL",
    0x45: "duplicate handle", 0x46: "force duplicate handle", 0x47: "get current directory",
    0x48: "allocate memory", 0x49: "free memory", 0x4A: "resize memory", 0x4B: "exec", 0x4C: "exit",
    0x4D: "get exit code", 0x4E: "find first", 0x4F: "find next", 0x50: "set PSP", 0x51: "get PSP",
    0x52: "get list of lists", 0x54: "get verify", 0x56: "rename", 0x57: "file date/time",
    0x58: "allocation strategy", 0x59: "extended error", 0x5A: "create temp file", 0x5B: "create new file",
    0x5C: "lock/unlock", 0x5D: "server function", 0x5E: "network", 0x5F: "redirection", 0x60: "truename",
    0x62: "get PSP", 0x63: "DBCS table", 0x65: "extended country info", 0x66: "code page",
    0x67: "set handle count", 0x68: "commit file", 0x69: "disk serial number", 0x6C: "extended open/create",
    0x71: "long filename function",
}

def _dos_uses_cf(ah):
    """Whether the function reports errors with CF set and the code in AX."""
    return ah >= 0x38 and ah not in (0x4C, 0x4D, 0x50, 0x51, 0x54, 0x62)


def _seg_off(c, seg_name, off_name, out=False):
    seg = c.out[seg_name] if out else (c.ds if seg_name == "ds" else c.es)
    off = c.off(off_name, out)
    return "%04X:%0*X" % (seg & 0xFFFF, 8 if c.wide else 4, off)


def _dos(c):
    ah, al = c.r("ah"), c.r("al")
    name = DOS_NAMES.get(ah, "function %02Xh" % ah)
    args, res = "", None
    o = lambda r: c.r(r, out=True)
    if ah == 0x02:
        ch = c.r("dl")
        args = repr(chr(ch)) if 32 <= ch < 127 else "%02Xh" % ch
    elif ah == 0x06:
        args = "read" if c.r("dl") == 0xFF else "write %02Xh" % c.r("dl")
    elif ah == 0x09:
        s = c.data["dx"].split(b"$", 1)[0]
        args = text_preview(s, len(s))
    elif ah in (0x01, 0x07, 0x08):
        res = lambda: "char %02Xh" % o("al")
    elif ah == 0x0B:
        res = lambda: "key waiting" if o("al") else "no key"
    elif ah == 0x0E:
        args = "drive %s:" % chr(65 + c.r("dl")) if c.r("dl") < 26 else "drive %d" % c.r("dl")
    elif ah == 0x19:
        res = lambda: "drive %s:" % chr(65 + (o("al") & 31))
    elif ah == 0x1A:
        args = "DTA " + _seg_off(c, "ds", "dx")
    elif ah == 0x25:
        args = "INT %02Xh -> %s" % (al, _seg_off(c, "ds", "dx"))
    elif ah == 0x35:
        args = "INT %02Xh" % al
        res = lambda: "%04X:%0*X" % (c.out["es"] & 0xFFFF, 8 if c.wide else 4, c.off("bx", True))
    elif ah == 0x30:
        res = lambda: "DOS %d.%02d" % (o("al"), o("ah"))
    elif ah == 0x2C:
        res = lambda: "%02d:%02d:%02d.%02d" % (o("ch"), o("cl"), o("dh"), o("dl"))
    elif ah == 0x2A:
        res = lambda: "%04d-%02d-%02d" % (o("cx"), o("dh"), o("dl"))
    elif ah in (0x39, 0x3A, 0x3B, 0x41, 0x4E):
        args = quoted(c.data["dx"])
        if ah == 0x4E:
            args += " attr %02Xh" % c.r("cl")
    elif ah == 0x3C or ah == 0x5B:
        args = "%s attr %02Xh" % (quoted(c.data["dx"]), c.r("cl"))
        res = lambda: "handle %d" % o("ax")
    elif ah == 0x3D:
        args = "%s mode %02Xh (%s)" % (quoted(c.data["dx"]), al, ("read", "write", "read/write", "?")[al & 3])
        res = lambda: "handle %d" % o("ax")
    elif ah == 0x3E:
        args = "handle %d" % c.r("bx")
    elif ah == 0x3F:
        args = "handle %d, %d bytes to %s" % (c.r("bx"), c.r("ecx") if c.wide else c.r("cx"), _seg_off(c, "ds", "dx"))
        res = lambda: "%d bytes: %s" % (c.r("eax", True) if c.wide else o("ax"),
                                         text_preview(c.data["dx_out"], c.r("eax", True) if c.wide else o("ax")))
    elif ah == 0x40:
        n = c.r("ecx") if c.wide else c.r("cx")
        args = "handle %d, %d bytes: %s" % (c.r("bx"), n, text_preview(c.data["dx"], n))
        res = lambda: "%d written" % (c.r("eax", True) if c.wide else o("ax"))
    elif ah == 0x42:
        args = "handle %d, %s %+d" % (c.r("bx"), ("from start", "from current", "from end", "?")[al & 3],
                                       struct.unpack("<i", struct.pack("<I", (c.r("cx") << 16) | c.r("dx")))[0])
        res = lambda: "position %d" % ((o("dx") << 16) | o("ax"))
    elif ah == 0x43:
        args = "%s %s" % (quoted(c.data["dx"]), "get" if al == 0 else "set %02Xh" % c.r("cl"))
        res = lambda: "attr %02Xh" % o("cl")
    elif ah == 0x44:
        args = "AL=%02Xh handle %d" % (al, c.r("bx"))
        res = lambda: "DX=%04X AX=%04X" % (o("dx"), o("ax"))
    elif ah == 0x47:
        args = "drive %d" % c.r("dl")
    elif ah == 0x48:
        args = "%d paragraphs (%d bytes)" % (c.r("bx"), c.r("bx") * 16)
        res = lambda: "segment %04X" % o("ax")
    elif ah == 0x49:
        args = "segment %04X" % (c.es & 0xFFFF)
    elif ah == 0x4A:
        args = "segment %04X to %d paragraphs" % (c.es & 0xFFFF, c.r("bx"))
    elif ah == 0x4B:
        args = "%s %s" % (("load and run", "load", "?", "overlay")[al & 3] if al <= 3 else "AL=%02Xh" % al,
                          quoted(c.data["dx"]))
        res = lambda: "child exited"
    elif ah == 0x4C:
        args = "exit code %d" % al
    elif ah == 0x31:
        args = "exit code %d, keep %d paragraphs" % (al, c.r("dx"))
    elif ah == 0x4D:
        res = lambda: "exit code %d, type %d" % (o("al"), o("ah"))
    elif ah in (0x51, 0x62):
        res = lambda: "PSP %04X" % o("bx")
    elif ah == 0x50:
        args = "PSP %04X" % c.r("bx")
    elif ah == 0x56:
        args = "%s -> %s" % (quoted(c.data["dx"]), quoted(c.data["di"]))
    elif ah == 0x57:
        args = "handle %d %s" % (c.r("bx"), "get" if al == 0 else "set")
    elif ah == 0x6C:
        args = "%s mode %04Xh action %04Xh" % (quoted(c.data["si"]), c.r("bx"), c.r("dx"))
        res = lambda: "handle %d (action %d)" % (o("ax"), o("cx"))
    elif ah == 0x71:
        name = "long filename function %02Xh" % al
        if al in (0x39, 0x3A, 0x3B, 0x41, 0x43, 0x4E, 0x56):
            args = quoted(c.data["dx"])
        elif al == 0x6C:
            args = quoted(c.data["si"])
        elif al == 0x60:
            args = quoted(c.data["si"])
    elif ah == 0xFF:
        name = "extender/TSR private function"
        args = "AX=%04X" % c.r("ax")
    else:
        args = "AL=%02Xh" % al if al else ""
    return name, args, res, _dos_uses_cf(ah)


def _dos_error(c):
    code = c.r("ax", True)
    if c.r("ah") == 0x71 and code == 0x7100:
        return "not supported (no long filename support)"
    return "error %02Xh (%s)" % (code, DOS_ERRORS.get(code, "unknown"))


# ---- DPMI (INT 31h) -----------------------------------------------------------

DPMI_NAMES = {
    0x0000: "allocate LDT descriptors", 0x0001: "free descriptor", 0x0002: "segment to descriptor",
    0x0003: "get selector increment", 0x0006: "get segment base", 0x0007: "set segment base",
    0x0008: "set segment limit", 0x0009: "set access rights", 0x000A: "create alias descriptor",
    0x000B: "get descriptor", 0x000C: "set descriptor", 0x000D: "allocate specific descriptor",
    0x0100: "allocate DOS memory", 0x0101: "free DOS memory", 0x0102: "resize DOS memory",
    0x0200: "get real-mode interrupt vector", 0x0201: "set real-mode interrupt vector",
    0x0202: "get exception handler", 0x0203: "set exception handler",
    0x0204: "get protected-mode interrupt vector", 0x0205: "set protected-mode interrupt vector",
    0x0300: "simulate real-mode interrupt", 0x0301: "call real-mode far procedure",
    0x0302: "call real-mode IRET procedure", 0x0303: "allocate real-mode callback",
    0x0304: "free real-mode callback", 0x0305: "get state save addresses", 0x0306: "get raw mode switch addresses",
    0x0400: "get DPMI version", 0x0500: "get free memory info", 0x0501: "allocate memory block",
    0x0502: "free memory block", 0x0503: "resize memory block", 0x0600: "lock linear region",
    0x0601: "unlock linear region", 0x0602: "mark real-mode region pageable", 0x0603: "relock real-mode region",
    0x0604: "get page size", 0x0702: "mark page as paging candidate", 0x0703: "discard page contents",
    0x0800: "map physical address", 0x0801: "free physical address mapping", 0x0900: "disable virtual interrupts",
    0x0901: "enable virtual interrupts", 0x0902: "get virtual interrupt state", 0x0A00: "get vendor API entry",
    0x0B00: "set debug watchpoint", 0x0B01: "clear debug watchpoint", 0x0B02: "get watchpoint state",
    0x0B03: "reset watchpoint",
}


def _rm_regs(data):
    """Decode a DPMI real-mode call structure (50 bytes)."""
    if len(data) < 0x32:
        return None
    edi, esi, ebp, _, ebx, edx, ecx, eax = struct.unpack_from("<8I", data, 0)
    flags, es, ds, fs, gs, ip, cs, sp, ss = struct.unpack_from("<9H", data, 0x20)
    return {"eax": eax, "ebx": ebx, "ecx": ecx, "edx": edx, "esi": esi, "edi": edi, "flags": flags,
            "es": es, "ds": ds, "cs": cs, "ip": ip}


def _dpmi(c):
    ax = c.r("ax")
    name = DPMI_NAMES.get(ax, "function %04Xh" % ax)
    args, res = "", None
    o = lambda r: c.r(r, out=True)
    cxdx = lambda out=False: (c.r("cx", out) << 16) | c.r("dx", out)
    bxcx = lambda out=False: (c.r("bx", out) << 16) | c.r("cx", out)
    if ax == 0x0000:
        args = "%d" % c.r("cx")
        res = lambda: "first selector %04X" % o("ax")
    elif ax == 0x0003:
        res = lambda: "increment %04Xh" % o("ax")
    elif ax in (0x0001, 0x000A, 0x000B, 0x000C):
        args = "selector %04X" % c.r("bx")
        if ax == 0x000A:
            res = lambda: "alias %04X" % o("ax")
    elif ax == 0x0002:
        args = "segment %04X" % c.r("bx")
        res = lambda: "selector %04X" % o("ax")
    elif ax == 0x0006:
        args = "selector %04X" % c.r("bx")
        res = lambda: "base %08X" % cxdx(True)
    elif ax == 0x0007:
        args = "selector %04X base %08X" % (c.r("bx"), cxdx())
    elif ax == 0x0008:
        args = "selector %04X limit %08X" % (c.r("bx"), cxdx())
    elif ax == 0x0009:
        args = "selector %04X access %02X flags %02X" % (c.r("bx"), c.r("cl"), c.r("ch"))
    elif ax == 0x0100:
        args = "%d paragraphs" % c.r("bx")
        res = lambda: "segment %04X selector %04X" % (o("ax"), o("dx"))
    elif ax == 0x0101:
        args = "selector %04X" % c.r("dx")
    elif ax in (0x0200, 0x0202, 0x0204):
        args = "%s %02Xh" % ("exception" if ax == 0x0202 else "INT", c.r("bl"))
        res = (lambda: "%04X:%04X" % (o("cx"), o("dx"))) if ax == 0x0200 else \
              (lambda: "%04X:%0*X" % (o("cx"), 8 if c.wide else 4, c.off("dx", True)))
    elif ax == 0x0201:
        args = "INT %02Xh -> %04X:%04X" % (c.r("bl"), c.r("cx"), c.r("dx"))
    elif ax in (0x0203, 0x0205):
        args = "%s %02Xh -> %04X:%08X" % ("exception" if ax == 0x0203 else "INT", c.r("bl"), c.r("cx"),
                                          c.r("edx") if c.wide else c.r("dx"))
    elif ax in (0x0300, 0x0301, 0x0302):
        rm = _rm_regs(c.data["di"])
        if ax == 0x0300:
            args = "INT %02Xh" % c.r("bl")
        else:
            args = "%04X:%04X" % (rm["cs"], rm["ip"]) if rm else ""
        if rm:
            what = ""
            if ax == 0x0300:
                what = describe_call_regs(c.r("bl"), rm)
            args += " with AX=%04X BX=%04X CX=%04X DX=%04X DS=%04X ES=%04X%s" % (
                rm["eax"] & 0xFFFF, rm["ebx"] & 0xFFFF, rm["ecx"] & 0xFFFF, rm["edx"] & 0xFFFF, rm["ds"], rm["es"],
                (" [" + what + "]") if what else "")

        def rmres():
            r = _rm_regs(c.data["di_out"])
            if not r:
                return "done"
            return "real-mode AX=%04X BX=%04X CX=%04X DX=%04X CF=%d" % (
                r["eax"] & 0xFFFF, r["ebx"] & 0xFFFF, r["ecx"] & 0xFFFF, r["edx"] & 0xFFFF, r["flags"] & 1)
        res = rmres
    elif ax == 0x0303:
        args = "for %04X:%08X" % (c.ds & 0xFFFF, c.off("si"))
        res = lambda: "callback %04X:%04X" % (o("cx"), o("dx"))
    elif ax == 0x0400:
        res = lambda: "DPMI %d.%02d" % (o("ah"), o("al"))
    elif ax == 0x0501:
        args = "%d bytes" % bxcx()
        res = lambda: "linear %08X, handle %08X" % (bxcx(True), (o("si") << 16) | o("di"))
    elif ax == 0x0502:
        args = "handle %08X" % ((c.r("si") << 16) | c.r("di"))
    elif ax == 0x0503:
        args = "handle %08X to %d bytes" % ((c.r("si") << 16) | c.r("di"), bxcx())
        res = lambda: "linear %08X" % bxcx(True)
    elif ax in (0x0600, 0x0601):
        args = "linear %08X, %d bytes" % (bxcx(), (c.r("si") << 16) | c.r("di"))
    elif ax == 0x0800:
        args = "physical %08X, %d bytes" % (bxcx(), (c.r("si") << 16) | c.r("di"))
        res = lambda: "linear %08X" % bxcx(True)
    return name, args, res, True


def _dpmi_error(c):
    return "failed (AX=%04X)" % c.r("ax", True)


# ---- BIOS, mouse and others ---------------------------------------------------

VIDEO_NAMES = {
    0x00: "set video mode", 0x01: "set cursor shape", 0x02: "set cursor position", 0x03: "get cursor",
    0x05: "set display page", 0x06: "scroll up", 0x07: "scroll down", 0x08: "read char/attr",
    0x09: "write char/attr", 0x0A: "write char", 0x0B: "set palette", 0x0C: "write pixel", 0x0D: "read pixel",
    0x0E: "teletype output", 0x0F: "get video mode", 0x10: "palette registers", 0x11: "character generator",
    0x12: "alternate select", 0x13: "write string", 0x1A: "display combination", 0x1B: "functionality info",
    0x1C: "save/restore state", 0x4F: "VESA",
}
VESA_NAMES = {0x00: "controller info", 0x01: "mode info", 0x02: "set mode", 0x03: "get mode", 0x04: "save/restore",
              0x05: "bank switch", 0x06: "scan line length", 0x07: "display start", 0x08: "DAC width",
              0x09: "palette", 0x0A: "protected-mode interface"}
KEYBOARD_NAMES = {0x00: "read key", 0x01: "check key", 0x02: "shift flags", 0x03: "typematic rate",
                  0x05: "store key", 0x10: "read key (enhanced)", 0x11: "check key (enhanced)",
                  0x12: "shift flags (enhanced)"}
MOUSE_NAMES = {
    0x00: "reset", 0x01: "show cursor", 0x02: "hide cursor", 0x03: "get position and buttons",
    0x04: "set position", 0x05: "button press data", 0x06: "button release data", 0x07: "set X range",
    0x08: "set Y range", 0x09: "graphics cursor shape", 0x0A: "text cursor", 0x0B: "motion counters",
    0x0C: "set event handler", 0x0F: "mickey/pixel ratio", 0x10: "exclusion area", 0x13: "double-speed threshold",
    0x14: "swap event handler", 0x15: "state size", 0x16: "save state", 0x17: "restore state",
    0x1A: "set sensitivity", 0x1B: "get sensitivity", 0x1D: "set display page", 0x21: "software reset",
    0x24: "version", 0x26: "max coordinates",
}
DISK_NAMES = {0x00: "reset", 0x01: "status", 0x02: "read sectors", 0x03: "write sectors", 0x04: "verify",
              0x08: "drive parameters", 0x15: "drive type", 0x16: "change status", 0x41: "extensions check",
              0x42: "extended read", 0x43: "extended write", 0x48: "extended parameters"}
CLOCK_NAMES = {0x00: "get tick count", 0x01: "set tick count", 0x02: "get RTC time", 0x03: "set RTC time",
               0x04: "get RTC date", 0x05: "set RTC date"}
SYSTEM_NAMES = {0x4F: "keyboard intercept", 0x86: "wait", 0x87: "move extended memory", 0x88: "extended memory size",
                0x90: "device busy", 0x91: "interrupt complete", 0xC0: "configuration", 0xE8: "memory map",
                0x24: "A20 gate", 0x53: "APM", 0xC2: "PS/2 mouse"}
MULTIPLEX_NAMES = {0x1600: "Windows check", 0x1680: "release time slice", 0x1687: "DPMI host check",
                   0x4300: "XMS check", 0x4310: "XMS entry", 0x1A00: "ANSI check", 0x1100: "network check",
                   0xAE00: "command line hook", 0xAE01: "command execute", 0x4A01: "HMA query",
                   0x4A02: "HMA allocate", 0x1605: "Windows startup"}
EMS_NAMES = {0x40: "status", 0x41: "page frame", 0x42: "page counts", 0x43: "allocate pages", 0x44: "map page",
             0x45: "free pages", 0x46: "version", 0x47: "save map", 0x48: "restore map", 0x4B: "handle count",
             0x4C: "handle pages", 0x50: "map multiple", 0x58: "mappable pages", 0xDE: "VCPI"}


def _video(c):
    ah, al = c.r("ah"), c.r("al")
    name = VIDEO_NAMES.get(ah, "function %02Xh" % ah)
    args, res = "", None
    o = lambda r: c.r(r, out=True)
    if ah == 0x00:
        args = "mode %02Xh" % al
    elif ah == 0x02:
        args = "row %d col %d page %d" % (c.r("dh"), c.r("dl"), c.r("bh"))
    elif ah == 0x0E:
        args = repr(chr(al)) if 32 <= al < 127 else "%02Xh" % al
    elif ah in (0x09, 0x0A):
        args = "%s x%d attr %02Xh" % (repr(chr(al)) if 32 <= al < 127 else "%02Xh" % al, c.r("cx"), c.r("bl"))
    elif ah == 0x0C:
        args = "(%d,%d) colour %d" % (c.r("cx"), c.r("dx"), al)
    elif ah == 0x0F:
        res = lambda: "mode %02Xh, %d columns, page %d" % (o("al"), o("ah"), o("bh"))
    elif ah == 0x10:
        args = "AL=%02Xh" % al
    elif ah == 0x4F:
        name = "VESA " + VESA_NAMES.get(al, "%02Xh" % al)
        if al == 0x01:
            args = "mode %04Xh" % c.r("cx")
        elif al == 0x02:
            args = "mode %04Xh" % c.r("bx")
        elif al == 0x05:
            args = "window %d bank %d" % (c.r("bl"), c.r("dx"))
        res = lambda: "status %04X" % o("ax")
    return name, args, res, False


def _keyboard(c):
    ah = c.r("ah")
    name = KEYBOARD_NAMES.get(ah, "function %02Xh" % ah)
    res = None
    if ah in (0x00, 0x10):
        res = lambda: "key %04X" % c.r("ax", True)
    elif ah in (0x01, 0x11):
        res = lambda: "no key" if c.zf() else "key %04X waiting" % c.r("ax", True)
    elif ah in (0x02, 0x12):
        res = lambda: "flags %02X" % c.r("al", True)
    return name, "", res, False


def _mouse(c):
    ax = c.r("ax")
    name = MOUSE_NAMES.get(ax, "function %04Xh" % ax)
    args, res = "", None
    o = lambda r: c.r(r, out=True)
    if ax == 0x00:
        res = lambda: "%s, %d buttons" % ("installed" if o("ax") == 0xFFFF else "not installed",
                                          2 if o("bx") == 0xFFFF else o("bx"))
    elif ax == 0x03:
        res = lambda: "x=%d y=%d buttons=%d" % (o("cx"), o("dx"), o("bx"))
    elif ax == 0x04:
        args = "x=%d y=%d" % (c.r("cx"), c.r("dx"))
    elif ax in (0x07, 0x08):
        args = "%d..%d" % (c.r("cx"), c.r("dx"))
    elif ax in (0x05, 0x06):
        args = "button %d" % c.r("bx")
        res = lambda: "count %d at x=%d y=%d, buttons=%d" % (o("bx"), o("cx"), o("dx"), o("ax"))
    elif ax == 0x0B:
        res = lambda: "dx=%d dy=%d" % (struct.unpack("<h", struct.pack("<H", o("cx")))[0],
                                       struct.unpack("<h", struct.pack("<H", o("dx")))[0])
    elif ax in (0x0C, 0x14):
        args = "mask %04Xh -> %04X:%0*X" % (c.r("cx"), c.es & 0xFFFF, 8 if c.wide else 4, c.off("dx"))
    return name, args, res, False


def _disk(c):
    ah = c.r("ah")
    name = DISK_NAMES.get(ah, "function %02Xh" % ah)
    args = ""
    if ah in (0x02, 0x03, 0x04):
        cyl = c.r("ch") | ((c.r("cl") & 0xC0) << 2)
        args = "%d sectors, drive %02Xh C/H/S %d/%d/%d to %04X:%04X" % (
            c.r("al"), c.r("dl"), cyl, c.r("dh"), c.r("cl") & 0x3F, c.es & 0xFFFF, c.r("bx"))
    elif ah in (0x00, 0x08, 0x15, 0x41, 0x42, 0x43, 0x48):
        args = "drive %02Xh" % c.r("dl")
    if ah == 0x15:
        res = lambda: "type %d (%s)" % (c.r("ah", True), {0: "none", 1: "floppy, no change line", 2: "floppy with change line",
                                                         3: "hard disk"}.get(c.r("ah", True), "?"))
    elif ah == 0x41:
        res = lambda: "extensions %d.%d" % (c.r("ah", True) >> 4, c.r("ah", True) & 15) if c.r("bx", True) == 0xAA55 \
            else "no extensions"
    else:
        res = lambda: "status %02Xh" % c.r("ah", True)
    return name, args, res, True


def _byname(table, key_reg):
    def f(c):
        k = c.r(key_reg)
        return table.get(k, "function %0*Xh" % (4 if key_reg == "ax" else 2, k)), "", None, False
    return f


VECTOR_INFO = {
    0x10: ("video", _video), 0x13: ("disk", _disk), 0x15: ("system", _byname(SYSTEM_NAMES, "ah")),
    0x16: ("keyboard", _keyboard), 0x1A: ("clock", _byname(CLOCK_NAMES, "ah")), 0x21: ("DOS", _dos),
    0x2F: ("multiplex", _byname(MULTIPLEX_NAMES, "ax")), 0x31: ("DPMI", _dpmi), 0x33: ("mouse", _mouse),
    0x67: ("EMS", _byname(EMS_NAMES, "ah")),
}
SIMPLE_VECTORS = {0x20: "terminate program", 0x28: "DOS idle", 0x05: "print screen", 0x11: "equipment list",
                  0x12: "memory size", 0x14: "serial port", 0x17: "printer", 0x19: "bootstrap",
                  0x1B: "Ctrl-Break", 0x1C: "timer tick", 0x23: "Ctrl-C", 0x24: "critical error",
                  0x25: "absolute disk read", 0x26: "absolute disk write", 0x27: "terminate and stay resident"}


class _Regs:
    """Adapter so describe_call_regs can reuse the decoders on a register dict."""

    def __init__(self, regs):
        self.inp = {k: regs.get(k, 0) for k in REGS_IN}
        self.mode = 0
        self.data = {"dx": b"", "si": b"", "di": b"", "dx_out": b"", "di_out": b""}
        self.ds = regs.get("ds", 0)
        self.es = regs.get("es", 0)

    r = IntCall.r
    off = IntCall.off
    wide = False


def describe_call_regs(vector, regs):
    """Short name of an INT call from registers alone (for nested simulated calls)."""
    info = VECTOR_INFO.get(vector)
    if not info:
        return SIMPLE_VECTORS.get(vector, "")
    try:
        name, args, _, _ = info[1](_Regs(regs))
    except Exception:
        return info[0]
    return "%s %s" % (info[0], name)


def describe(c):
    """(function description, result description) for a logged call."""
    info = VECTOR_INFO.get(c.vector)
    if info:
        area, fn = info
        try:
            name, args, res, uses_cf = fn(c)
        except Exception as e:  # keep the log readable if one decoder trips
            name, args, res, uses_cf = "(decode error: %s)" % e, "", None, False
        what = "%s: %s" % (area, name) + (" " + args if args else "")
    else:
        what = SIMPLE_VECTORS.get(c.vector, "") or "AX=%04X" % c.r("ax")
        res, uses_cf = None, False
    if c.status == PENDING:
        result = "(still running)"
    elif c.status == NO_RETURN:
        result = "(never returned)"
    elif uses_cf and c.cf():
        result = _dpmi_error(c) if c.vector == 0x31 else (_dos_error(c) if c.vector == 0x21 else
                                                            "failed (CF set, AX=%04X)" % c.r("ax", True))
    elif res is not None:
        try:
            result = res()
        except Exception as e:
            result = "(decode error: %s)" % e
    else:
        result = "ok"
    return what, result


def is_polling(c):
    """Calls that only poll (no key waiting, idle and time-slice calls), hidden by default."""
    v = c.vector
    if v == 0x16 and c.r("ah") in (0x01, 0x11) and (not c.returned or c.zf()):
        return True
    if v == 0x21 and c.r("ah") == 0x0B and c.returned and c.r("al", True) == 0:
        return True
    if v == 0x21 and c.r("ah") == 0x06 and c.r("dl") == 0xFF and c.returned and c.zf():
        return True
    if v == 0x28 or (v == 0x2F and c.r("ax") == 0x1680) or (v == 0x15 and c.r("ah") in (0x90, 0x91, 0x4F)):
        return True
    return False


def format_call(c, t0=None, hz=0):
    what, result = describe(c)
    when = ""
    if t0 is not None and hz:
        when = "%9.3fs " % ((c.tsc - t0) / hz)
    rep = " (x%d)" % c.count if c.count > 1 else ""
    mode = "" if c.mode == 0 else " [%s]" % MODES.get(c.mode, "?")
    return "#%-6d %s%s INT %02Xh %s%s -> %s  (from %s%s)" % (
        c.seq, when, "  " * min(c.depth, 8), c.vector, what, rep, result, c.caller(), mode)


def parse_filter(text):
    """'21 31:0501 10:00 16' -> {vector: None or set of function numbers}.
    Function numbers are AH, or AX for INT 31h, 33h and 2Fh."""
    out = {}
    for item in text.replace(",", " ").split():
        if ":" in item:
            v, fns = item.split(":", 1)
            out.setdefault(int(v, 16), set())
            if out[int(v, 16)] is not None:
                out[int(v, 16)].update(int(f, 16) for f in fns.split("/") if f)
        else:
            out[int(item, 16)] = None
    return out


def function_number(c):
    return c.r("ax") if c.vector in (0x31, 0x33, 0x2F) else c.r("ah")


def matches_filter(c, flt):
    if not flt:
        return True
    if c.vector not in flt:
        return False
    fns = flt[c.vector]
    return fns is None or function_number(c) in fns


# ---- DOS memory: MCB chain and PSPs ------------------------------------------

class Block:
    def __init__(self, mcb, sig, owner, size, name):
        self.mcb, self.sig, self.owner, self.size, self.name = mcb, sig, owner, size, name
        self.kind = ""
        self.program = ""  # owner's name

    @property
    def start(self):
        return self.mcb + 1

    @property
    def end(self):
        return self.mcb + 1 + self.size

    def contains_linear(self, linear):
        return self.start * 16 <= linear < self.end * 16


class Program:
    def __init__(self, psp):
        self.psp = psp
        self.name = ""
        self.parent = 0
        self.env = 0
        self.path = ""
        self.args = ""
        self.blocks = []

    @property
    def load_segment(self):
        return self.psp + 0x10


def _walk(mem, seg, limit=0x10000):
    """Follow an MCB chain from seg in a 1 MiB image; None if it isn't one."""
    blocks = []
    while len(blocks) < 4096:
        if seg * 16 + 16 > len(mem):
            return None
        sig = mem[seg * 16]
        if sig not in (0x4D, 0x5A):
            return None
        owner, size = struct.unpack_from("<HH", mem, seg * 16 + 1)
        name = mem[seg * 16 + 8:seg * 16 + 16].split(b"\0", 1)[0]
        blocks.append(Block(seg, chr(sig), owner, size, name.decode("cp437", "replace")))
        nxt = seg + 1 + size
        if sig == 0x5A:
            return blocks
        if nxt <= seg or nxt >= limit:
            return None
        seg = nxt


def find_mcb_chain(mem):
    """Find the DOS MCB chain in a 1 MiB memory image (bytes, zeros where
    unreadable): the lowest chain of 'M' blocks ending in a 'Z' that reaches
    the top of conventional memory (BIOS 0040:0013), preferring one whose
    first block belongs to DOS (owner 0008)."""
    top = struct.unpack_from("<H", mem, 0x413)[0] * 64
    fallback = None
    for seg in range(0x40, min(top, 0x9000)):
        if mem[seg * 16] != 0x4D:
            continue
        chain = _walk(mem, seg)
        if not chain or len(chain) < 3:
            continue
        # The chain covers conventional memory up to its top; with DOS=UMB and the
        # UMB link off, the last block ends at top-1, where the link MCB sits.
        if not any(b.mcb == top - 1 or b.end in (top, top - 1) for b in chain):
            continue
        if chain[0].owner == 8:
            return chain, top
        if fallback is None:
            fallback = chain
    return fallback, top


def analyze(mem, chain):
    """Label blocks and collect the programs (PSPs) they belong to."""
    progs = {}
    for b in chain:
        if b.owner == 0:
            b.kind = "free"
        elif b.owner in (6, 7, 8):
            b.kind = {6: "UMB (DOS)", 7: "excluded (UMB)", 8: "DOS"}[b.owner]
            if b.name in ("SC", "SD"):
                b.kind += " " + {"SC": "system code", "SD": "system data"}[b.name]
        else:
            progs.setdefault(b.owner, Program(b.owner)).blocks.append(b)
    for p in progs.values():
        base = p.psp * 16
        if base + 0x100 <= len(mem) and mem[base:base + 2] == b"\xCD\x20":
            p.parent, = struct.unpack_from("<H", mem, base + 0x16)
            p.env, = struct.unpack_from("<H", mem, base + 0x2C)
            n = mem[base + 0x80]
            p.args = mem[base + 0x81:base + 0x81 + min(n, 126)].decode("cp437", "replace").strip()
            # Under DPMI, PSP:2Ch becomes a selector; only trust a segment that
            # is one of this program's blocks.
            if not any(b.start == p.env for b in p.blocks):
                p.env = 0
            if p.env and p.env * 16 + 32 < len(mem):
                env = mem[p.env * 16:p.env * 16 + 32768]
                end = env.find(b"\0\0")
                if end >= 0 and env[end + 2:end + 4] == b"\x01\x00":
                    p.path = env[end + 4:].split(b"\0", 1)[0].decode("cp437", "replace")
        for b in p.blocks:
            if b.start == p.psp:
                b.kind = "program"
                # MCB names exist since DOS 4; before that the field holds leftovers.
                if b.name and all(32 < ord(ch) < 127 for ch in b.name):
                    p.name = b.name
            elif b.start == p.env:
                b.kind = "environment"
            else:
                b.kind = "data"
        if not p.name and p.path:
            p.name = p.path.replace("/", "\\").rsplit("\\", 1)[-1].split(".")[0]
        for b in p.blocks:
            b.program = p.name or "PSP %04X" % p.psp
    return progs


def current_psp(mem, chain, progs):
    """DOS's current PSP, from its swappable data area: the DOS data segment
    holds the first MCB's segment at offset 24h (just before the list of
    lists, MS-DOS and FreeDOS alike) and the current PSP at 330h (SDA+10h).
    Returns (psp, DOS data segment) or (None, None)."""
    if not chain:
        return None, None
    first = chain[0].mcb
    found = set()
    for seg in range(0x40, first):
        a = seg * 16
        if struct.unpack_from("<H", mem, a + 0x24)[0] != first or mem[a + 0x321] > 8:
            continue
        psp = struct.unpack_from("<H", mem, a + 0x330)[0]
        if psp in progs:
            found.add((psp, seg))
    return found.pop() if len(found) == 1 else (None, None)


def program_at(progs, linear):
    for p in progs.values():
        for b in p.blocks:
            if b.contains_linear(linear):
                return p
    return None


def newest_program(progs):
    """The program block highest in memory among programs that aren't the
    parent of another one: usually the one most recently started."""
    parents = {p.parent for p in progs.values() if p.parent != p.psp}
    leaves = [p for p in progs.values() if p.psp not in parents and any(b.kind == "program" for b in p.blocks)]
    return max(leaves, key=lambda p: p.psp) if leaves else None


def format_size(paras):
    n = paras * 16
    return "%d" % n if n < 10240 else "%dK" % (n // 1024)


def call_from_regs(vector, regs, mode, data):
    """An IntCall for a call about to be made, from live registers and the
    bytes at DS:(E)DX, DS:(E)SI and ES:(E)DI (data: dict dx/si/di)."""
    raw = bytearray(RECORD_SIZE)
    seg = regs.get("seg") or {}
    u = [0] * 32
    u[1] = (vector & 0xFF) | (mode << 8)
    u[4], u[5], u[6], u[7], u[8], u[9] = (regs["cs"], regs["eip_off"] if "eip_off" in regs else regs["eip"],
                                          regs["ss"], regs["esp"], regs["ds"], regs["es"])
    u[10:16] = [regs[r] for r in REGS_IN]
    lens = 0
    for i, k in enumerate(("dx", "si", "di")):
        d = data.get(k, b"")[:_NBYTES]
        raw[128 + i * _NBYTES:128 + i * _NBYTES + len(d)] = d
        lens |= len(d) << (8 * i)
    u[28] = lens
    u[30] = seg.get("cs", {}).get("base", regs["cs"] * 16)
    u[31] = 1
    struct.pack_into("<32I", raw, 0, *u)
    return IntCall(raw)
