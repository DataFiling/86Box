#!/bin/bash
# Build a hard disk image with FastDoom (viti95/FastDoom, a DOS/4GW Doom
# port) and the Freedoom game data, as out/fastdoom-hd.img, for the Pentium
# test machine (vms/pentium.cfg plus the [Hard disks] lines in README.md).
#
# FASTDOOM_PATCH=1 (default) applies fastdoom-ns_task-stack.diff, which
# fixes the timer interrupt handler corrupting DOS memory (see DEVLOG.md);
# FASTDOOM_PATCH=0 builds the unpatched game, which hangs during startup.
# FASTDOOM_DEBUG=1 (default) builds with -debug (frame-based locals, as in
# the bug); FASTDOOM_DEBUG=0 builds the release version.
# FASTDOOM_EXTENDER=dos4gw (default) or dos32a: the DOS extender bound to the
# game (DOS/32A comes with the FastDoom repository; bind_dos32a.py does what
# its SB/SS tools would).
#
# Needs: git and curl access to GitHub, Open Watcom v2 (WATCOM), nasm, mtools.

set -e
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${OUT:-$HERE/out}
: "${WATCOM:=/home/user/open-watcom/open-watcom-v2/rel}"
: "${FASTDOOM_COMMIT:=9a7b435a9b95db1ba38f89f1139050d748d32fde}"
: "${FREEDOOM_VERSION:=0.13.0}"
: "${FASTDOOM_PATCH:=1}"
: "${FASTDOOM_DEBUG:=1}"
: "${FASTDOOM_EXTENDER:=dos4gw}"
export WATCOM PATH="$WATCOM/binl64:$PATH" INCLUDE="$WATCOM/h" EDPATH="$WATCOM/eddat"

mkdir -p "$OUT/fastdoom-build"
cd "$OUT/fastdoom-build"

if [ ! -d fastdoom ]; then
    git clone -q https://github.com/viti95/FastDoom fastdoom
fi
(cd fastdoom && git checkout -q -f "$FASTDOOM_COMMIT")
if [ "$FASTDOOM_PATCH" = 1 ]; then
    (cd fastdoom && git apply "$HERE/fastdoom-ns_task-stack.diff")
fi
# Both builds keep the linker map (FASTDOOM/fdoom.map) for symbolising addresses.
find fastdoom -iname "*.obj" -delete
(cd fastdoom && ./build.sh fdoom.exe $([ "$FASTDOOM_DEBUG" = 1 ] && echo -debug) > build.log 2>&1) ||
    { tail -20 fastdoom/build.log; exit 1; }
cp fastdoom/FASTDOOM/fdoom.map FDOOM.MAP
case "$FASTDOOM_EXTENDER" in
dos4gw) cp fastdoom/FDOOM.EXE . ;;
dos32a) python3 "$HERE/bind_dos32a.py" fastdoom/FDOOM.EXE fastdoom/dos32a/BINW/DOS32A.EXE \
            fastdoom/DOS32A.D32 FDOOM.EXE ;;
*) echo "FASTDOOM_EXTENDER is dos4gw or dos32a"; exit 1 ;;
esac

if [ ! -f freedoom1.wad ]; then
    curl -sSL -o freedoom.zip \
        "https://github.com/freedoom/freedoom/releases/download/v$FREEDOOM_VERSION/freedoom-$FREEDOOM_VERSION.zip"
    unzip -p freedoom.zip "freedoom-$FREEDOOM_VERSION/freedoom1.wad" > freedoom1.wad
fi

# 64 MB disk (130 cylinders, 16 heads, 63 sectors), one FAT16 partition from
# sector 63. The MBR boots drive A: instead, since the disk has no DOS.
C=130 H=16 S=63
TOTAL=$((C * H * S))
IMG="$OUT/fastdoom-hd.img"
rm -f "$IMG"
truncate -s $((TOTAL * 512)) "$IMG"
nasm -f bin -o mbr.bin "$HERE/mbr_boot_a.asm"
python3 - "$IMG" $C $H $S <<'PY'
import struct, sys
img, C, H, S = sys.argv[1], *map(int, sys.argv[2:])
total = C * H * S
def chs(lba):
    c, h, s = lba // (H * S), (lba // S) % H, lba % S + 1
    return bytes([h, ((c >> 2) & 0xC0) | s, c & 0xFF])
mbr = bytearray(open("mbr.bin", "rb").read().ljust(446, b"\0")) + bytearray(66)
mbr[446:462] = bytes([0x80]) + chs(S) + bytes([0x06]) + chs(total - 1) + struct.pack("<II", S, total - S)
mbr[510:512] = b"\x55\xaa"
with open(img, "r+b") as f:
    f.write(mbr)
PY
P="$IMG@@$((S * 512))"
mformat -i "$P" -T $((TOTAL - S)) -h $H -s $S -H $S -v FASTDOOM ::
mmd -i "$P" ::/FDOOM
mcopy -i "$P" FDOOM.EXE ::/FDOOM/
if [ "$FASTDOOM_EXTENDER" = dos4gw ]; then
    mcopy -i "$P" "$WATCOM/binw/dos4gw.exe" ::/FDOOM/DOS4GW.EXE
fi
mcopy -i "$P" freedoom1.wad ::/FDOOM/FREEDM1.WAD   # FastDoom's name for it
mcopy -i "$P" fastdoom/FREEDM1.TCF ::/FDOOM/
mdir -i "$P" ::/FDOOM
echo "Wrote $IMG (linker map: $OUT/fastdoom-build/FDOOM.MAP)"
