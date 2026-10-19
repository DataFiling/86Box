#!/bin/bash
# Build the DOS CD-ROM drivers for the game VM's floppy (make_game_vm.sh):
#
#   out/cd/GCDROM.SYS   IDE/ATAPI CD-ROM driver (Jack R. Ellis's XCDROM, as
#                       FreeDOS packages it: GPL 2 or later), with
#                       gcdrom-legacy-ide.diff: this version, adapted for
#                       SATA, looked for drives at 0080h and 0000h unless a
#                       native-mode controller was found, so it missed the
#                       game VM's drive on the PIIX3's legacy channels
#   out/cd/SHSUCDX.COM  CD-ROM extender, the MSCDEX replacement that gives the
#                       drive a letter (Jason Hood after John McCoy: zlib
#                       license)
#
# Both are assembled from source at the commits below. Needs git, patch and nasm.

set -e
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${OUT:-$HERE/out}
GCDROM_REPO=https://gitlab.com/FreeDOS/drivers/gcdrom.git
GCDROM_COMMIT=815078126e1332ec0917536043d973407e1008c1
SHSUCD_REPO=https://github.com/adoxa/shsucd.git
SHSUCD_COMMIT=5ea0787549f60270df5f70a0d6b8d4d9f5cb49da

for t in git patch nasm; do
    command -v $t > /dev/null || { echo "Needs $t"; exit 1; }
done
SRC="$OUT/cd-src"
mkdir -p "$SRC" "$OUT/cd"
fetch() {  # REPO COMMIT DIR
    [ -d "$3/.git" ] || git clone -q "$1" "$3"
    git -C "$3" checkout -q "$2"
}
fetch "$GCDROM_REPO" "$GCDROM_COMMIT" "$SRC/gcdrom"
fetch "$SHSUCD_REPO" "$SHSUCD_COMMIT" "$SRC/shsucd"

# GCDROM: XCDROM.ASM (with LF line ends, for the patch) and its English
# messages. Unpatched, it assembles to the GCDROM.SYS the package ships.
B="$SRC/build"
rm -rf "$B" && mkdir -p "$B/SOURCE/GCDROM"
tr -d '\r' < "$SRC/gcdrom/SOURCE/GCDROM/XCDROM.ASM" > "$B/SOURCE/GCDROM/XCDROM.ASM"
tr -d '\r' < "$SRC/gcdrom/SOURCE/GCDROM/XCDMSGS.ENG" > "$B/SOURCE/GCDROM/xcdmsgs.eng"
patch -s -d "$B" -p1 < "$HERE/gcdrom-legacy-ide.diff"
(cd "$B/SOURCE/GCDROM" && nasm -O9 -w-all -f bin -o "$OUT/cd/GCDROM.SYS" XCDROM.ASM)

# SHSUCDX, the 386+ build (shcdx86.com is the 8086 one; the game VM is a Pentium).
cp "$SRC/shsucd/shsucdx.nsm" "$SRC/shsucd/nasm.mac" "$SRC/shsucd/undoc.mac" "$SRC/shsucd/cdrom.mac" "$B/"
(cd "$B" && nasm -O9 -w-all -f bin -o "$OUT/cd/SHSUCDX.COM" shsucdx.nsm)

echo "Wrote $OUT/cd/GCDROM.SYS ($(stat -c %s "$OUT/cd/GCDROM.SYS") bytes) and $OUT/cd/SHSUCDX.COM ($(stat -c %s "$OUT/cd/SHSUCDX.COM") bytes)"
