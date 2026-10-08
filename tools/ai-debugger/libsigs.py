"""Name library functions in a program without its map: signatures of the
public (and static) functions in OMF object libraries (.lib, .obj), matched
against the code in memory.

A function's signature is its bytes as the compiler emitted them, with the
bytes the linker fills in (fixups: addresses of data, call offsets...)
masked out. In a program built with the same library the function's bytes
are the same apart from those. Calls the function makes through fixups are
kept too, so a match also names the functions it calls.

Open Watcom's libraries (clib3r.lib, clibs.lib, math...) are OMF; so were
most DOS compilers' (Borland, Microsoft), whose libraries this reads too.
"""

import collections
import hashlib
import os
import pickle
import struct

MIN_KNOWN = 10      # known (unmasked) bytes a signature needs to be matched on its own
MAX_COMPARE = 1024  # bytes of a function compared
FORMAT = 1          # of the cached signatures: bump when the reader changes
WEAK = 16           # matches with fewer known bytes need more evidence (see identify)


class OmfError(ValueError):
    pass


def _index(data, i):
    """An OMF index (1 or 2 bytes) -> (value, next position)."""
    b = data[i]
    if b & 0x80:
        return ((b & 0x7F) << 8) | data[i + 1], i + 2
    return b, i + 1


def _name(data, i):
    n = data[i]
    return data[i + 1:i + 1 + n].decode("latin-1"), i + 1 + n


# Fixup location types -> bytes they cover.
_LOC_SIZE = {0: 1, 1: 2, 2: 2, 3: 4, 4: 1, 5: 2, 9: 4, 11: 6, 13: 4}

Function = collections.namedtuple("Function", "name module local code mask refs bits")
# code: bytes with masked bytes zeroed; mask: bytes, 1 where the byte is known;
# refs: ((offset of the fixup in the function, target name, self-relative), ...)


def _records(data, pos, end):
    while pos + 3 <= end:
        rtype = data[pos]
        length = struct.unpack_from("<H", data, pos + 1)[0]
        body = data[pos + 3:pos + 3 + length - 1]
        yield pos, rtype, body
        pos += 3 + length
        if rtype in (0x8A, 0x8B, 0xF1):
            return


class _Module:
    def __init__(self):
        self.name = ""
        self.lnames = [None]
        self.segs = [None]  # (name, class, length, use32)
        self.exts = [None]
        self.publics = []   # (seg index, offset, name, local)
        self.data = {}      # seg index -> bytearray
        self.known = {}     # seg index -> bytearray (1 = byte from LEDATA, not fixed up)
        self.fixups = []    # (seg index, offset, size, target name or None, self-relative)
        self.last = None    # (seg index, offset) of the last LEDATA
        self.threads = {}
        self.easy = False


def _parse_fixupp(m, body, wide):
    i = 0
    while i < len(body):
        b = body[i]
        if not b & 0x80:  # THREAD: remember a frame or target method for later fixups
            method = (b >> 2) & 7
            i += 1
            datum = None
            if not (b & 0x40) or method < 3:
                datum, i = _index(body, i)
            m.threads[("F" if b & 0x40 else "T", b & 3)] = (method, datum)
            continue
        loc = (b >> 2) & 0xF
        rel = not (b & 0x40)
        off = ((b & 3) << 8) | body[i + 1]
        i += 2
        fixdat = body[i]
        i += 1
        if fixdat & 0x80:
            pass  # frame from a thread
        else:
            if ((fixdat >> 4) & 7) < 3:
                _, i = _index(body, i)
        if fixdat & 0x08:
            method, datum = m.threads.get(("T", fixdat & 3), (0, None))
            method |= fixdat & 0x04
        else:
            method = fixdat & 7
            datum, i = _index(body, i)
        if not fixdat & 0x04:  # with a displacement
            disp = struct.unpack_from("<I" if wide else "<H", body, i)[0]
            i += 4 if wide else 2
        else:
            disp = 0
        target = None
        if (method & 3) == 2 and datum is not None and datum < len(m.exts):
            target = m.exts[datum]
        elif (method & 3) == 0 and datum is not None:
            target = ("seg", datum, disp)
        if m.last is not None:
            seg, base = m.last
            size = _LOC_SIZE.get(loc, 2)
            if m.easy and loc in (1, 3, 5):  # Easy OMF-386: offsets are 32-bit
                size += 2
            m.fixups.append((seg, base + off, size, target, rel))


