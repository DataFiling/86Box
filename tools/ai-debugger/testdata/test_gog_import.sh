#!/bin/bash
# Test gog_import.py without a GOG installer: build two folders laid out as
# GOG installs its DOS games (goggame-*.info play task, DOSBox configs that
# mount ".." as C:, a DOSBOX folder) around real programs from
# add_real_titles.sh's downloads, import them onto a scratch floppy-sized
# image, and check what lands there.
#   1. Second Reality, run from a subfolder (mount C, c:, cd SR, SECOND.EXE).
#   2. Wolfenstein 3D's EXE at the root, with a CD image mounted as D:.
#   3. A Linux installer: a shell script with a zip appended, the game under
#      data/noarch/data and the configs in data/noarch, without
#      goggame-*.info (modelled on descriptions of GOG's Linux installers,
#      not checked against a real one), imported onto a partitioned hard
#      disk image without --offset.
#   4. A game whose CD is a GOG-style cue sheet (game.ins naming GAME.GOG,
#      with the file in lower case, a CD audio track at 22 kHz mono, an "OGG"
#      file type 86Box doesn't know) plus a second disc, imported onto that
#      disk with an 86box.cfg next to it: the CD goes into the VM's drive.
#   5. A game whose CD is a folder DOSBox mounts as a CD-ROM, as E:.
# To run an import in a VM, give it the VM's disk image instead.

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
printf 'FILE "game.gog" BINARY\r\n  TRACK 01 MODE1/2352\r\n    INDEX 01 00:00:00\r\n' > "$G2/game.ins"
head -c 4096 /dev/zero > "$G2/game.gog"   # the CD's data track, which the cue sheet names
echo manual > "$G2/manual.pdf"
printf '[sblaster]\nsbtype=sb16\nirq=5\n[dos]\nems=false\n[autoexec]\n# Lines in this section will be run at startup.\nmount C ".."\nimgmount D "..\\game.ins" -t iso\nc:\nWOLF3D.EXE\nexit\n' > "$G2/dosboxWolf.conf"
printf '{"name": "Wolfenstein 3D", "playTasks": [{"isPrimary": true, "arguments": "-conf \\"..\\\\dosboxWolf.conf\\"", "workingDir": "DOSBOX"}]}\n' > "$G2/goggame-2.info"

IMG="$W/test.img"
dd if=/dev/zero of="$IMG" bs=1M count=8 status=none
mformat -i "$IMG" -T 16384 -h 2 -s 32 ::
python3 "$HERE/../gog_import.py" "$G" "$IMG"
python3 "$HERE/../gog_import.py" "$G2" "$IMG"
# Again (same name: same batch file), and a game whose name starts the same.
python3 "$HERE/../gog_import.py" "$G" "$IMG" > /dev/null
sed -i 's/"Second Reality"/"Second Reality II"/' "$G/goggame-1.info"
python3 "$HERE/../gog_import.py" "$G" "$IMG" --dir SR2 > /dev/null

# 3. The Linux installer, onto a 20 MB disk with one FAT16 partition at
# sector 63, as make_game_vm.sh makes.
N="$W/linux/data/noarch"
mkdir -p "$N/data/SUB" "$N/support" "$N/docs"
cp "$SRC/wolf3d/WOLFSRC/WOLF3D.EXE" "$N/data/"
echo data > "$N/data/SUB/LEVELS.DAT"
printf '#!/bin/sh\ncd "$(dirname "$0")"\ndosbox -conf dosboxWolf.conf -conf dosboxWolf_single.conf\n' > "$N/start.sh"
printf 'Wolfenstein 3D\n1.4\n' > "$N/gameinfo"
echo icon > "$N/support/icon.png"
printf '[dosbox]\nmemsize=16\n' > "$N/dosboxWolf.conf"
printf '[autoexec]\n@echo off\nmount C "data"\nc:\nWOLF3D.EXE\nexit\n' > "$N/dosboxWolf_single.conf"
printf '[autoexec]\nmount C "data"\nc:\nSETUP.EXE\nexit\n' > "$N/dosboxWolf_setup.conf"
printf '#!/bin/sh\n# makeself stub\nexit 0\n' > "$W/wolf.sh"
(cd "$W/linux" && python3 -m zipfile -c "$W/wolf.zip" data)
cat "$W/wolf.zip" >> "$W/wolf.sh"
HD="$W/hd.img"
truncate -s $((40 * 16 * 63 * 512)) "$HD"
python3 -c '
import struct, sys
mbr = bytearray(512)
mbr[446:462] = bytes([0x80, 1, 1, 0, 0x06, 15, 63, 39]) + struct.pack("<II", 63, 40 * 16 * 63 - 63)
mbr[510:] = b"\x55\xaa"
open(sys.argv[1], "r+b").write(mbr)' "$HD"
mformat -i "$HD@@32256" -T $((40 * 16 * 63 - 63)) -h 16 -s 63 -H 63 ::
python3 "$HERE/../gog_import.py" "$W/wolf.sh" "$HD"

