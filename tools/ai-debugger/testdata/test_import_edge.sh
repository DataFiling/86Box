#!/bin/bash
# Test gog_import.py on games from other sources than GOG, and on archives
# built to break it: what downloads of DOS games look like (a game zip with
# its data next to the program's folder, title initials as the program's
# name, an SB16 setup folder, install floppies, an ISO with loose music
# tracks, a zip inside a zip), DOSBox configs from other packagers, and
# hostile archives (links, a FIFO, names DOS can't have, Deflate64). Needs
# mtools and Python; 7-Zip, bsdtar and ffmpeg cases are skipped without them.
# Programs are fake: MZ headers, never run.
#   1. Two games whose folders have the same name (KEEN): the second gets
#      KEEN1, and importing the first again reuses KEEN.
#   2. A game whose program is in BIN, with DATA and a CFG next to it: the
#      whole folder is copied and the batch file goes into BIN.
#   3. Title initials (AOE.EXE for "Aces Over Europe"), with a bigger
#      unrelated program, an installer and a setup program at the zip's root.
#   4. A Windows-made zip: backslashes, a name with a newline, CON, a name
#      FAT can't hold, a link entry: skipped and reported, the rest copied.
#   5. A Deflate64 zip (zipfile can't), unpacked by 7-Zip.
#   6. A 7z with a link to /etc/passwd and a FIFO, unpacked by bsdtar only:
#      neither reaches the disk, and nothing hangs.
#   7. An ISO with loose music (Track02.wav): a cue sheet is written.
#   8. Two install floppy images: unpacked into DISK1, DISK2; the batch file
#      runs DISK1's installer.
#   9. An SB16 folder of sound setup files: copied over the game's.
#  10. A zip holding only a zip (and a note): both unpacked.
#  11. --json: the report as JSON.
#  12. An 86Box running in the VM's folder: the import refuses.
#  13. A DOSBox config from another packager: "imgmount d -t iso" (option
#      first), "d:" then "cd \GAME" on the CD, a C: mount by absolute
#      Windows path; and a GOG config whose C: is missing still stops.
#  14. A CHD image (by its header) goes into the drive.

set -e
HERE=$(cd "$(dirname "$0")" && pwd)
IMPORT="$HERE/../gog_import.py"
W=$(mktemp -d)
trap '[ -n "$KEEP" ] || rm -rf "$W"' EXIT
fail() { echo "FAIL: $*"; exit 1; }
SEVEN=$(command -v 7zz || command -v 7z || command -v 7za || true)

# A VM: a 20 MB disk with one FAT16 partition at sector 63, a CD drive.
VM="$W/vm"
mkdir -p "$VM"
HD="$VM/hd.img"
truncate -s $((40 * 16 * 63 * 512)) "$HD"
python3 -c '
import struct, sys
mbr = bytearray(512)
mbr[446:462] = bytes([0x80, 1, 1, 0, 0x06, 15, 63, 39]) + struct.pack("<II", 63, 40 * 16 * 63 - 63)
mbr[510:] = b"\x55\xaa"
open(sys.argv[1], "r+b").write(mbr)' "$HD"
mformat -i "$HD@@32256" -T $((40 * 16 * 63 - 63)) -h 16 -s 63 -H 63 ::
printf '[Floppy and CD-ROM drives]\ncdrom_01_parameters = 1, atapi\ncdrom_01_ide_channel = 1:0\n' > "$VM/86box.cfg"
C="$HD@@32256"

