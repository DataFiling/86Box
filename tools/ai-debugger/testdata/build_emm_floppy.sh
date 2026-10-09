#!/bin/bash
# Make out/boot-emm.img: the test floppy (out/boot.img, from build_testbed.sh)
# with JemmEx loaded from FDCONFIG.SYS, so DOS runs in V86 mode under an
# EMM386-style memory manager (XMS, EMS, VCPI) as on most 386+ DOS setups.
#
# Needs: the Jemm repository (github.com/Baron-von-Riedesel/Jemm), the JWasm
# source tree, Open Watcom v2 (its wlink stands in for JWlink), mtools.
# JWasm is rebuilt with _FORTIFY_SOURCE off: its listing writer overflows a
# 3-byte field on Jemm's sources, which fortified glibc aborts on.

set -e
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${OUT:-$HERE/out}
: "${WATCOM:=/home/user/open-watcom/open-watcom-v2/rel}"
: "${JEMM_SRC:=/home/user/baron-von-riedesel/jemm}"
: "${JWASM_SRC:=/home/user/baron-von-riedesel/jwasm}"

[ -f "$OUT/boot.img" ] || { echo "build out/boot.img first (build_testbed.sh)"; exit 1; }
TOOLS="$OUT/emm-tools"
mkdir -p "$TOOLS/jwasm"
if [ ! -x "$TOOLS/jwasm/jwasm" ]; then
    make -s -C "$JWASM_SRC" -f GccUnix.mak OUTD="$TOOLS/jwasm" \
        extra_c_flags="-DNDEBUG -O2 -U_FORTIFY_SOURCE -D_FORTIFY_SOURCE=0" > /dev/null 2>&1
fi
printf '#!/bin/sh\nexec "%s/binl64/wlink" "$@"\n' "$WATCOM" > "$TOOLS/jwlink"
chmod +x "$TOOLS/jwlink"
mkdir -p "$TOOLS/bin" && ln -sf "$TOOLS/jwasm/jwasm" "$TOOLS/bin/jwasm" && ln -sf "$TOOLS/jwlink" "$TOOLS/bin/jwlink"

# Jemm's sources include their files in lower case; the files are upper case.
for d in src Include; do
    (cd "$JEMM_SRC/$d" && for f in *; do
        l=$(echo "$f" | tr 'A-Z' 'a-z')
        if [ "$f" != "$l" ] && [ ! -e "$l" ]; then ln -s "$f" "$l"; fi
    done)
done
(cd "$JEMM_SRC" && PATH="$TOOLS/bin:$PATH" WATCOM="$WATCOM" make -s -f Linux.mak > /dev/null)

cp "$OUT/boot.img" "$OUT/boot-emm.img"
mcopy -o -i "$OUT/boot-emm.img" "$JEMM_SRC/build/JEMMEX/JEMMEX.EXE" ::/JEMMEX.EXE
printf 'FILES=20\r\nBUFFERS=20\r\nDEVICE=JEMMEX.EXE\r\nSHELL=COMMAND.COM /E:512 /P\r\n' > "$TOOLS/FDCONFIG.SYS"
mcopy -o -i "$OUT/boot-emm.img" "$TOOLS/FDCONFIG.SYS" ::/FDCONFIG.SYS
echo "Wrote $OUT/boot-emm.img (FreeDOS + JemmEx)"
