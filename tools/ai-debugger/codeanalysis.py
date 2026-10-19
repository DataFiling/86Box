"""Static helpers over a program's code bytes: who references an address,
where functions start (from call targets), and encoding simple patches."""

import re
import struct

import dos


def call_targets(data, base, bits, lo=None, hi=None):
    """Targets of direct near calls (E8 rel16/rel32) found anywhere in data
    (at base), limited to [lo, hi) (default: the data itself). Unaligned byte
    matches are kept only if the target is inside the range, which filters
    out most false hits."""
    lo = base if lo is None else lo
    hi = base + len(data) if hi is None else hi
    size = 4 if bits == 32 else 2
    targets = {}
    for m in re.finditer(b"\xE8", data):
        i = m.start()
        if i + 1 + size > len(data):
            break
        rel = struct.unpack_from("<i" if size == 4 else "<h", data, i + 1)[0]
        end = i + 1 + size
        if size == 4:
            t = (base + end + rel) & 0xFFFFFFFF
        else:  # 16-bit: offsets wrap within the code segment, which starts at base
            t = base + ((end + rel) & 0xFFFF)
        if lo <= t < hi:
            targets[t] = targets.get(t, 0) + 1
    return targets


def function_start(targets, linear, max_distance=0x4000):
    """The nearest call target at or before linear (a likely function entry)."""
    best = None
    for t in targets:
        if t <= linear and linear - t <= max_distance and (best is None or t > best):
            best = t
    return best


def _decode_at(data, base, bits, pos, detail=False):
    """The instruction starting at data[pos] (capstone), or None."""
    if dos.capstone is None:
        return None
    md = dos.capstone.Cs(dos.capstone.CS_ARCH_X86, dos.capstone.CS_MODE_16 if bits == 16 else dos.capstone.CS_MODE_32)
    md.detail = detail
    return next(md.disasm(data[pos:pos + 15], base + pos), None)


def _operand_at(insn, start, pos, value):
    """Whether the instruction (starting at data offset start) encodes `value`
    as its displacement or immediate exactly at data offset pos."""
    try:
        if insn.disp_offset and start + insn.disp_offset == pos and (insn.disp & 0xFFFFFFFF) == value:
            return True
        if insn.imm_offset and start + insn.imm_offset == pos:
            return any(op.type == dos.capstone.x86.X86_OP_IMM and (op.imm & 0xFFFFFFFF) == value
                       for op in insn.operands)
    except (AttributeError, dos.capstone.CsError):
        return False
    return False


def references(data, base, bits, target, max_results=50, offset16=None):
    """Instructions in data (at linear base) that refer to the linear target:
    32-bit absolute operands, and near call/jmp/jcc whose destination is the
    target. In 16-bit code, offset16 (the target's offset in its segment,
    usually DS) also finds [disp16] memory operands and imm16 values equal to
    it, the way 16-bit code passes near pointers (mov ax,ofs / push ofs).
    Returns [(linear, kind, insn text)]."""
    found = []
    seen = set()
    if bits == 16 and offset16 is not None:
        pat = struct.pack("<H", offset16 & 0xFFFF)
        for m in re.finditer(re.escape(pat), data):
            p = m.start()
            for back in range(5, 0, -1):
                s = p - back
                if s < 0:
                    continue
                insn = _decode_at(data, base, bits, s, detail=True)
                if insn is None or s + insn.size <= p:
                    continue
                try:
                    disp = insn.disp_offset and s + insn.disp_offset == p and (insn.disp & 0xFFFF) == offset16
                    imm = (insn.imm_offset and s + insn.imm_offset == p and
                           any(op.type == dos.capstone.x86.X86_OP_IMM and (op.imm & 0xFFFF) == offset16
                               for op in insn.operands))
                except (AttributeError, dos.capstone.CsError):
                    continue
                if disp or imm:
                    if base + s not in seen:
                        seen.add(base + s)
                        found.append((base + s, "uses offset" if disp else "offset value",
                                      "%s %s" % (insn.mnemonic, insn.op_str)))
                    break
            if len(found) >= max_results:
                return found
    if bits == 32:
        # Absolute operands: the 4 bytes of the address, inside an instruction
        # that starts up to 7 bytes earlier and really decodes with it.
        pat = struct.pack("<I", target)
        for m in re.finditer(re.escape(pat), data):
            p = m.start()
            # The earliest instruction start whose displacement or immediate is
            # exactly these 4 bytes (prefixes and opcode take at most ~7 bytes).
            for back in range(7, 0, -1):
                s = p - back
                if s < 0:
                    continue
                insn = _decode_at(data, base, bits, s, detail=True)
                if insn is not None and _operand_at(insn, s, p, target):
                    if base + s not in seen:
                        seen.add(base + s)
                        found.append((base + s, "uses address", "%s %s" % (insn.mnemonic, insn.op_str)))
                    break
            if len(found) >= max_results:
                return found
    # Relative branches: E8/E9 rel, 0F 8x rel (rel32 in 32-bit code, rel16 in 16-bit).
    size = 4 if bits == 32 else 2
    fmt = "<i" if size == 4 else "<h"
    for m in re.finditer(b"[\xE8\xE9]|\x0F[\x80-\x8F]", data):
        s = m.start()
        op_len = len(m.group(0))
        if s + op_len + size > len(data):
            continue
        rel = struct.unpack_from(fmt, data, s + op_len)[0]
        end = s + op_len + size
        if bits == 32:
            dest = (base + end + rel) & 0xFFFFFFFF
        else:
            dest = base + ((end + rel) & 0xFFFF)
        if dest != target or base + s in seen:
            continue
        insn = _decode_at(data, base, bits, s)
        if insn is None or insn.size != op_len + size:
            continue
        seen.add(base + s)
        kind = "calls it" if data[s] == 0xE8 else "jumps to it"
        found.append((base + s, kind, "%s %s" % (insn.mnemonic, insn.op_str)))
        if len(found) >= max_results:
            break
    found.sort()
    return found


def instructions(data, base, bits):
    """Decode data (at linear base) from its start; [] without capstone."""
    if dos.capstone is None:
        return []
    md = dos.capstone.Cs(dos.capstone.CS_ARCH_X86, dos.capstone.CS_MODE_16 if bits == 16 else dos.capstone.CS_MODE_32)
    return list(md.disasm(data, base))


def instruction_end(data, base, bits, length):
    """Offset of the first instruction boundary at or after `length`, decoding
    from the start of data; None if it can't be decoded that far."""
    pos = 0
    for insn in instructions(data, base, bits):
        if pos >= length:
            return pos
        pos += insn.size
    return pos if pos >= length else None


def encode_branch(kind, at, target, bits):
    """Bytes for a jmp/call from linear `at` to linear `target` (same code segment)."""
    if kind == "jmp" and -128 <= target - (at + 2) <= 127:
        return bytes([0xEB, (target - (at + 2)) & 0xFF])
    op = 0xE9 if kind == "jmp" else 0xE8
    if bits == 32:
        return bytes([op]) + struct.pack("<i", target - (at + 5))
    return bytes([op]) + struct.pack("<H", (target - (at + 3)) & 0xFFFF)
