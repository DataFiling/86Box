#!/bin/bash
# Test gog_import.py without a GOG installer: build two folders laid out as
# GOG installs its DOS games (goggame-*.info play task, DOSBox configs that
# mount ".." as C:, a DOSBOX folder) around real programs from
# add_real_titles.sh's downloads, import them onto a scratch floppy-sized
# image, and check what lands there.
#   1. Second Reality, run from a subfolder (mount C, c:, cd SR, SECOND.EXE).
#   2. Wolfenstein 3D's EXE at the root, with a CD image mounted as D:.
# To run an import in a VM, give it a VM disk image instead (with --offset).

set -e
HERE=$(cd "$(dirname "$0")" && pwd)
SRC=${SRC:-$HERE/out/real-titles}
W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
[ -f "$SRC/SecondReality/MAIN/2NDFIX.EXE" ] || { echo "run add_real_titles.sh first (it fetches into $SRC)"; exit 1; }

# 1. Second Reality in SR\, as GOG would ship it.
G="$W/sr"
mkdir -p "$G/DOSBOX" "$G/cloud_saves" "$G/SR/DATA"
cp "$SRC/SecondReality/MAIN/2NDFIX.EXE" "$G/SR/SECOND.EXE"
for f in "$SRC"/SecondReality/MAIN/DATA/*; do
    case "$(basename "$f")" in REALITY.FC) cp "$f" "$G/SR/" ;; *) cp "$f" "$G/SR/DATA/" ;; esac
done
printf '[dosbox]\nmemsize=16\n[sblaster]\nsbtype=sbpro2\nsbbase=220\nirq=7\ndma=1\n[dos]\nems=true\n' > "$G/dosbox_sr.conf"
printf '[autoexec]\n@echo off\nmount C ".."\nmount C "..\\cloud_saves" -t overlay\nc:\ncls\ncd SR\nSECOND.EXE\nexit\n' > "$G/dosbox_sr_single.conf"
printf '{"name": "Second Reality", "playTasks": [{"isPrimary": true, "path": "DOSBOX\\\\dosbox.exe", "arguments": "-conf \\"..\\\\dosbox_sr.conf\\" -conf \\"..\\\\dosbox_sr_single.conf\\" -noconsole", "workingDir": "DOSBOX"}]}\n' > "$G/goggame-1.info"
touch "$G/DOSBOX/dosbox.exe" "$G/unins000.exe" "$G/gog.ico"

# 2. Wolfenstein 3D at C:'s root, with a CD image as D:.
G2="$W/wolf"
mkdir -p "$G2/DOSBOX"
cp "$SRC/wolf3d/WOLFSRC/WOLF3D.EXE" "$G2/"
head -c 4096 /dev/zero > "$G2/game.ins"
echo manual > "$G2/manual.pdf"
printf '[sblaster]\nsbtype=sb16\nirq=5\n[dos]\nems=false\n[autoexec]\nmount C ".."\nimgmount D "..\\game.ins" -t iso\nc:\nWOLF3D.EXE\nexit\n' > "$G2/dosboxWolf.conf"
printf '{"name": "Wolfenstein 3D", "playTasks": [{"isPrimary": true, "arguments": "-conf \\"..\\\\dosboxWolf.conf\\"", "workingDir": "DOSBOX"}]}\n' > "$G2/goggame-2.info"

IMG="$W/test.img"
dd if=/dev/zero of="$IMG" bs=1M count=8 status=none
mformat -i "$IMG" -T 16384 -h 2 -s 32 ::
python3 "$HERE/../gog_import.py" "$G" "$IMG"
python3 "$HERE/../gog_import.py" "$G2" "$IMG"

fail() { echo "FAIL: $*"; exit 1; }
[ "$(mdir -b -i "$IMG" ::/SR/DATA | wc -l)" = 79 ] || fail "SR\\DATA should hold 79 parts"
mtype -i "$IMG" ::/SECONDRE.BAT | tr -d '\r' | grep -qx "cd SR" || fail "SECONDRE.BAT should cd into SR"
mdir -b -i "$IMG" ::/ | grep -qi "unins000\|gog.ico\|dosbox" && fail "GOG's own files were copied"
[ "$(mdir -b -i "$IMG" ::/WOLFENST)" = "::/WOLFENST/WOLF3D.EXE" ] || fail "WOLFENST should hold only WOLF3D.EXE (no CD image, no manual)"
mtype -i "$IMG" ::/WOLFENST.BAT | tr -d '\r' | grep -qx 'cd \\WOLFENST' || fail "WOLFENST.BAT should cd into its folder"
echo "PASS"
