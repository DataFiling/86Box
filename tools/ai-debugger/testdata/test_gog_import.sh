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
# Games without a DOSBox configuration, and CDs on their own:
#   6. A zip of a DOS game in its own folder, with an installer, a DOS
#      extender, a Windows DOSBox and its launcher, and a Mac's __MACOSX.
#   7. A 7z (with 7-Zip or bsdtar) of a game whose batch file starts it,
#      with a setup program, and its CD as a cue sheet with a raw MODE1 data
#      track and an OGG track, in a folder of its own.
#   8. An ISO on its own: into the drive, its installer reported.
#   9. A raw MODE2 image without a cue sheet: one is written.
#  10. A zip holding GOG's Linux installer (case 3's).
#  11. A folder holding only an installer, and --run.
#  12. A 7z with a "../" path (made with bsdtar): refused.
# To run an import in a VM, give it the VM's disk image instead.

set -e
HERE=$(cd "$(dirname "$0")" && pwd)
SRC=${SRC:-$HERE/out/real-titles}
W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
fail() { echo "FAIL: $*"; exit 1; }
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

cp "$W/86box.cfg" "$W/86box.cfg.5"

# ISO images and raw (2352-byte sector) images, written here: an ISO 9660
# volume with one root folder level, as DOS's CD extensions read it.
cat > "$W/mkiso.py" << 'EOF'
import struct, sys


def both(fmt, v):
    return struct.pack("<" + fmt, v) + struct.pack(">" + fmt, v)


def record(name, lba, size, flags):
    n = len(name)
    r = bytes([0, 0]) + both("I", lba) + both("I", size) + bytes(7) + bytes([flags, 0, 0]) + both("H", 1) + \
        bytes([n]) + name
    r += bytes(len(r) % 2)
    return bytes([len(r)]) + r[1:]