def read_modules(data):
    """The modules of an OMF library or object file -> [_Module]."""
    if not data or data[0] not in (0xF0, 0x80, 0x82):
        raise OmfError("not an OMF library or object file")
    page = 0
    pos = 0
    if data[0] == 0xF0:
        page = struct.unpack_from("<H", data, 1)[0] + 3
        pos = page
    modules = []
    while pos < len(data) and data[pos] in (0x80, 0x82):
        m = _Module()
        end_pos = pos
        for rpos, rtype, body in _records(data, pos, len(data)):
            end_pos = rpos + 3 + len(body) + 1
            wide = (rtype & 1) or m.easy
            if rtype == 0x88 and len(body) >= 2 and body[1] == 0xAA:
                m.easy = True  # Phar Lap Easy OMF-386: 16-bit record types with 32-bit fields
            elif rtype in (0x80, 0x82):
                m.name, _ = _name(body, 0)
            elif rtype == 0x96:
                i = 0
                while i < len(body):
                    n, i = _name(body, i)
                    m.lnames.append(n)
            elif rtype in (0x98, 0x99):
                attr = body[0]
                i = 1
                if (attr >> 5) == 0:
                    i += 3
                length = struct.unpack_from("<I" if wide else "<H", body, i)[0]
                i += 4 if wide else 2
                if attr & 2:  # "big": a full 64 KiB (or 4 GiB) segment
                    length = 0x10000 if not wide else length
                sname, i = _index(body, i)
                cname, i = _index(body, i)
                m.segs.append((m.lnames[sname] if sname < len(m.lnames) else "",
                               m.lnames[cname] if cname < len(m.lnames) else "", length, bool(attr & 1) or m.easy))
            elif rtype in (0x8C, 0xB4, 0xBC):  # EXTDEF, LEXTDEF, CEXTDEF
                i = 0
                while i < len(body):
                    if rtype == 0xBC:
                        idx, i = _index(body, i)
                        n = m.lnames[idx] if idx < len(m.lnames) else ""
                    else:
                        n, i = _name(body, i)
                    _, i = _index(body, i)
                    m.exts.append(n)
            elif rtype in (0xB0, 0xB8):  # COMDEF, LCOMDEF: communal variables, also external names
                i = 0
                while i < len(body):
                    n, i = _name(body, i)
                    _, i = _index(body, i)
                    kind = body[i]
                    i += 1
                    for _ in range(2 if kind == 0x61 else 1):
                        i += {0x81: 3, 0x84: 4, 0x88: 5}.get(body[i], 1)
                    m.exts.append(n)
            elif rtype in (0x90, 0x91, 0xB6, 0xB7):  # PUBDEF, LPUBDEF
                _, i = _index(body, 0)
                seg, i = _index(body, i)
                if seg == 0:
                    i += 2
                while i < len(body):
                    n, i = _name(body, i)
                    off = struct.unpack_from("<I" if wide else "<H", body, i)[0]
                    i += 4 if wide else 2
                    _, i = _index(body, i)
                    if seg:
                        m.publics.append((seg, off, n, rtype >= 0xB6))
            elif rtype in (0xA0, 0xA1):  # LEDATA
                seg, i = _index(body, 0)
                off = struct.unpack_from("<I" if wide else "<H", body, i)[0]
                i += 4 if wide else 2
                chunk = body[i:]
                if seg < len(m.segs):
                    buf = m.data.setdefault(seg, bytearray(m.segs[seg][2]))
                    kn = m.known.setdefault(seg, bytearray(m.segs[seg][2]))
                    if off + len(chunk) > len(buf):
                        buf.extend(bytes(off + len(chunk) - len(buf)))
                        kn.extend(bytes(off + len(chunk) - len(kn)))
                    buf[off:off + len(chunk)] = chunk
                    kn[off:off + len(chunk)] = b"\1" * len(chunk)
                m.last = (seg, off)
            elif rtype in (0xA2, 0xA3):  # LIDATA (repeated data): left unknown
                m.last = None
            elif rtype in (0x9C, 0x9D):
                _parse_fixupp(m, body, rtype & 1)
        modules.append(m)
        pos = end_pos
        if page:
            pos = (pos + page - 1) // page * page
        while pos < len(data) and data[pos] == 0:
            pos += 1
    return modules


