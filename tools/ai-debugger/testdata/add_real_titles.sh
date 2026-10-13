#!/bin/bash
# Copy two real titles from their public GitHub releases onto a FAT disk or
# floppy image (mtools), for testing the debugger on software it wasn't
# written for:
#
#   C:\SR      Future Crew's Second Reality (1993; public domain since 2013,
#              github.com/mtuomi/SecondReality). The release holds the loader
#              (MAIN/2NDFIX.EXE, run as SECOND.EXE) without the parts archive
#              the original had appended to it, so the parts go in SR\DATA as
#              loose files, which the loader also accepts. Needs a 386+, 570 KB
#              free conventional memory, and EMS for Sound Blaster music.
#   C:\WOLF3D  id Software's Wolfenstein 3D WOLF3D.EXE and its Borland map
#              (github.com/id-Software/wolf3d). The game data isn't released,
#              so it stops at its data-file check ("NO WOLFENSTEIN 3-D DATA
#              FILES to be found!"); the map is for load_symbols.
#
# Usage: add_real_titles.sh IMAGE [OFFSET]   (OFFSET: the FAT partition's byte
# offset in a hard disk image, e.g. $((63*512)); 0 or none for a floppy).
# The emulator must not be running on IMAGE while it is changed.

set -e
IMG=${1:?usage: $0 IMAGE [OFFSET]}
OFF=${2:-0}
HERE=$(cd "$(dirname "$0")" && pwd)
SRC=${SRC:-$HERE/out/real-titles}
T="$IMG@@$OFF"

fetch() {  # repo dir paths...: a shallow, partial clone with just these paths
    local repo=$1 dir=$2
    shift 2
    [ -d "$SRC/$dir/.git" ] || git clone -q --depth 1 --filter=blob:none --no-checkout "https://github.com/$repo" "$SRC/$dir"
    git -C "$SRC/$dir" checkout -q HEAD -- "$@"
}
mkdir -p "$SRC"
fetch mtuomi/SecondReality SecondReality MAIN/2NDFIX.EXE MAIN/DATA
fetch id-Software/wolf3d wolf3d WOLFSRC/WOLF3D.EXE WOLFSRC/WOLF3D.MAP

mmd -D s -i "$T" ::/SR ::/SR/DATA ::/WOLF3D 2>/dev/null || true  # -D s: don't ask about existing ones
mcopy -o -i "$T" "$SRC/SecondReality/MAIN/2NDFIX.EXE" ::/SR/SECOND.EXE
mcopy -o -i "$T" "$SRC/SecondReality/MAIN/DATA/REALITY.FC" ::/SR/REALITY.FC
for f in "$SRC"/SecondReality/MAIN/DATA/*; do
    [ "$(basename "$f")" = REALITY.FC ] || mcopy -o -i "$T" "$f" "::/SR/DATA/$(basename "$f")"
done
mcopy -o -i "$T" "$SRC/wolf3d/WOLFSRC/WOLF3D.EXE" ::/WOLF3D/WOLF3D.EXE
echo "Copied to $IMG. The Wolfenstein 3D map: $SRC/wolf3d/WOLFSRC/WOLF3D.MAP"