# 4. A CD game, imported onto a VM's disk: 86box.cfg next to it, with a drive.
printf '[Floppy and CD-ROM drives]\ncdrom_01_parameters = 1, atapi\ncdrom_01_ide_channel = 1:0\n' > "$W/86box.cfg"
G4="$W/cdgame"
mkdir -p "$G4/DOSBOX" "$G4/GAME"
cp "$SRC/wolf3d/WOLFSRC/WOLF3D.EXE" "$G4/GAME/GAME.EXE"
head -c 51200 /dev/urandom > "$G4/game.gog"
python3 -c '
import struct, sys, wave
for name, ch, rate in (("track02.wav", 2, 44100), ("Track03.WAV", 1, 22050)):
    w = wave.open(sys.argv[1] + "/" + name, "wb"); w.setnchannels(ch); w.setsampwidth(2); w.setframerate(rate)
    w.writeframes(b"\0\0" * ch * rate); w.close()' "$G4"
printf 'FILE "GAME.GOG" BINARY\r\n  TRACK 01 MODE1/2048\r\n    INDEX 01 00:00:00\r\nFILE "track02.wav" WAVE\r\n  TRACK 02 AUDIO\r\n    INDEX 01 00:00:00\r\nFILE "Track03.WAV" OGG\r\n  TRACK 03 AUDIO\r\n    INDEX 01 00:00:00\r\n' > "$G4/game.ins"
head -c 4096 /dev/urandom > "$G4/disc2.iso"
printf '[autoexec]\nmount C ".."\nimgmount D "..\\game.ins" "..\\disc2.iso" -t iso\nc:\ncd GAME\nGAME.EXE\n' > "$G4/dosbox_cd.conf"
printf '{"name": "CD Game", "playTasks": [{"isPrimary": true, "arguments": "-conf \\"..\\\\dosbox_cd.conf\\"", "workingDir": "DOSBOX"}]}\n' > "$G4/goggame-4.info"
python3 "$HERE/../gog_import.py" "$G4" "$HD" | tee "$W/cd.out"
cp "$W/86box.cfg" "$W/86box.cfg.4"

# 5. A folder DOSBox mounts as a CD-ROM, as E:.
G5="$W/cdfolder"
mkdir -p "$G5/DOSBOX" "$G5/CD/DATA"
cp "$SRC/wolf3d/WOLFSRC/WOLF3D.EXE" "$G5/RUN.EXE"
echo level > "$G5/CD/DATA/LEVEL1.DAT"
printf '[autoexec]\nmount C ".."\nmount E "..\\CD" -t cdrom\nc:\nRUN.EXE\n' > "$G5/dosbox_f.conf"
printf '{"name": "Folder CD", "playTasks": [{"isPrimary": true, "arguments": "-conf \\"..\\\\dosbox_f.conf\\"", "workingDir": "DOSBOX"}]}\n' > "$G5/goggame-5.info"
python3 "$HERE/../gog_import.py" "$G5" "$HD" | tee "$W/folder.out"