def functions(modules):
    """The functions (publics and statics in code segments) of OMF modules."""
    out = []
    for m in modules:
        for seg, buf in m.data.items():
            name, cls, length, use32 = m.segs[seg]
            if not cls.upper().endswith("CODE"):
                continue
            kn = bytearray(m.known[seg])
            refs = collections.defaultdict(list)
            for fseg, off, size, target, rel in m.fixups:
                if fseg != seg:
                    continue
                kn[off:off + size] = bytes(size)
                if target is not None:
                    refs[off].append((size, target, rel))
            pubs = sorted({(off, n, local) for s, off, n, local in m.publics if s == seg})
            by_off = {off: n for off, n, _ in reversed(pubs)}  # the first name at an offset
            for k, (off, n, local) in enumerate(pubs):
                end = next((o for o, _, _ in pubs[k + 1:] if o > off), max(length, len(buf)))
                if end <= off:
                    continue
                fref = []
                for roff in sorted(r for r in refs if off <= r < end):
                    for size, target, rel in refs[roff]:
                        if isinstance(target, tuple):  # an address in a segment of this module
                            _, tseg, disp = target
                            target = by_off.get(disp) if tseg == seg else None
                        if target:
                            fref.append((roff - off, target, rel))
                out.append(Function(n, m.name, local, bytes(buf[off:end]), bytes(kn[off:end]), tuple(fref),
                                    32 if use32 else 16))
    return out


class Signatures:
    """Function signatures from libraries, indexed for matching."""

    def __init__(self, funcs, sources=()):
        self.sources = list(sources)
        self.by_bits = {16: [], 32: []}
        self.by_name = collections.defaultdict(list)
        self.by_module = collections.defaultdict(list)
        seen = {}
        for item in funcs:
            lib, f = item if isinstance(item, tuple) else ("", item)
            key = (f.code[:MAX_COMPARE], f.mask[:MAX_COMPARE], f.bits, f.refs)
            if key in seen:  # the same code under another name (an alias, or another library)
                if f.name not in seen[key]["names"]:
                    seen[key]["names"].append(f.name)
                    self.by_name[f.name].append(seen[key])
                seen[key]["libs"].add(lib)
                continue
            sig = {"names": [f.name], "module": f.module, "local": f.local, "code": key[0], "mask": key[1],
                   "known": sum(key[1]), "length": len(f.code), "refs": f.refs, "bits": f.bits, "libs": {lib}}
            seen[key] = sig
            self.by_module[(f.module, f.bits)].append(sig)
            self.by_bits[f.bits].append(sig)
            self.by_name[f.name].append(sig)
        # Indexes: by the first 6 bytes (to scan all code), for signatures whose
        # first 6 bytes are known, and by the first 2 (to test call targets).
        self.prefix = {16: collections.defaultdict(list), 32: collections.defaultdict(list)}
        self.first2 = {16: collections.defaultdict(list), 32: collections.defaultdict(list)}
        self.thin = {16: collections.defaultdict(list), 32: collections.defaultdict(list)}
        for bits, sigs in self.by_bits.items():
            for s in sigs:
                s["runs"] = _known_runs(s["code"], s["mask"])
                # Tables in code segments (all zeros, repeated bytes) match anywhere.
                s["plain"] = len(set(b for _, run in s["runs"] for b in run)) < 5
                if 1 <= s["known"] < 6 and s["mask"][0] and any(r[2] for r in s["refs"]):
                    self.thin[bits][s["code"][:1]].append(s)
                if s["plain"]:
                    continue
                if s["mask"][:6] == b"\1" * 6 and s["known"] >= MIN_KNOWN:
                    self.prefix[bits][s["code"][:6]].append(s)
                self.first2[bits][s["code"][:2] if s["mask"][:2] == b"\1\1" else None].append(s)

    def __len__(self):
        return sum(len(v) for v in self.by_bits.values())


def _known_runs(code, mask):
    """(start, bytes) of each run of known bytes."""
    runs, i = [], 0
    while i < len(mask):
        if mask[i]:
            j = i
            while j < len(mask) and mask[j]:
                j += 1
            runs.append((i, code[i:j]))
            i = j
        else:
            i += 1
    return runs


