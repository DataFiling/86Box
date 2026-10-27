#!/bin/bash
# Build a bootable 1.44 MB FreeDOS floppy image for the debugger test machines.
#
#   build_floppy.sh OUT.img KERNEL.SYS COMMAND.COM [FILE | FILE=DOSNAME ...]
#
# Uses the FreeDOS kernel repository's NASM image builder (test/bootimg) and
# boot sector (test/ldosboot), so no DOS or root access is needed. Files are
# stored in the root directory under their 8.3 names (uppercased), or under
# DOSNAME when given as FILE=DOSNAME. Set FDOS_KERNEL_DIR if the kernel
# repository is not at /home/user/fdos/kernel.

set -e

if [ $# -lt 3 ]; then
    sed -n '2,11p' "$0"
    exit 1
fi

OUT=$(realpath -m "$1")
KERNEL=$(realpath "$2")
SHELL_COM=$(realpath "$3")
shift 3

KDIR=${FDOS_KERNEL_DIR:-/home/user/fdos/kernel}
T=$KDIR/test
NASM=${NASM:-nasm}

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

cp "$KERNEL" "$STAGE/KERNEL.SYS"
cp "$SHELL_COM" "$STAGE/COMMAND.COM"
names="KERNEL.SYS,COMMAND.COM"
for f in "$@"; do
    src=${f%%=*}
    if [ "$src" != "$f" ]; then
        dst=${f#*=}
    else
        dst=$(basename "$src")
    fi
    dst=$(echo "$dst" | tr 'a-z' 'A-Z')
    cp "$(realpath "$src")" "$STAGE/$dst"
    names="$names,$dst"
done

cd "$STAGE"

# FAT12, 1 sector per cluster, 2880 sectors, 224 root entries, drive 00h.
"$NASM" "$T/ldosboot/boot.asm" -w-user -I"$T/lmacros/" \
    -D_COMPAT_FREEDOS=1 -D_LBA=0 -D_USE_PART_INFO=0 -D_QUERY_GEOMETRY=0 \
    -D_FAT12 -D_UNIT=00h -o boot.bin

"$NASM" "$T/bootimg/bootimg.asm" -I./ -I"$T/bootimg/" -I"$T/lmacros/" \
    -D_PAYLOADFILE="$names" -D_BOOTPATCHFILE="'boot.bin'" \
    -D_WARN_DEFAULT_OFF=1 -D_WARN_TOOMANYFAT=0 -D_WARN_ALIGNDATA=0 \
    -D_MBR_PART_TYPE=ptFAT12 -D_BPE=12 -D_SPC=1 -D_SPI=2880 -D_SPF=9 \
    -D_NUMROOT=224 -D_UNIT=00h -o "$OUT"

echo "Built $OUT:"
mdir -i "$OUT" -b :: 2>/dev/null | sed 's#^::/#  #' || true