def iso(label, files):
    """files: [(name, data)] in the root folder."""
    data, lba = [], 21
    for name, body in files:
        data.append((name, lba, body))
        lba += (len(body) + 2047) // 2048 or 1
    root = record(b"\0", 20, 2048, 2) + record(b"\1", 20, 2048, 2)
    for name, at, body in data:
        root += record(name.encode() + b";1", at, len(body), 0)
    pvd = bytearray(2048)
    pvd[0:8] = b"\x01CD001\x01\x00"
    pvd[8:72] = b" " * 32 + label.encode().ljust(32)
    pvd[80:88] = both("I", lba)
    pvd[120:132] = both("H", 1) + both("H", 1) + both("H", 2048)
    pvd[132:140] = both("I", 10)
    pvd[140:144] = struct.pack("<I", 18)
    pvd[148:152] = struct.pack(">I", 19)
    pvd[156:190] = record(b"\0", 20, 2048, 2)
    pvd[881] = 1
    out = bytearray(16 * 2048) + pvd + b"\xffCD001\x01".ljust(2048, b"\0")
    out += (bytes([1, 0]) + struct.pack("<IH", 20, 1) + b"\0\0").ljust(2048, b"\0")
    out += (bytes([1, 0]) + struct.pack(">IH", 20, 1) + b"\0\0").ljust(2048, b"\0")
    out += root.ljust(2048, b"\0")
    for name, at, body in data:
        out += body.ljust(((len(body) + 2047) // 2048 or 1) * 2048, b"\0")
    return bytes(out)


def raw(image, mode):
    out = bytearray()
    for n in range(len(image) // 2048):
        a = n + 150
        bcd = lambda v: (v // 10) << 4 | v % 10
        out += b"\0" + b"\xff" * 10 + b"\0" + bytes([bcd(a // 4500), bcd(a // 75 % 60), bcd(a % 75), mode])
        out += (bytes(8) if mode == 2 else b"") + image[n * 2048:(n + 1) * 2048]
        out += bytes(2352 - len(out) % 2352) if len(out) % 2352 else b""
    return bytes(out)


path, label, mode = sys.argv[1], sys.argv[2], int(sys.argv[3])
files = [(a, open(b, "rb").read()) for a, b in (f.split("=", 1) for f in sys.argv[4:])]
image = iso(label, files)
open(path, "wb").write(raw(image, mode) if mode else image)
EOF
EXE="$SRC/wolf3d/WOLFSRC/WOLF3D.EXE"
SEVEN=$(command -v 7z || command -v 7zz || command -v 7za || command -v bsdtar || true)

# 6. A zip of a DOS game in its own folder.
G6="$W/zip/KEEN4"
mkdir -p "$G6" "$W/zip/__MACOSX/KEEN4"
cp "$EXE" "$G6/KEEN4E.EXE"
head -c 3000 "$EXE" > "$G6/INSTALL.EXE"
head -c 2000 "$EXE" > "$G6/DOS4GW.EXE"
echo story > "$G6/STORY.TXT"
python3 -c '
import struct, sys
b = bytearray(256); b[0:2] = b"MZ"; struct.pack_into("<HI", b, 0x18, 0x40, 0); struct.pack_into("<I", b, 0x3C, 0x80)
b[0x80:0x84] = b"PE\0\0"; open(sys.argv[1], "wb").write(b)' "$G6/DOSBox.exe"
printf 'DOSBox.exe -conf keen.cfg -noconsole\r\n' > "$G6/Play.bat"
echo mac > "$W/zip/__MACOSX/KEEN4/._KEEN4E.EXE"
(cd "$W/zip" && python3 -m zipfile -c "$W/Commander Keen 4 (1991)(id Software).zip" KEEN4 __MACOSX)
python3 "$HERE/../gog_import.py" "$W/Commander Keen 4 (1991)(id Software).zip" "$HD" | tee "$W/zip.out"

# 7. A 7z of a game with its CD in a folder of its own.
if [ -n "$SEVEN" ]; then
    G7="$W/7z/SPACEGM"
    mkdir -p "$G7" "$W/7z/CD"
    cp "$EXE" "$G7/SPACE.EXE"
    head -c 3000 "$EXE" > "$G7/SETUP.EXE"
    printf '@echo off\r\nspace.exe /nojoy\r\n' > "$G7/GO.BAT"
    echo track > "$W/7z/levels.dat"
    python3 "$W/mkiso.py" "$W/7z/CD/space.bin" SPACECD 1 "LEVELS.DAT=$W/7z/levels.dat"
    rm "$W/7z/levels.dat"
    printf 'OggS' > "$W/7z/CD/Track 02.ogg"   # not a real track: its format can't be told, so it stays
    printf 'FILE "SPACE.BIN" BINARY\r\n  TRACK 01 MODE1/2352\r\n    INDEX 01 00:00:00\r\nFILE "Track 02.ogg" OGG\r\n  TRACK 02 AUDIO\r\n    INDEX 01 00:00:00\r\n' > "$W/7z/CD/space.cue"
    case "$SEVEN" in
        *bsdtar) (cd "$W/7z" && bsdtar --format 7zip -cf "$W/Space Game.7z" SPACEGM CD) ;;
        *) (cd "$W/7z" && "$SEVEN" a -bd "$W/Space Game.7z" SPACEGM CD > /dev/null) ;;
    esac
    python3 "$HERE/../gog_import.py" "$W/Space Game.7z" "$HD" | tee "$W/7z.out"
    cp "$W/86box.cfg" "$W/86box.cfg.7"
else
    echo "(no 7z or bsdtar: case 7 skipped)"
fi

# 8. An ISO on its own.
head -c 4000 "$EXE" > "$W/install.bin"
echo readme > "$W/readme.txt"
python3 "$W/mkiso.py" "$W/Big Game CD.iso" BIGGAME 0 "INSTALL.EXE=$W/install.bin" "README.TXT=$W/readme.txt"
python3 "$HERE/../gog_import.py" "$W/Big Game CD.iso" "$HD" | tee "$W/iso.out"
cp "$W/86box.cfg" "$W/86box.cfg.8"

# 9. A raw MODE2 image without a cue sheet.
python3 "$W/mkiso.py" "$W/rawgame.bin" RAWGAME 2 "SETUP.EXE=$W/install.bin"
python3 "$HERE/../gog_import.py" "$W/rawgame.bin" "$HD" | tee "$W/raw.out"
cp "$W/86box.cfg" "$W/86box.cfg.9"

# 10. A zip holding GOG's Linux installer.
mkdir -p "$W/backup"
cp "$W/wolf.sh" "$W/backup/gog_wolfenstein_3d_1.4.sh"
(cd "$W/backup" && python3 -m zipfile -c "$W/backup.zip" gog_wolfenstein_3d_1.4.sh)
python3 "$HERE/../gog_import.py" "$W/backup.zip" "$HD" | tee "$W/nested.out"

# 11. A folder holding only an installer; and --run.
mkdir -p "$W/SHAREW"
cp "$W/install.bin" "$W/SHAREW/INSTALL.EXE"
echo part > "$W/SHAREW/GAME.1"
python3 "$HERE/../gog_import.py" "$W/SHAREW" "$HD" | tee "$W/installer.out"
python3 "$HERE/../gog_import.py" "$W/SHAREW" "$HD" --run "INSTALL.EXE C:\\SHAREW" > /dev/null

# 12. A 7z with a "../" path.
if command -v bsdtar > /dev/null; then
    mkdir -p "$W/evil/in"
    echo evil > "$W/evil/x.txt"
    (cd "$W/evil/in" && bsdtar --format 7zip -P -cf "$W/evil.7z" ../x.txt)
    out=$(python3 "$HERE/../gog_import.py" "$W/evil.7z" "$HD" 2>&1) && fail "a 7z with ../ should be refused"
    case "$out" in *outside*|*"Path contains"*|*".."*) ;; *) echo "FAIL: ../ refused with: $out"; exit 1 ;; esac
fi

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
grep -qx 'cdrom_01_image_path = cd/FOLDERCD/CD' "$W/86box.cfg.5" || fail "the VM's drive should hold the folder cd/FOLDERCD/CD"
[ -f "$W/cd/FOLDERCD/CD/DATA/LEVEL1.DAT" ] || fail "the CD folder should be copied whole"
mdir -b -i "$HD@@32256" ::/FOLDERCD | grep -q "/CD" && fail "the CD folder was copied to C:"
grep -q "as E:" "$W/folder.out" || fail "the E: letter should be reported"
# 6: KEEN4 as it is, the game picked, no Mac files, the Windows programs not offered.
[ "$(mdir -b -i "$HD@@32256" ::/KEEN4 | wc -l)" = 6 ] || fail "C:\\KEEN4 should hold the zip's 6 files"
mdir -b -i "$HD@@32256" ::/ | grep -qi MACOSX && fail "__MACOSX was copied"
mtype -i "$HD@@32256" ::/KEEN4.BAT | tr -d '\r' | tail -2 | tr '\n' ' ' | grep -qx 'cd \\KEEN4 KEEN4E.EXE ' ||
    fail "KEEN4.BAT should run KEEN4E.EXE in C:\\KEEN4"
grep -q "Commander Keen 4:" "$W/zip.out" || fail "the game's name should come from the archive's"
grep -qi "dosbox\|play.bat" <(grep "Other programs" "$W/zip.out") && fail "DOSBox and its launcher were offered"
# 7: the batch file that runs the game, the CD in the drive, nothing of it on C:.
if [ -n "$SEVEN" ]; then
    mtype -i "$HD@@32256" ::/SPACEGM.BAT | tr -d '\r' | tail -1 | grep -qx GO.BAT || fail "SPACEGM.BAT should run GO.BAT"
    grep -q "SETUP.EXE and pick Sound Blaster" "$W/7z.out" || fail "the setup program should be named for the sound"
    grep -qx 'cdrom_01_image_path = cd/SPACEGM/space.cue' "$W/86box.cfg.7" || fail "the 7z's CD should be in the drive"
    tr -d '\r' < "$W/cd/SPACEGM/space.cue" | grep -qx 'FILE "space.bin" BINARY' || fail "the cue should name space.bin"
    tr -d '\r' < "$W/cd/SPACEGM/space.cue" | grep -qx 'FILE "Track 02.ogg" WAVE' || fail "the OGG track should be WAVE"
    mdir -b -i "$HD@@32256" ::/ | grep -qi "/CD/\|space.bin" && fail "the CD was copied to C:"
    [ -z "$(ls -A "$(dirname "$HD")" | grep '^\.import-')" ] || fail "the unpacking folder was left behind"
fi
# 8: the ISO in the drive, its installer reported, nothing on C:.
grep -qx 'cdrom_01_image_path = cd/BIGGAMEC/Big Game CD.iso' "$W/86box.cfg.8" || fail "the ISO should be in the drive"
grep -q "On the CD (BIGGAME): INSTALL.EXE, README.TXT" "$W/iso.out" || fail "the CD's files should be listed"
grep -q "type D: and then INSTALL" "$W/iso.out" || fail "the installer should be named"
mdir -b -i "$HD@@32256" ::/ | grep -qi "BIGGAME" && fail "an ISO alone should put nothing on C:"
# 9: a cue sheet for the raw image.
grep -qx 'cdrom_01_image_path = cd/RAWGAME/rawgame.cue' "$W/86box.cfg.9" || fail "the raw image should get a cue sheet"
tr -d '\r' < "$W/cd/RAWGAME/rawgame.cue" | grep -qx '  TRACK 01 MODE2/2352' || fail "the raw image is MODE2/2352"
grep -q "type D: and then SETUP" "$W/raw.out" || fail "the raw image's SETUP should be named"
# 10: the installer inside the zip imported.
grep -q "holds GOG's installer gog_wolfenstein_3d_1.4.sh" "$W/nested.out" || fail "the zip's installer should be used"
grep -q "Wolfenstein 3D: 2 files" "$W/nested.out" || fail "the zip's installer's game should be imported"
# 11: an installer only: no batch file until --run.
grep -qF "run C:\\SHAREW\\INSTALL.EXE" "$W/installer.out" || fail "the installer should be named"
mtype -i "$HD@@32256" ::/SHAREW.BAT | tr -d '\r' | tail -1 | grep -qx 'INSTALL.EXE C:\\SHAREW' ||
    fail "--run should write SHAREW.BAT"
echo "PASS"