def matches(sig, data, pos):
    """Whether the code at data[pos] is sig's function."""
    if pos + len(sig["code"]) > len(data):
        return False
    return all(data[pos + i:pos + i + len(b)] == b for i, b in sig["runs"])


def _plain_first(names):
    """Distinct names, the plainest first: malloc_ before _nmalloc_."""
    out = []
    for n in names:
        if n not in out:
            out.append(n)
    return sorted(out, key=lambda n: (len(n) - len(n.lstrip("_")), len(n)))


def _call_target(data, at, base, bits, frame):
    """Target of the call/jump whose rel16/rel32 operand is at data[at]."""
    if bits == 32:
        return (base + at + 4 + struct.unpack_from("<i", data, at)[0]) & 0xFFFFFFFF
    return frame + ((base + at + 2 - frame + struct.unpack_from("<h", data, at)[0]) & 0xFFFF)


def _refs_agree(sig, data, pos, base, bits, frame, found, data_syms):
    """How many of sig's calls go to a function found under the called name,
    and of its (32-bit) variable addresses to a variable found under its name."""
    n = 0
    for off, name, rel in sig["refs"]:
        if pos + off + bits // 8 > len(data):
            continue
        if rel:
            f = found.get(_call_target(data, pos + off, base, bits, frame))
            n += f is not None and (f["name"] == name or name in f["alts"])
        elif bits == 32 and name in data_syms and off + 4 <= len(sig["code"]):
            v = struct.unpack_from("<I", data, pos + off)[0] - struct.unpack_from("<I", sig["code"], off)[0]
            n += data_syms[name].most_common(1)[0][0] == v & 0xFFFFFFFF
    return n