fail() { echo "FAIL: $*"; exit 1; }
[ "$(mdir -b -i "$IMG" ::/SR/DATA | wc -l)" = 79 ] || fail "SR\\DATA should hold 79 parts"
mtype -i "$IMG" ::/SECONDRE.BAT | tr -d '\r' | grep -qx "cd SR" || fail "SECONDRE.BAT should cd into SR"
mdir -b -i "$IMG" ::/ | grep -qi "unins000\|gog.ico\|dosbox" && fail "GOG's own files were copied"
[ "$(mdir -b -i "$IMG" ::/WOLFENST)" = "::/WOLFENST/WOLF3D.EXE" ] || fail "WOLFENST should hold only WOLF3D.EXE (no CD image, no manual)"
mtype -i "$IMG" ::/WOLFENST.BAT | tr -d '\r' | grep -qx 'cd \\WOLFENST' || fail "WOLFENST.BAT should cd into its folder"
mtype -i "$IMG" ::/WOLFENST.BAT | grep -q '#' && fail "a config comment became a command"
[ "$(mdir -b -i "$IMG" ::/ | grep -ci 'SECONDR.*\.BAT')" = 2 ] || fail "Second Reality II should get SECONDR1.BAT, beside SECONDRE.BAT"
mtype -i "$IMG" ::/SECONDR1.BAT | tr -d '\r' | grep -qx 'cd \\SR2' || fail "SECONDR1.BAT should start the second game"
[ "$(mdir -b -i "$HD@@32256" ::/WOLFENST | sort | tr '\n' ' ')" = "::/WOLFENST/SUB/ ::/WOLFENST/WOLF3D.EXE " ] ||
    fail "the Linux installer's game should be in WOLFENST on the partition"
mtype -i "$HD@@32256" ::/WOLFENST.BAT | tr -d '\r' | grep -q SETUP && fail "the setup config was used"
out=$(python3 "$HERE/../gog_import.py" "$G" "$IMG" --offset 512 2>&1) && fail "a wrong --offset should fail"
case "$out" in *Traceback*|"") fail "a wrong --offset should fail with mtools' error: $out" ;; esac
# 4: the cue sheet as 86Box needs it, the second disc in the recent images.
C4="$W/cd/CDGAME"
grep -qx 'cdrom_01_image_path = cd/CDGAME/game.cue' "$W/86box.cfg.4" || fail "the VM's drive should hold cd/CDGAME/game.cue"
grep -qx 'cdrom_01_image_history_01 = cd/CDGAME/disc2.iso' "$W/86box.cfg.4" || fail "disc 2 should be in the recent images"
tr -d '\r' < "$C4/game.cue" | grep -qx 'FILE "game.gog" BINARY' || fail "the cue should name game.gog as it is on disk"
[ "$(stat -c %h "$C4/game.gog")" = 2 ] || fail "the data track should be a hard link, not a copy"
if command -v ffmpeg > /dev/null; then
    tr -d '\r' < "$C4/game.cue" | grep -qx 'FILE "Track03.wav" WAVE' || fail "track 3 should be converted to Track03.wav"
    python3 -c 'import sys, wave; w = wave.open(sys.argv[1]); sys.exit((w.getnchannels(), w.getframerate()) != (2, 44100))' \
        "$C4/Track03.wav" || fail "Track03.wav should be 44.1 kHz stereo"
else
    grep -q "Track03.WAV is 1 channels at 22050 Hz" "$W/cd.out" || fail "track 3's format should be reported"
fi
mdir -b -i "$HD@@32256" ::/GAME | grep -qi "gog\|ins\|iso" && fail "the CD's files were copied to C:"
# 5: the folder as the CD, not on C:, and the letter noted.
grep -qx 'cdrom_01_image_path = cd/FOLDERCD/CD' "$W/86box.cfg" || fail "the VM's drive should hold the folder cd/FOLDERCD/CD"
[ -f "$W/cd/FOLDERCD/CD/DATA/LEVEL1.DAT" ] || fail "the CD folder should be copied whole"
mdir -b -i "$HD@@32256" ::/FOLDERCD | grep -q "/CD" && fail "the CD folder was copied to C:"
grep -q "as E:" "$W/folder.out" || fail "the E: letter should be reported"
echo "PASS"
