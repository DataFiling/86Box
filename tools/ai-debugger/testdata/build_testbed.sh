#!/bin/bash
# Build the debugger test floppy: FreeDOS kernel and FreeCOM, CuteMouse, the
# test programs in games/ and DOS/4GW, as testdata/out/boot.img.
#
# Prerequisites (see testdata/README.md for how to get and build them):
#   WATCOM     Open Watcom v2 release tree (rel/), with binl64 tools
#   FDOS_KERNEL_DIR  FreeDOS kernel repository, built (bin/kernel.sys)
#   FREECOM    FreeCOM repository, built (command.com)
#   CTMOUSE_SRC      FDOS/mouse repository (CuteMouse source)
#   JWASM      JWasm assembler binary
#   nasm, mtools

set -e
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${OUT:-$HERE/out}
: "${WATCOM:=/home/user/open-watcom/open-watcom-v2/rel}"
: "${FDOS_KERNEL_DIR:=/home/user/fdos/kernel}"
: "${FREECOM:=/home/user/fdos/freecom}"
: "${CTMOUSE_SRC:=/home/user/fdos/mouse}"
: "${JWASM:=/home/user/baron-von-riedesel/jwasm/build/GccUnixR/jwasm}"
export WATCOM PATH="$WATCOM/binl64:$PATH" INCLUDE="$WATCOM/h" FDOS_KERNEL_DIR

mkdir -p "$OUT"
cd "$OUT"

# Test programs (the build lines match the comments in each source file).
G=$HERE/games
wcl    -zq -ms -bt=dos -0 -os -fm=txtgame.map -fe=txtgame.exe "$G/txtgame.c"
wcl    -zq -ms -bt=dos -2 -os -fm=vgagame.map -fe=vgagame.exe "$G/vgagame.c"
wcl    -zq -ms -bt=dos -0 -os -fe=mousetst.exe "$G/mousetst.c"
wcl386 -zq -bt=dos -l=dos4g -3r -os -fm=pmgame.map -fe=pmgame.exe "$G/pmgame.c"
wcl386 -zq -bt=dos -l=dos4g -3r -fp3 -ox -fe=bench.exe "$G/bench.c"
cp "$WATCOM/binw/dos4gw.exe" DOS4GW.EXE

# CuteMouse 2.1b4. Its source targets TASM/older JWasm; JWasm 2.21 needs:
# case-insensitive include names, "<>" instead of "?" to declare record and
# structure variables, and loading the mouse-type string pointer in one place
# where the source takes the OFFSET of an indexed table entry. Linking through
# OMF keeps the .COM layout of the original exe2bin build.
rm -rf ctm && cp -r "$CTMOUSE_SRC" ctm && cd ctm
for d in asmlib/*/; do d=${d%/}; u=$(dirname "$d")/$(basename "$d" | tr a-z A-Z); [ "$u" != "$d" ] && ln -sfn "$(basename "$d")" "$u"; done
find asmlib -type f | while read -r f; do
    b=$(basename "$f"); n=${b%.*}; e=${b##*.}; U=$(echo "$n" | tr a-z A-Z).$e
    [ "$U" != "$b" ] && [ ! -e "$(dirname "$f")/$U" ] && ln -s "$b" "$(dirname "$f")/$U"
done
sed -i -E 's/^((HW_list|KBD_flags|VIDEO_control|VIDEO_switches|VGA_options|KBD_status)[ \t]+[A-Z]+) \?/\1 <>/' asmlib/bios/area0.def
sed -i -E '220,275s/\b(POINT|BUTTLASTSTATE)([ \t]+)\?/\1\2<>/; s/BUTTLASTSTATE <>,\?,\?/BUTTLASTSTATE <>,<>,<>/' ctmouse.asm
sed -i 's/^\t\tsay\tS_mousetype\[si\]/\t\tmov\tdi,S_mousetype[si]\n\t\tcall\tsayASCIIZ/' ctmouse.asm
"$JWASM" -q -mt -Fo=ctmouse.obj ctmouse.asm
wlink format dos com name ../CTMOUSE.COM file ctmouse.obj option quiet
cd .. && rm -rf ctm

# Boot files.
printf 'FILES=20\r\nBUFFERS=20\r\nSHELL=COMMAND.COM /E:512 /P\r\n' > FDCONFIG.SYS
printf '@ECHO OFF\r\nPROMPT $P$G\r\nCTMOUSE\r\nECHO READY\r\n' > AUTOEXEC.BAT

"$HERE/build_floppy.sh" "$OUT/boot.img" "$FDOS_KERNEL_DIR/bin/kernel.sys" "$FREECOM/command.com" \
    FDCONFIG.SYS AUTOEXEC.BAT CTMOUSE.COM txtgame.exe vgagame.exe pmgame.exe mousetst.exe bench.exe DOS4GW.EXE