def identify(data, base, bits, sigs, call_targets=(), frame=0):
    """Library functions in code `data` at linear `base`. 16-bit code takes the
    linear address of its code segment (`frame`) for near call offsets.
    call_targets: linear addresses called in the code (function entries).
    -> ({linear: {"name", "alts", "how", "length"}}, {linear: data symbol name})."""
    data = bytes(data)
    hi = base + len(data)
    found = {}
    targets = {t for t in call_targets if base <= t < hi}

    def best_at(pos, cands):
        hit = [s for s in cands if matches(s, data, pos)]
        if not hit:
            return None, []
        top = max(s["known"] for s in hit)
        best = [s for s in hit if s["known"] == top]
        return best[0], best

    def accept(lin, sig, alts, how):
        names = _plain_first(n for s in alts for n in s["names"])
        found[lin] = {"name": names[0], "alts": names[1:], "how": how, "length": sig["length"], "sig": sig,
                      "cands": alts}

    # Signatures long enough to be told apart: anywhere in the code.
    prefix = sigs.prefix[bits]
    if prefix:
        for pos in range(len(data) - 5):
            cands = prefix.get(data[pos:pos + 6])
            if cands:
                sig, alts = best_at(pos, cands)
                if sig is not None:
                    accept(base + pos, sig, alts, "signature")
    # Shorter ones only where the code calls them.
    for t in sorted(targets - set(found)):
        pos = t - base
        sig, alts = best_at(pos, sigs.first2[bits].get(data[pos:pos + 2], []) + sigs.first2[bits].get(None, []))
        if sig is not None and sig["known"] >= 6:
            accept(t, sig, alts, "signature")
    # Overlapping matches: the one with the most known bytes wins (a short
    # signature can match inside a longer function), then the first.
    kept = {}
    for lin in sorted(found, key=lambda a: (-found[a]["sig"]["known"], a)):
        f = found[lin]
        end = lin + f["length"]
        if not any(k < end and lin < k + kept[k]["length"] for k in kept):
            kept[lin] = f
    found = kept
    # Weak matches (few known bytes) must fit in: their library is one the
    # confident matches came from (not the C++ library's string operators in
    # a C program), and the functions they call with signatures are there.
    confident = collections.Counter(lib for f in found.values() if f["sig"]["known"] >= 24
                                    for lib in f["sig"].get("libs", ()))
    used = {lib for lib, n in confident.items() if n >= 3}
    for lin in sorted(found, key=lambda a: found[a]["sig"]["known"]):
        f = found[lin]
        if f["sig"]["known"] >= WEAK:
            continue
        if used and not any(lib in used for s in f["cands"] for lib in s.get("libs", ())):
            del found[lin]
            continue
        for off, name, rel in f["sig"]["refs"]:
            at = lin - base + off
            if not rel or at + bits // 8 > len(data):
                continue
            t = _call_target(data, at, base, bits, frame)
            g = found.get(t)
            if g is not None and (g["name"] == name or name in g["alts"]):
                continue
            callee = [s for s in sigs.by_name.get(name, []) if s["bits"] == bits]
            if callee and not (base <= t < hi and any(matches(s, data, t - base) for s in callee)):
                del found[lin]
                break
    # What the identified functions call or use, by the names in their
    # fixups: only what all candidate signatures of a function agree on, until
    # their calls tell them apart.
    data_syms = collections.defaultdict(collections.Counter)
    size = 4 if bits == 32 else 2

    def propagate(todo, done):
        while todo:
            lin = todo.pop()
            f = found[lin]
            pos = lin - base
            refs = set(f["cands"][0]["refs"]) if f["cands"] else set(f["sig"]["refs"])
            for other in f["cands"][1:]:
                refs &= set(other["refs"])
            for off, name, rel in sorted(refs - done.get(lin, set())):
                done.setdefault(lin, set()).add((off, name, rel))
                at = pos + off
                if at + size > len(data):
                    continue
                if rel:
                    t = _call_target(data, at, base, bits, frame)
                    if not base <= t < hi or t in found:
                        continue
                    # The linker resolved this name here. Code unlike the library's
                    # is the program's own (main_, or a library function it replaces).
                    hit = [s for s in sigs.by_name.get(name, []) if s["bits"] == bits and matches(s, data, t - base)]
                    sig = hit[0] if hit else {"names": [name], "known": 0, "length": 1, "refs": ()}
                    found[t] = {"name": name, "alts": [], "how": "called from " + f["name"], "length": sig["length"],
                                "sig": sig, "cands": [sig]}
                    todo.append(t)
                elif bits == 32 and off + 4 <= len(f["sig"]["code"]):
                    # An absolute address (offset32): data, or a function's address.
                    addend = struct.unpack_from("<I", f["sig"]["code"], off)[0]
                    v = struct.unpack_from("<I", data, at)[0]
                    data_syms[name][(v - addend) & 0xFFFFFFFF] += 1

    # A module's functions stay together and in order: short functions right
    # before or after one found, from the same module.
    def neighbours():
        added = []
        grew = True
        while grew:
            grew = False
            for lin, f in sorted(found.items()):
                module = f["sig"].get("module")
                for s in sigs.by_module.get((module, bits), []):
                    for at in (lin + f["length"], lin - s["length"]):
                        if at in found or not base <= at < hi or s["plain"] or s["known"] < 2:
                            continue
                        if matches(s, data, at - base) and not any(o < at + s["length"] and at < o + found[o]["length"]
                                                                   for o in found):
                            accept(at, s, [s], "next to " + f["name"])
                            added.append(at)
                            grew = True
        return added

    def disambiguate():
        """Functions with the same bytes but different calls (printf_ and
        wprintf_s_ call different routines): keep the candidates whose calls
        (and variables) go where the names found say, then those from the
        library the unambiguous functions came from (e.g. the register- rather
        than the stack-convention C library). -> the functions narrowed down."""
        libs = collections.Counter(lib for f in found.values() if len(f["cands"]) == 1
                                   for lib in f["sig"].get("libs", ()))
        changed = []
        for lin, f in found.items():
            if len(f["cands"]) < 2:
                continue
            scores = [((_refs_agree(s, data, lin - base, base, bits, frame, found, data_syms),
                        max((libs[x] for x in s.get("libs", ())), default=0)), s) for s in f["cands"]]
            top = max(sc for sc, _ in scores)
            keep = [s for sc, s in scores if sc == top]
            if len(keep) < len(f["cands"]):
                names = _plain_first(n for s in keep for n in s["names"])
                f.update(name=names[0], alts=names[1:], sig=keep[0], cands=keep)
                changed.append(lin)
        return changed

    done = {}
    disambiguate()
    # A weak match that still has candidates with different names is a guess.
    for lin in [a for a, f in found.items() if f["sig"]["known"] < WEAK and len(f["cands"]) > 1]:
        del found[lin]
    propagate(list(found), done)
    propagate(disambiguate(), done)
    while True:
        added = neighbours()
        if not added:
            break
        propagate(added, done)
    # Thin wrappers (a jump or call to another function and little else),
    # where the code calls them: when all their calls go to the functions
    # found under the names they call.
    for t in sorted(targets - set(found)):
        pos = t - base
        for s in sigs.thin[bits].get(data[pos:pos + 1], []):
            calls = [r for r in s["refs"] if r[2]]
            if matches(s, data, pos) and _refs_agree(s, data, pos, base, bits, frame, found, {}) == len(calls):
                accept(t, s, [s], "calls " + calls[0][1])
                break
    out = {}
    for n, addrs in data_syms.items():
        a = addrs.most_common(1)[0][0]
        if base <= a < hi:
            if a not in found and any(s["bits"] == bits and matches(s, data, a - base) for s in sigs.by_name.get(n, [])):
                found[a] = {"name": n, "alts": [], "how": "address used by a library function", "length": 1,
                            "sig": {"names": [n], "known": 0, "length": 1, "refs": ()}, "cands": []}
        else:
            out[a] = n
    return found, out