# mz FILE [SIZE]: a fake DOS program.
mz() { python3 -c '
import sys
size = int(sys.argv[2]) if len(sys.argv) > 2 else 600
b = bytearray(size); b[0:2] = b"MZ"; b[0x18] = 0x1C
open(sys.argv[1], "wb").write(b)' "$@"; }
imp() { python3 "$IMPORT" "$@"; }

# 1. Two games with a KEEN folder.
for g in Alpha Beta; do
    mkdir -p "$W/$g/KEEN"
    mz "$W/$g/KEEN/KEEN.EXE"
    echo "$g" > "$W/$g/KEEN/WHO.TXT"
    (cd "$W/$g" && python3 -m zipfile -c "$W/$g Keen.zip" KEEN)
done
imp "$W/Alpha Keen.zip" "$HD" > "$W/1a.out"
imp "$W/Beta Keen.zip" "$HD" > "$W/1b.out"
imp "$W/Alpha Keen.zip" "$HD" > "$W/1c.out"
[ "$(mtype -i "$C" ::/KEEN/WHO.TXT | tr -d '\r')" = Alpha ] || fail "C:\\KEEN should still be Alpha's"
[ "$(mtype -i "$C" ::/KEEN1/WHO.TXT | tr -d '\r')" = Beta ] || fail "Beta should go into C:\\KEEN1"
grep -q 'copied to C:\\KEEN;' "$W/1c.out" || fail "importing Alpha again should reuse C:\\KEEN: $(cat "$W/1c.out")"

# 2. The program in BIN, data next to it.
mkdir -p "$W/2/SPACEGAM/BIN" "$W/2/SPACEGAM/DATA"
mz "$W/2/SPACEGAM/BIN/SPACE.EXE"
echo level > "$W/2/SPACEGAM/DATA/LEVEL1.DAT"
echo cfg > "$W/2/SPACEGAM/SPACE.CFG"
echo "downloaded from somewhere" > "$W/2/Info.nfo"
(cd "$W/2" && python3 -m zipfile -c "$W/Space Game.zip" SPACEGAM Info.nfo)
imp "$W/Space Game.zip" "$HD" > "$W/2.out"
mtype -i "$C" ::/SPACEGAM/DATA/LEVEL1.DAT > /dev/null || fail "DATA should be copied with the game"
mtype -i "$C" ::/SPACEGAM/SPACE.CFG > /dev/null || fail "SPACE.CFG should be copied"
mtype -i "$C" ::/SPACEGAM.BAT | tr -d '\r' | grep -qx 'cd \\SPACEGAM\\BIN' || fail "the batch file should go into BIN"
grep -q "Not copied (outside the game's folder): Info.nfo" "$W/2.out" || fail "the site's note should be reported"

# 3. Title initials.
mkdir -p "$W/3"
mz "$W/3/AOE.EXE" 3000
mz "$W/3/BIGTOOL.EXE" 90000
mz "$W/3/INSTALL.EXE" 120000
mz "$W/3/SETUP.EXE" 50000
(cd "$W/3" && python3 -m zipfile -c "$W/Aces Over Europe (1993)(Dynamix, Inc.) [!].zip" .)
imp "$W/Aces Over Europe (1993)(Dynamix, Inc.) [!].zip" "$HD" > "$W/3.out"
mtype -i "$C" ::/ACESOVER.BAT | tr -d '\r' | tail -1 | grep -qx 'AOE.EXE' || fail "AOE.EXE should start the game: $(cat "$W/3.out")"
grep -q "initials" "$W/3.out" || fail "the reason for AOE.EXE should be given"

# 4. A hostile, Windows-made zip.
python3 -c '
import stat, sys, zipfile
z = zipfile.ZipFile(sys.argv[1], "w")
z.writestr("WIN\\GAME.EXE", b"MZ" + bytes(598))
z.writestr("WIN\\DATA\\LEVEL.DAT", b"level")
z.writestr("WIN\\NL\nPath = etc.TXT", b"x")
z.writestr("WIN\\CON", b"x")
z.writestr("WIN\\WHAT?.TXT", b"x")
i = zipfile.ZipInfo("WIN\\LINK"); i.external_attr = (stat.S_IFLNK | 0o777) << 16
z.writestr(i, "/etc/passwd")
z.writestr("../../escape.txt", b"x")
z.close()' "$W/win.zip"
imp "$W/win.zip" "$HD" > "$W/4.out" 2>&1 || fail "the hostile zip should import: $(cat "$W/4.out")"
mtype -i "$C" ::/WIN/DATA/LEVEL.DAT > /dev/null || fail "backslashes should be folders"
mdir -b -i "$C" ::/WIN | grep -qi 'link\|con\|what' && fail "a link or a bad name reached C:"
[ -e "$W/escape.txt" ] || [ -e "$VM/escape.txt" ] && fail "../ escaped the unpacking folder"
grep -qi "^ok\|GAME.EXE" "$W/4.out" || fail "the game should be imported: $(cat "$W/4.out")"

# 5. Deflate64.
if [ -n "$SEVEN" ]; then
    mkdir -p "$W/5/D64"
    mz "$W/5/D64/D64.EXE"
    head -c 100000 /dev/zero | tr '\0' a > "$W/5/D64/BIG.DAT"
    (cd "$W/5" && "$SEVEN" a -bd -tzip -mm=Deflate64 "$W/d64.zip" D64 > /dev/null)
    imp "$W/d64.zip" "$HD" > "$W/5.out" 2>&1 || fail "a Deflate64 zip should import: $(cat "$W/5.out")"
    [ "$(mtype -i "$C" ::/D64/BIG.DAT | wc -c)" = 100000 ] || fail "BIG.DAT should be unpacked whole"
else
    echo "(no 7-Zip: case 5 skipped)"
fi

# 6. Links and a FIFO, bsdtar only.
if command -v bsdtar > /dev/null; then
    mkdir -p "$W/6/LINKY" "$W/bsdonly"
    mz "$W/6/LINKY/LINKY.EXE"
    ln -s /etc/passwd "$W/6/LINKY/PASSWD"
    mkfifo "$W/6/LINKY/FIFO"
    (cd "$W/6" && bsdtar --format 7zip -cf "$W/links.7z" LINKY)
    for t in python3 bsdtar mdir mcopy mmd mtype minfo mdeltree mformat mshortname mdu; do ln -s "$(command -v $t)" "$W/bsdonly/$t"; done
    PATH="$W/bsdonly" "$(command -v timeout)" 60 python3 "$IMPORT" "$W/links.7z" "$HD" > "$W/6.out" 2>&1 ||
        fail "the 7z with links should import (or it hung): $(cat "$W/6.out")"
    mdir -b -i "$C" ::/LINKY | grep -qi 'passwd\|fifo' && fail "the link or the FIFO reached C:"
    mtype -i "$C" ::/LINKY/LINKY.EXE > /dev/null || fail "LINKY.EXE should be copied"
else
    echo "(no bsdtar: case 6 skipped)"
fi
[ -z "$(ls -A "$VM" | grep '^\.import-')" ] || fail "an unpacking folder was left in the VM's folder"

# ISO images: mkiso PATH LABEL NAME=FILE...: one root folder, ISO 9660.
cat > "$W/mkiso.py" << 'EOF'
import struct, sys


def both(fmt, v):
    return struct.pack("<" + fmt, v) + struct.pack(">" + fmt, v)


def record(name, lba, size, flags):
    r = bytes([0, 0]) + both("I", lba) + both("I", size) + bytes(7) + bytes([flags, 0, 0]) + both("H", 1) + \
        bytes([len(name)]) + name
    r += bytes(len(r) % 2)
    return bytes([len(r)]) + r[1:]


path, label = sys.argv[1], sys.argv[2]
files = [(a, open(b, "rb").read()) for a, b in (f.split("=", 1) for f in sys.argv[3:])]
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
pvd[156:190] = record(b"\0", 20, 2048, 2)
out = bytearray(16 * 2048) + pvd + b"\xffCD001\x01".ljust(2048, b"\0") + bytes(2 * 2048) + root.ljust(2048, b"\0")
for name, at, body in data:
    out += body.ljust(((len(body) + 2047) // 2048 or 1) * 2048, b"\0")
open(path, "wb").write(out)
EOF

# 7. An ISO with loose music.
mkdir -p "$W/7"
mz "$W/7/inst.bin"
python3 "$W/mkiso.py" "$W/7/Music Game.iso" MUSICCD "INSTALL.EXE=$W/7/inst.bin"
python3 -c '
import sys, wave
w = wave.open(sys.argv[1], "wb"); w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100)
w.writeframes(bytes(44100 * 4)); w.close()' "$W/7/Track02.wav"
cp "$W/7/Track02.wav" "$W/7/Track03.wav"
imp "$W/7/Music Game.iso" "$HD" > "$W/7.out"
grep -qx 'cdrom_01_image_path = cd/MUSICGAM/Music Game.cue' "$VM/86box.cfg" || fail "the ISO and its music should get a cue sheet: $(cat "$W/7.out")"
tr -d '\r' < "$VM/cd/MUSICGAM/Music Game.cue" | grep -qx '  TRACK 03 AUDIO' || fail "the cue should list track 3"
grep -q "type D: and then INSTALL" "$W/7.out" || fail "the ISO's installer should be named"

# 8. Install floppies.
mkdir -p "$W/8"
for n in 1 2; do
    mformat -C -f 1440 -i "$W/8/Disk$n.img" ::
    echo "disk $n" > "$W/8/LABEL.TXT"
    mcopy -i "$W/8/Disk$n.img" "$W/8/LABEL.TXT" ::/LABEL.TXT
done
mz "$W/8/INSTALL.EXE"
mcopy -i "$W/8/Disk1.img" "$W/8/INSTALL.EXE" ::/INSTALL.EXE
rm "$W/8/LABEL.TXT" "$W/8/INSTALL.EXE"
(cd "$W/8" && python3 -m zipfile -c "$W/Floppy Game.zip" Disk1.img Disk2.img)
imp "$W/Floppy Game.zip" "$HD" > "$W/8.out"
[ "$(mtype -i "$C" ::/FLOPPYGA/DISK2/LABEL.TXT | tr -d '\r')" = "disk 2" ] || fail "disk 2's files should be in DISK2: $(cat "$W/8.out")"
mtype -i "$C" ::/FLOPPYGA.BAT | tr -d '\r' | tail -2 | tr '\n' ' ' | grep -qx 'cd \\FLOPPYGA\\DISK1 INSTALL.EXE ' ||
    fail "the batch file should run DISK1's installer"

# 9. An SB16 folder.
mkdir -p "$W/9/SNDGAME/SB16"
mz "$W/9/SNDGAME/SNDGAME.EXE"
echo "pc speaker" > "$W/9/SNDGAME/SOUND.CFG"
echo "sb16 220 7 1 5" > "$W/9/SNDGAME/SB16/SOUND.CFG"
imp "$W/9/SNDGAME" "$HD" > "$W/9.out"
[ "$(mtype -i "$C" ::/SNDGAME/SOUND.CFG | tr -d '\r')" = "sb16 220 7 1 5" ] || fail "the SB16 setup should be in place"

# 10. A zip in a zip.
mkdir -p "$W/10/INNER"
mz "$W/10/INNER/INNER.EXE"
(cd "$W/10" && python3 -m zipfile -c "$W/10/INNER.ZIP" INNER && echo "unzip me" > README.TXT &&
    python3 -m zipfile -c "$W/Outer.zip" INNER.ZIP README.TXT)
imp "$W/Outer.zip" "$HD" > "$W/10.out"
mtype -i "$C" ::/INNER/INNER.EXE > /dev/null || fail "the inner zip's game should be imported: $(cat "$W/10.out")"

# 11. --json.
imp "$W/Space Game.zip" "$HD" --json > "$W/11.json" 2> "$W/11.err"
python3 -c '
import json, sys
r = json.load(open(sys.argv[1]))
assert r["kind"] == "plain" and r["dir"] == "C:\\SPACEGAM" and r["batch"] == "C:\\SPACEGAM.BAT", r
assert r["runs"][-1] == "SPACE.EXE" and r["run_source"].startswith("guessed"), r
assert r["next"] == ["C:\\SPACEGAM.BAT"], r' "$W/11.json" || fail "the JSON report is wrong: $(cat "$W/11.json")"
grep -q "copied to" "$W/11.err" || fail "with --json the text should go to stderr"

# 12. 86Box running there.
mkdir -p "$W/fake"
cp "$(command -v sleep)" "$W/fake/86Box"
(cd "$VM" && exec "$W/fake/86Box" 30) &
FAKE=$!
sleep 0.5
out=$(imp "$W/Space Game.zip" "$HD" 2>&1) && { kill $FAKE; fail "the import should refuse while 86Box runs"; }
kill $FAKE 2> /dev/null || true
case "$out" in *"is running this VM"*) ;; *) fail "the refusal should say why: $out" ;; esac

