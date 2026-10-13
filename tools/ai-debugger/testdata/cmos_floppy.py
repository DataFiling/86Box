"""Configure a fresh AT-class test machine's CMOS for one 1.44 MB drive A:
(plus an x87 if --fpu) through the debugger's I/O port access, fix the
standard CMOS checksum, and hard-reset so the BIOS boots from the floppy.

    python3 cmos_floppy.py [--port 12345] [--fpu]

86Box saves CMOS in the VM's .nvr file, so this is needed once per VM.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from gdb_rsp import GdbClient  # noqa: E402


def cmos_read(c, reg):
    c.monitor("ob 70 %x" % (0x80 | reg))  # bit 7 keeps NMI masked
    return int(c.monitor("ib 71 1").split()[1], 16)


def cmos_write(c, reg, val):
    c.monitor("ob 70 %x" % (0x80 | reg))
    c.monitor("ob 71 %x" % val)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12345)
    ap.add_argument("--fpu", action="store_true")
    a = ap.parse_args()

    c = GdbClient(port=a.port)
    c.connect()
    c.pause()

    cmos_write(c, 0x10, 0x40)                       # drive A: 1.44 MB, no B:
    cmos_write(c, 0x12, 0x00)                       # no hard disks
    cmos_write(c, 0x14, 0x01 | (0x02 if a.fpu else 0))  # floppy present, EGA/VGA
    total = sum(cmos_read(c, r) for r in range(0x10, 0x2E))
    cmos_write(c, 0x2E, (total >> 8) & 0xFF)
    cmos_write(c, 0x2F, total & 0xFF)
    cmos_write(c, 0x0E, 0x00)                       # clear diagnostic status
    print("CMOS 10h=%02X 14h=%02X checksum=%04X" % (cmos_read(c, 0x10), cmos_read(c, 0x14), total))
    c.monitor("ob 70 0")
    c.monitor("r")
    c.resume()
    c.close()


if __name__ == "__main__":
    main()
