#!/usr/bin/env python3
"""Bind the DOS/32A extender to a linear executable (LE), as DOS/32A's own
tools do with "SB /R program.exe" then "SS program.exe config.d32", without
needing DOS to run them. The output is byte-identical to theirs (checked on
FastDoom with DOS/32A 9.1.2).

    bind_dos32a.py program.exe DOS32A.EXE config.d32 out.exe

- program.exe: an LE program with any stub (e.g. Open Watcom's DOS/4GW stub)
- DOS32A.EXE: the extender (dos32a/BINW/DOS32A.EXE in the FastDoom repository)
- config.d32: a 24-byte "ID32" settings block made by SS (FastDoom: DOS32A.D32)

What SB does: the extender becomes the stub. Its MZ header is rewritten with
the relocation table at 40h and the LE header's file offset at 3Ch (the
new-executable convention), and the LE part is appended unchanged except
for its one file-relative offset, that of the data pages (LE header +80h).
SS then copies the settings over the extender's own "ID32" block.
"""

import struct
import sys

MZ_FIELDS = "<14H"  # e_magic ... e_ovno


def le_part(exe):
    off = struct.unpack_from("<I", exe, 0x3C)[0]
    if exe[off:off + 2] != b"LE":
        raise ValueError("not a linear executable (no LE header at %Xh)" % off)
    return off, exe[off:]


def extender_stub(dos32a):
    """DOS32A.EXE with its MZ header moved to the 40h layout; returns bytes
    whose length is where the LE header goes."""
    f = list(struct.unpack_from(MZ_FIELDS, dos32a, 0))
    if f[0] != 0x5A4D:
        raise ValueError("DOS32A.EXE is not an MZ program")
    nrel, hdr_size, rel_off = f[3], f[4] * 16, f[12]
    relocs = dos32a[rel_off:rel_off + 4 * nrel]
    body = dos32a[hdr_size:]
    new_hdr = (0x40 + len(relocs) + 15) // 16 * 16
    total = new_hdr + len(body)
    f[1], f[2] = total % 512, (total + 511) // 512  # bytes in the last page, pages
    f[4], f[12] = new_hdr // 16, 0x40
    head = struct.pack(MZ_FIELDS, *f).ljust(0x3C, b"\0") + struct.pack("<I", total)
    return (head + relocs).ljust(new_hdr, b"\0") + body


def bind(program, dos32a, config):
    _, le = le_part(program)
    stub = bytearray(extender_stub(dos32a))
    if config:
        i = stub.find(b"ID32")
        if i < 0 or not config.startswith(b"ID32"):
            raise ValueError("no ID32 settings block in the extender or the config file")
        stub[i:i + len(config)] = config
    le = bytearray(le)
    old_off = struct.unpack_from("<I", program, 0x3C)[0]
    pages = struct.unpack_from("<I", le, 0x80)[0]  # data pages offset, from the start of the file
    struct.pack_into("<I", le, 0x80, pages - old_off + len(stub))
    return bytes(stub) + bytes(le)


def main():
    if len(sys.argv) != 5:
        sys.exit(__doc__.split("\n\n")[1])
    prog, ext, cfg, out = sys.argv[1:]
    data = bind(open(prog, "rb").read(), open(ext, "rb").read(), open(cfg, "rb").read())
    with open(out, "wb") as f:
        f.write(data)
    print("Wrote %s (%d bytes): DOS/32A + %s" % (out, len(data), prog))


if __name__ == "__main__":
    main()