def reachable_span(data, base, bits, starts, entry, frame=0):
    """(lo, hi) of the code reached from `entry` by direct calls and jumps
    between the function `starts` (each runs to the next start): the
    program's code, as opposed to other copies of it in the same memory
    (an extender's load buffer). None if too little is reached to tell."""
    starts = sorted(a for a in set(starts) | {entry} if base <= a < base + len(data))
    index = {a: i for i, a in enumerate(starts)}
    size = 4 if bits == 32 else 2
    seen, todo = set(), [entry]
    while todo:
        a = todo.pop()
        if a in seen:
            continue
        seen.add(a)
        end = starts[index[a] + 1] if index[a] + 1 < len(starts) else base + len(data)
        chunk = data[a - base:min(end, a + 0x10000) - base]
        for i, b in enumerate(chunk):
            if b in (0xE8, 0xE9) and i + 1 + size <= len(chunk):
                t = _call_target(data, a - base + i + 1, base, bits, frame)
                if t in index and t not in seen:
                    todo.append(t)
    if len(seen) < 5:
        return None
    last = max(seen)
    end = starts[index[last] + 1] if index[last] + 1 < len(starts) else base + len(data)
    return min(seen), end


def default_libraries(bits=None):
    """Open Watcom's DOS libraries under $WATCOM (lib286/dos, lib286, lib386/dos, lib386)."""
    root = os.environ.get("WATCOM")
    if not root:
        return []
    dirs = []
    if bits in (None, 16):
        dirs += [os.path.join(root, "lib286", "dos"), os.path.join(root, "lib286")]
    if bits in (None, 32):
        dirs += [os.path.join(root, "lib386", "dos"), os.path.join(root, "lib386")]
    out = []
    for d in dirs:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        out += [os.path.join(d, n) for n in names if n.lower().endswith((".lib", ".obj"))]
    return out


def load(paths, cache_dir=None):
    """Signatures for these library/object files (cached by path, size and time)."""
    funcs, used = [], []
    for p in paths:
        st = os.stat(p)
        cache = None
        if cache_dir:
            key = "%d|%s|%d|%r" % (FORMAT, os.path.abspath(p), st.st_size, st.st_mtime)
            cache = os.path.join(cache_dir, "sig-%s.pkl" % hashlib.sha1(key.encode()).hexdigest()[:16])
            try:
                with open(cache, "rb") as f:
                    got = pickle.load(f)
                if got[0] == (os.path.abspath(p), st.st_size, st.st_mtime):
                    funcs += [(p, f) for f in got[1]]
                    used.append(p)
                    continue
            except (OSError, pickle.PickleError, EOFError, IndexError, TypeError):
                pass
        with open(p, "rb") as f:
            data = f.read()
        try:
            fs = functions(read_modules(data))
        except (OmfError, IndexError, struct.error):
            continue  # not OMF, or a format this reader doesn't know: skip the file
        if cache:
            try:
                with open(cache, "wb") as f:
                    pickle.dump(((os.path.abspath(p), st.st_size, st.st_mtime), fs), f)
            except OSError:
                pass
        funcs += [(p, f) for f in fs]
        used.append(p)
    return Signatures(funcs, used)
