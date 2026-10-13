#!/bin/bash
# Make a ready-to-run 86Box VM for debugging DOS games:
#
#   DIR/86box.cfg  Pentium MMX 200 (ASUS P/I-P55T2P4), 32 MB, VGA, PS/2 wheel
#                  mouse, Sound Blaster 16 set like DOSBox's (220h, IRQ 7,
#                  DMA 1, high DMA 5: what GOG pre-configures its DOS games
#                  for), the debugger on port 12345 (-p to change)
#   DIR/boot.img   the FreeDOS boot floppy with JemmEx (EMS/XMS), CuteMouse
#                  and BLASTER set; it boots to A:\> and switches to C:
#   DIR/hd.img     an empty FAT16 hard disk, C: (-s MB, 17 to 504), for games:
#                  gog_import.py setup_game.exe DIR/hd.img puts one there
#   DIR/nvr/       the CMOS settings (floppy drive, IDE auto-detect), so the
#                  first boot needs no BIOS setup
#
# Usage: make_game_vm.sh DIR [-s MB] [-p PORT] [-f FLOPPY]
#   FLOPPY: a boot floppy from build_emm_floppy.sh (default out/boot-emm.img).
# Then: 86Box -P DIR -R /path/to/roms   (86Box built with -DGDBSTUB=ON)

set -e
HERE=$(cd "$(dirname "$0")" && pwd)
DIR=${1:?usage: $0 DIR [-s MB] [-p PORT] [-f FLOPPY]}
shift
SIZE=500 PORT=12345 FLOPPY="$HERE/out/boot-emm.img"
while getopts "s:p:f:" o; do
    case $o in
        s) SIZE=$OPTARG ;;
        p) PORT=$OPTARG ;;
        f) FLOPPY=$OPTARG ;;
        *) exit 1 ;;
    esac
done
[ -f "$FLOPPY" ] || { echo "No boot floppy at $FLOPPY: build it with build_emm_floppy.sh, or give one with -f."; exit 1; }
[ "$SIZE" -ge 17 ] && [ "$SIZE" -le 504 ] || { echo "-s: 17 to 504 (MB; FAT16 on a CHS disk without translation)"; exit 1; }
for t in mformat mcopy mtype python3; do
    command -v $t >/dev/null || { echo "Needs $t (mtools, python3)"; exit 1; }
done
if [ -e "$DIR/hd.img" ]; then
    echo "$DIR/hd.img exists: not overwriting a disk that may hold games."
    exit 1
fi
mkdir -p "$DIR/nvr"

# The disk: 16 heads, 63 sectors, as many cylinders as fit SIZE MB; one
# FAT16 partition from sector 63 (byte 32256). Its MBR boots the floppy:
# DOS is on A:, the disk is only for games.
H=16 S=63
C=$((SIZE * 1024 * 1024 / (H * S * 512)))
TOTAL=$((C * H * S))
truncate -s $((TOTAL * 512)) "$DIR/hd.img"
MBR=$(mktemp)
trap 'rm -f "$MBR"' EXIT
if command -v nasm >/dev/null; then
    nasm -f bin -o "$MBR" "$HERE/mbr_boot_a.asm"
else
    : > "$MBR"  # no boot code: the BIOS falls back to the floppy by itself
fi
python3 - "$DIR/hd.img" "$MBR" $C $H $S <<'PY'
import struct, sys
img, mbr_file, C, H, S = sys.argv[1], sys.argv[2], *map(int, sys.argv[3:])
total = C * H * S
def chs(lba):
    c, h, s = lba // (H * S), (lba // S) % H, lba % S + 1
    return bytes([h, ((c >> 2) & 0xC0) | s, c & 0xFF])
code = open(mbr_file, "rb").read()[:446]
mbr = bytearray(code.ljust(446, b"\0")) + bytearray(66)
ptype = 0x06 if (total - S) * 512 >= 32 * 1024 * 1024 else 0x04  # FAT16 big / small
mbr[446:462] = bytes([0x80]) + chs(S) + bytes([ptype]) + chs(total - 1) + struct.pack("<II", S, total - S)
mbr[510:512] = b"\x55\xaa"
with open(img, "r+b") as f:
    f.write(mbr)
PY
mformat -i "$DIR/hd.img@@$((S * 512))" -T $((TOTAL - S)) -h $H -s $S -H $S -v GAMES ::

# The floppy: a copy, with BLASTER set to match the card, and C: as the
# current drive once it has booted.
cp "$FLOPPY" "$DIR/boot.img"
A=$(mktemp)
mtype -i "$DIR/boot.img" ::/AUTOEXEC.BAT | tr -d '\r' | grep -v -i '^SET BLASTER=' > "$A"
{
    grep -v -i '^ECHO READY' "$A"
    echo 'SET BLASTER=A220 I7 D1 H5 P330 T6'
    echo 'PATH A:\;C:\'
    echo 'C:'
    echo 'ECHO READY'
} | sed 's/$/\r/' > "$A.new"
mcopy -o -i "$DIR/boot.img" "$A.new" ::/AUTOEXEC.BAT
rm -f "$A" "$A.new"

# The CMOS: floppy drive A: 1.44 MB, primary master IDE auto-detected.
[ -f "$HERE/vms/p55t2p4.nvr" ] && cp "$HERE/vms/p55t2p4.nvr" "$DIR/nvr/"

cat > "$DIR/86box.cfg" <<EOF
[General]
gdbstub_port = $PORT

[Machine]
machine = p55t2p4
cpu_family = pentium_p55c
cpu_speed = 200000000
cpu_multi = 3
cpu_use_dynarec = 0
fpu_type = internal
mem_size = 32768

[Video]
gfxcard = vga

[Input devices]
keyboard_type = keyboard_ps2
mouse_type = ps2

[PS/2 Mouse]
buttons = 4

[Sound]
sndcard = sb16

[Sound Blaster 16]
irq = 7
dma = 1
dma16 = 5

[Floppy and CD-ROM drives]
fdd_01_type = 35_2hd
fdd_01_fn = boot.img
fdd_02_type = none

[Hard disks]
hdd_01_parameters = $S, $H, $C, 0, ide
hdd_01_fn = hd.img
hdd_01_ide_channel = 0:0
hdd_01_speed = 1997_5400rpm
EOF
echo "Made $DIR: C: is $((TOTAL * 512 / 1048576)) MB (hd.img, FAT16 partition at byte 32256)."
echo "Debugger port $PORT. Start it with: 86Box -P $DIR -R /path/to/roms"