# 13. Other packagers' DOSBox configs.
mkdir -p "$W/13/PACK/GAMEDIR" "$W/13/PACK/CD"
mz "$W/13/PACK/GAMEDIR/SAVE.EXE"
mkdir -p "$W/13/cdsrc"
mz "$W/13/cdsrc/rebel.bin"
python3 "$W/mkiso.py" "$W/13/PACK/CD/REBEL.ISO" REBEL "REBEL.EXE=$W/13/cdsrc/rebel.bin"
printf '[autoexec]\nmount c "C:\\Users\\packer\\Games\\PACK\\GAMEDIR"\nimgmount d -t iso ".\\CD\\REBEL.ISO"\nd:\ncd \\REBEL\nREBEL.EXE\n' > "$W/13/PACK/dosbox.conf"
imp "$W/13/PACK" "$HD" > "$W/13.out" 2>&1 || fail "the packager's config should import: $(cat "$W/13.out")"
grep -qx 'cdrom_01_image_path = cd/PACK/REBEL.ISO' "$VM/86box.cfg" || fail "imgmount with -t first should attach the CD: $(cat "$W/13.out")"
mtype -i "$C" ::/PACK.BAT | tr -d '\r' | grep -qx 'cd \\REBEL' || fail "the batch file should keep the cd on D:"
mtype -i "$C" ::/PACK/SAVE.EXE > /dev/null || fail "C: (by its Windows path) should be copied"
mkdir -p "$W/13/GOGBAD"
printf '[autoexec]\nmount C "..\\\\missing"\nc:\nGAME.EXE\n' > "$W/13/GOGBAD/dosbox_bad.conf"
printf '{"name": "Bad", "playTasks": [{"isPrimary": true, "arguments": "-conf \\"dosbox_bad.conf\\"", "workingDir": ""}]}\n' > "$W/13/GOGBAD/goggame-9.info"
out=$(imp "$W/13/GOGBAD" "$HD" 2>&1) && fail "a GOG config without C: should stop"
case "$out" in *"mounts no folder as C:"*) ;; *) fail "the GOG error should stay: $out" ;; esac

# 14. A CHD.
printf 'MComprHD' > "$W/Game.chd"
head -c 2000 /dev/zero >> "$W/Game.chd"
imp "$W/Game.chd" "$HD" > "$W/14.out"
grep -qx 'cdrom_01_image_path = cd/GAME/Game.chd' "$VM/86box.cfg" || fail "the CHD should go into the drive: $(cat "$W/14.out")"

echo "PASS"
