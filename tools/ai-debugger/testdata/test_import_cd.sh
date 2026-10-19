#!/bin/bash
# CD image tests for tools/ai-debugger/gog_import.py. Each case runs in its
# own subshell with set -e, on its own fresh VM; a failed case is reported
# and the next one runs. Prints "FAIL: case N: why" for each failure, "PASS"
# when none.
# Needs mtools and Python; ffmpeg for the FLAC file in case 2 (skipped
# without it). Programs are fake MZ headers, never run.
#   1. A UTF-8 cue Spiel.cue naming "Spiel für Kinder.bin" and
#      "Lied 02 – Überall.wav" (44.1 kHz stereo), both there: imported
#      as a CD (the cue given, no BOM; and its folder, with a BOM cue): no
#      "isn't there" note, both files in cd/, the written cue is UTF-8 with
#      a BOM and names them exactly; one disc only.
#   2. A game folder with a program, 'Game (Disc 1).iso' with
#      its Track 02 and 03 WAVs, 'Game (Disc 2).iso' with its Track 02:
#      disc 1's cue has its own tracks 2 and 3 only, disc 2's its track 2
#      only, no music or ISO on C:. And game.iso with Track02.wav,
#      Track02.flac and Track04.wav: one track 2 (the WAV), a silent
#      stand-in for 3, Track04.wav as TRACK 04.
#   3. Disc1/game1.iso + Disc1/Track02.wav, Disc2/game2.iso +
#      Disc2/Track02.wav (other contents): separate CDn folders, each
#      disc's cue plays its own Track02.wav.
#   4. Foo/game.cue (+game.bin) then Bar/game.cue (+game.bin)
#      as CD-only imports into one VM: different cd/ folders, each with its
#      own game.bin, Foo's disc stays in the recent images; Foo imported
#      again reuses its folder.
#   5. cdrdao .toc with DATAFILE "data.bin" and AUDIOFILE
#      "track02.wav" 0: a game folder with the TOC and its files in its
#      tracks/ subfolder (both files and the TOC go to cd/, not C:), and a
#      TOC naming "tracks/DATA.BIN" and "tracks/Track02.wav" (other case
#      than on disk) given directly: both files copied, the written .toc
#      names them as copied.
#   6. 'CD-Man (1989)(Creative Dimensions).zip' with
#      CD-MAN/CDMAN.EXE, CDMAN.DAT: copied to C:, a batch file starts it,
#      nothing in the drive. Rally/RALLY.EXE + Rally/SOUND/CD/*.VOC (the
#      folder given, and in a zip): SOUND\CD copied to C:, nothing in the
#      drive.
#   7. game.iso + mono 22 kHz Track02.wav, ffmpeg not on
#      PATH: the drive holds the bare ISO (no cue written), a note says the
#      music is left out.
#   8. game.ins naming GAME.GOG and GAME02.OGG, neither
#      there: nothing in the drive, no sheet in cd/, a note says so.
#   9. Tactic/TACTIC.EXE + Tactic/MAPS/TERRAIN.ISO (50 KB,
#      no ISO 9660 volume): TERRAIN.ISO copied to C: whole, nothing in the
#      drive.

HERE=$(cd "$(dirname "$0")" && pwd)
IMPORT=${IMPORT:-$HERE/../gog_import.py}  # IMPORT=... tests another copy
W=$(mktemp -d)
trap '[ -n "$KEEP" ] || rm -rf "$W"' EXIT
export TMPDIR="$W/tmp"  # the importer's own temporary files stay in here too
mkdir -p "$TMPDIR"
FAILS="$W/fails"
: > "$FAILS"
fail() { echo "FAIL: case $CASE: $(printf '%s' "$*" | tr '\n' ' ')" | tee -a "$FAILS"; }
TO=120
TIMEOUT=$(command -v timeout)

# mz FILE [SIZE]: a fake DOS program.
mz() { python3 -c '
import sys
size = int(sys.argv[2]) if len(sys.argv) > 2 else 600
b = bytearray(size); b[0:2] = b"MZ"; b[0x18] = 0x1C
open(sys.argv[1], "wb").write(b)' "$@"; }

# imp ARGS...: the importer, never longer than $TO seconds.
imp() { (ulimit -f 102400 && exec "$TIMEOUT" -k 5 "$TO" python3 -I "$IMPORT" "$@"); }

# mkvm DIR: a VM, a 20 MB disk with one FAT16 partition at sector 63 and a
# CD drive; HD is its disk, C its FAT volume for mtools, CFG its 86box.cfg.
mkvm() {
    VM=$1
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
    CFG="$VM/86box.cfg"
}

# listing: every file and folder on C:, recursively.
listing() { mdir -/ -b -i "$C" ::/ 2> /dev/null || true; }

# cfgval KEY: a value in the VM's 86box.cfg.
cfgval() { sed -n "s/^$1 = //p" "$CFG"; }

# mkiso PATH LABEL NAME=FILE...: an ISO 9660 image, one root folder.
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
# iso PATH LABEL: an ISO with an INSTALL.EXE (contents differ by label).
iso() {
    mz "$W/inst.$2.bin" 700
    printf '%s' "$2" >> "$W/inst.$2.bin"
    python3 "$W/mkiso.py" "$1" "$2" "INSTALL.EXE=$W/inst.$2.bin"
}

# mkwav PATH CHANNELS RATE [FILL]: a 1-second 16-bit WAV, its samples FILL.
mkwav() { python3 -c '
import sys, wave
ch, rate = int(sys.argv[2]), int(sys.argv[3])
fill = int(sys.argv[4]) if len(sys.argv) > 4 else 0
w = wave.open(sys.argv[1], "wb"); w.setnchannels(ch); w.setsampwidth(2); w.setframerate(rate)
w.writeframes(bytes([fill]) * (rate * 2 * ch)); w.close()' "$@"; }

# wavinfo PATH: "channels rate silent|sound" of a WAV.
wavinfo() { python3 -c '
import sys, wave
w = wave.open(sys.argv[1]); d = w.readframes(w.getnframes())
print(w.getnchannels(), w.getframerate(), "silent" if not any(d) else "sound")' "$1"; }

# cueinfo CUE: "BOM" or "NOBOM", then "NN<tab>file<tab>mode" per track; the
# sheet must be strict UTF-8 (after a BOM).
cat > "$W/cueinfo.py" << 'EOF'
import re, sys
data = open(sys.argv[1], "rb").read()
bom = data.startswith(b"\xef\xbb\xbf")
text = (data[3:] if bom else data).decode("utf-8")
print("BOM" if bom else "NOBOM")
f = ""
for line in text.splitlines():
    m = re.match(r'\s*FILE\s+(?:"([^"]+)"|(\S+))', line, re.I)
    if m:
        f = m.group(1) or m.group(2)
        continue
    m = re.match(r"\s*TRACK\s+(\d+)\s+(\S+)", line, re.I)
    if m:
        print("%s\t%s\t%s" % (m.group(1), f, m.group(2).upper()))
EOF
cueinfo() { python3 "$W/cueinfo.py" "$1"; }
# tracks CUE: just the "NN<tab>file<tab>mode" lines.
tracks() { cueinfo "$1" | tail -n +2; }
# trackfile CUE NN: the file a cue's track NN is in.
trackfile() { tracks "$1" | awk -F '\t' -v n="$2" '$1 == n { print $2 }'; }
# tocfiles TOC: the files a cdrdao .toc names, one per line.
tocfiles() { python3 -c '
import re, sys
for m in re.finditer(r"^\s*(?:DATA|AUDIO)?FILE\s+\"([^\"]+)\"", open(sys.argv[1], encoding="utf-8").read(), re.M | re.I):
    print(m.group(1))' "$1"; }

samefile() { cmp -s "$1" "$2"; }

# 1. A UTF-8 cue sheet with non-ASCII names.
case1() {
    local S="$W/1/src" BIN='Spiel für Kinder.bin' WAV='Lied 02 – Überall.wav'
    mkdir -p "$S"
    iso "$S/$BIN" SPIEL
    mkwav "$S/$WAV" 2 44100 7
    printf 'FILE "%s" BINARY\n  TRACK 01 MODE1/2048\n    INDEX 01 00:00:00\nFILE "%s" WAVE\n  TRACK 02 AUDIO\n    INDEX 01 00:00:00\n' \
        "$BIN" "$WAV" > "$S/Spiel.cue"
    local want
    want=$(printf 'BOM\n01\t%s\tMODE1/2048\n02\t%s\tAUDIO' "$BIN" "$WAV")
    # 1a: the cue sheet given (no BOM).
    mkvm "$W/1/vm"
    imp "$S/Spiel.cue" "$HD" > "$W/1a.out" 2>&1 || { fail "1a: the import failed: $(tail -3 "$W/1a.out")"; return; }
    grep -q "isn't there" "$W/1a.out" && fail "1a: a file the UTF-8 cue names is reported missing: $(grep "isn't there" "$W/1a.out")"
    [ "$(cfgval cdrom_01_image_path)" = "cd/SPIEL/Spiel.cue" ] ||
        fail "1a: the cue should be in the drive: $(grep cdrom_01 "$CFG" | tr '\n' ';') $(cat "$W/1a.out")"
    grep -q '^cdrom_01_image_history' "$CFG" && fail "1a: there should be one disc: $(grep history "$CFG")"
    samefile "$VM/cd/SPIEL/$BIN" "$S/$BIN" || fail "1a: $BIN should be in cd/SPIEL: $(ls "$VM/cd/SPIEL")"
    samefile "$VM/cd/SPIEL/$WAV" "$S/$WAV" || fail "1a: $WAV should be in cd/SPIEL: $(ls "$VM/cd/SPIEL")"
    local got
    got=$(cueinfo "$VM/cd/SPIEL/Spiel.cue" 2>&1) || true
    [ "$got" = "$want" ] || fail "1a: the written cue should be UTF-8 with a BOM naming both files: $got"
    # 1b: its folder given, the cue with a BOM.
    mkdir -p "$W/1/folder/Spiel"
    cp "$S/$BIN" "$S/$WAV" "$W/1/folder/Spiel/"
    { printf '\xef\xbb\xbf'; cat "$S/Spiel.cue"; } > "$W/1/folder/Spiel/Spiel.cue"
    mkvm "$W/1/vm2"
    imp "$W/1/folder/Spiel" "$HD" > "$W/1b.out" 2>&1 || { fail "1b: the import failed: $(tail -3 "$W/1b.out")"; return; }
    grep -q "isn't there" "$W/1b.out" && fail "1b: a file the BOM cue names is reported missing"
    local path
    path=$(cfgval cdrom_01_image_path)
    case "$path" in */Spiel.cue) ;; *) fail "1b: the cue should be in the drive, not $path: $(cat "$W/1b.out")" ;; esac
    grep -q '^cdrom_01_image_history' "$CFG" && fail "1b: there should be one disc: $(grep history "$CFG")"
    if [ -n "$path" ] && [ -f "$VM/$path" ]; then
        got=$(cueinfo "$VM/$path" 2>&1) || true
        [ "$got" = "$want" ] || fail "1b: the written cue should be UTF-8 with a BOM naming both files: $got"
        samefile "$(dirname "$VM/$path")/$WAV" "$S/$WAV" || fail "1b: $WAV should be next to the cue"
        samefile "$(dirname "$VM/$path")/$BIN" "$S/$BIN" || fail "1b: $BIN should be next to the cue"
    fi
}

# 2. Loose tracks of two discs in one folder; duplicate and missing tracks.
case2() {
    local G="$W/2/MUSGAME"
    mkdir -p "$G"
    mz "$G/MUSGAME.EXE" 5000
    iso "$G/Game (Disc 1).iso" DISC1
    iso "$G/Game (Disc 2).iso" DISC2
    mkwav "$G/Game (Disc 1) (Track 02).wav" 2 44100 1
    mkwav "$G/Game (Disc 1) (Track 03).wav" 2 44100 2
    mkwav "$G/Game (Disc 2) (Track 02).wav" 2 44100 3
    mkvm "$W/2/vm"
    imp "$G" "$HD" > "$W/2a.out" 2>&1 || { fail "2a: the import failed: $(tail -3 "$W/2a.out")"; return; }
    local c1 c2
    c1=$(find "$VM/cd" -name 'Game (Disc 1).cue')
    c2=$(find "$VM/cd" -name 'Game (Disc 2).cue')
    if [ -z "$c1" ] || [ -z "$c2" ]; then
        fail "2a: each disc should get a cue: $(find "$VM/cd" -type f) $(cat "$W/2a.out")"
    else
        [ "$(tracks "$c1" | cut -f1 | tr '\n' ' ')" = "01 02 03 " ] || fail "2a: disc 1 should have tracks 1-3: $(tracks "$c1")"
        [ "$(tracks "$c2" | cut -f1 | tr '\n' ' ')" = "01 02 " ] || fail "2a: disc 2 should have tracks 1-2: $(tracks "$c2")"
        samefile "$(dirname "$c1")/$(trackfile "$c1" 02)" "$G/Game (Disc 1) (Track 02).wav" ||
            fail "2a: disc 1's track 2 should be its own: $(trackfile "$c1" 02)"
        samefile "$(dirname "$c1")/$(trackfile "$c1" 03)" "$G/Game (Disc 1) (Track 03).wav" ||
            fail "2a: disc 1's track 3 should be its own: $(trackfile "$c1" 03)"
        samefile "$(dirname "$c2")/$(trackfile "$c2" 02)" "$G/Game (Disc 2) (Track 02).wav" ||
            fail "2a: disc 2's track 2 should be its own: $(trackfile "$c2" 02)"
        [ "$(cfgval cdrom_01_image_path)" = "${c1#$VM/}" ] || fail "2a: disc 1 should be in the drive: $(grep cdrom_01 "$CFG")"
        [ "$(cfgval cdrom_01_image_history_01)" = "${c2#$VM/}" ] || fail "2a: disc 2 should be in the recent images"
    fi
    listing | grep -qi '/MUSGAME\.EXE$' || fail "2a: the program should be on C:"
    local hits
    hits=$(listing | grep -Ei '\.(wav|iso|cue)$' || true)
    [ -z "$hits" ] || fail "2a: CD files reached C:: $hits"

    # 2b: duplicate track 2, no track 3.
    local S="$W/2/dup"
    mkdir -p "$S"
    iso "$S/game.iso" DUPCD
    mkwav "$S/Track02.wav" 2 44100 4
    mkwav "$S/Track04.wav" 2 44100 5
    if command -v ffmpeg > /dev/null; then
        ffmpeg -nostdin -v error -y -i "$S/Track02.wav" "$S/Track02.flac"
    else
        echo "(no ffmpeg: case 2b without Track02.flac)"
    fi
    mkvm "$W/2/vm2"
    imp "$S/game.iso" "$HD" > "$W/2b.out" 2>&1 || { fail "2b: the import failed: $(tail -3 "$W/2b.out")"; return; }
    local cue="$VM/cd/GAME/game.cue"
    [ "$(cfgval cdrom_01_image_path)" = "cd/GAME/game.cue" ] || fail "2b: game.cue should be in the drive: $(cat "$W/2b.out")"
    [ -f "$cue" ] || { fail "2b: no game.cue in cd/GAME: $(ls "$VM/cd/GAME")"; return; }
    [ "$(tracks "$cue" | cut -f1 | tr '\n' ' ')" = "01 02 03 04 " ] || fail "2b: tracks 1-4 should be there once each: $(tracks "$cue")"
    [ "$(trackfile "$cue" 01)" = game.iso ] || fail "2b: track 1 should be game.iso"
    [ "$(trackfile "$cue" 02)" = Track02.wav ] || fail "2b: track 2 should be Track02.wav: $(trackfile "$cue" 02)"
    samefile "$VM/cd/GAME/Track02.wav" "$S/Track02.wav" || fail "2b: Track02.wav should be copied"
    [ "$(trackfile "$cue" 04)" = Track04.wav ] || fail "2b: track 4 should be Track04.wav: $(trackfile "$cue" 04)"
    samefile "$VM/cd/GAME/Track04.wav" "$S/Track04.wav" || fail "2b: Track04.wav should be copied"
    local t3
    t3=$(trackfile "$cue" 03)
    if [ -z "$t3" ] || [ ! -f "$VM/cd/GAME/$t3" ]; then
        fail "2b: track 3 should be a silent stand-in: '$t3'"
    else
        case "$t3" in Track02.*|Track04.*) fail "2b: track 3 should not be a real track: $t3" ;; esac
        [ "$(wavinfo "$VM/cd/GAME/$t3")" = "2 44100 silent" ] || fail "2b: track 3 should be silent 44.1 kHz stereo: $(wavinfo "$VM/cd/GAME/$t3")"
    fi
    ls "$VM/cd/GAME" | grep -qi '\.flac$' && fail "2b: the duplicate FLAC should be left out: $(ls "$VM/cd/GAME")"
    true
}

# 3. Two discs in their own folders, each with its Track02.wav.
case3() {
    local T="$W/3/TwoDisc"
    mkdir -p "$T/Disc1" "$T/Disc2"
    iso "$T/Disc1/game1.iso" GAME1
    iso "$T/Disc2/game2.iso" GAME2
    mkwav "$T/Disc1/Track02.wav" 2 44100 17
    mkwav "$T/Disc2/Track02.wav" 2 44100 34
    mkvm "$W/3/vm"
    imp "$T" "$HD" > "$W/3.out" 2>&1 || { fail "the import failed: $(tail -3 "$W/3.out")"; return; }
    local c1 c2
    c1=$(find "$VM/cd" -name game1.cue)
    c2=$(find "$VM/cd" -name game2.cue)
    if [ -z "$c1" ] || [ -z "$c2" ]; then
        fail "each disc should get a cue: $(find "$VM/cd" -type f | tr '\n' ' ') $(cat "$W/3.out")"
        return
    fi
    [ "$(dirname "$c1")" != "$(dirname "$c2")" ] || fail "the discs share the folder $(dirname "$c1")"
    [ "$(trackfile "$c1" 02)" = Track02.wav ] || fail "game1.cue's track 2: $(tracks "$c1")"
    [ "$(trackfile "$c2" 02)" = Track02.wav ] || fail "game2.cue's track 2: $(tracks "$c2")"
    samefile "$(dirname "$c1")/Track02.wav" "$T/Disc1/Track02.wav" || fail "disc 1 plays another disc's Track02.wav"
    samefile "$(dirname "$c2")/Track02.wav" "$T/Disc2/Track02.wav" || fail "disc 2 plays another disc's Track02.wav"
    [ "$(cfgval cdrom_01_image_path)" = "${c1#$VM/}" ] || fail "disc 1 should be in the drive: $(grep cdrom_01 "$CFG")"
    [ "$(cfgval cdrom_01_image_history_01)" = "${c2#$VM/}" ] || fail "disc 2 should be in the recent images: $(grep cdrom_01 "$CFG")"
}

# 4. Two games' CD-only imports, both named game.cue.
case4() {
    local g
    for g in Foo Bar; do
        mkdir -p "$W/4/$g"
        iso "$W/4/$g/game.bin" "${g^^}CD"
        printf 'FILE "game.bin" BINARY\n  TRACK 01 MODE1/2048\n    INDEX 01 00:00:00\n' > "$W/4/$g/game.cue"
    done
    mkvm "$W/4/vm"
    imp "$W/4/Foo/game.cue" "$HD" > "$W/4a.out" 2>&1 || { fail "Foo's import failed: $(tail -3 "$W/4a.out")"; return; }
    local foo
    foo=$(cfgval cdrom_01_image_path)
    imp "$W/4/Bar/game.cue" "$HD" > "$W/4b.out" 2>&1 || { fail "Bar's import failed: $(tail -3 "$W/4b.out")"; return; }
    local bar
    bar=$(cfgval cdrom_01_image_path)
    [ -n "$foo" ] && [ -n "$bar" ] || { fail "both discs should go into the drive: '$foo' '$bar'"; return; }
    [ "$(dirname "$foo")" != "$(dirname "$bar")" ] || fail "Foo's and Bar's discs share cd/ folder $(dirname "$foo")"
    samefile "$VM/$(dirname "$foo")/game.bin" "$W/4/Foo/game.bin" || fail "Foo's game.bin was replaced (by Bar's)"
    samefile "$VM/$(dirname "$bar")/game.bin" "$W/4/Bar/game.bin" || fail "Bar's game.bin isn't Bar's"
    grep -qx "cdrom_01_image_history_[0-9]* = $foo" "$CFG" ||
        fail "Foo's disc should stay in the recent images: $(grep cdrom_01 "$CFG" | tr '\n' ';')"
    [ "$(find "$VM/cd" -name game.bin | wc -l)" = 2 ] || fail "there should be two game.bin: $(find "$VM/cd" -name game.bin)"
    # Foo again: its own folder.
    imp "$W/4/Foo/game.cue" "$HD" > "$W/4c.out" 2>&1 || { fail "Foo's second import failed: $(tail -3 "$W/4c.out")"; return; }
    [ "$(cfgval cdrom_01_image_path)" = "$foo" ] || fail "importing Foo again should reuse $foo, not $(cfgval cdrom_01_image_path)"
    [ "$(find "$VM/cd" -name game.bin | wc -l)" = 2 ] || fail "importing Foo again made another copy"
    grep -qx "cdrom_01_image_history_[0-9]* = $bar" "$CFG" || fail "Bar's disc should stay in the recent images"
}

# 5. cdrdao .toc with an AUDIOFILE.
case5() {
    local G="$W/5/TOCGAME"
    mkdir -p "$G/tracks"
    mz "$G/TOCGAME.EXE" 4000
    iso "$G/tracks/data.bin" TOCCD
    mkwav "$G/tracks/track02.wav" 2 44100 9
    printf 'CD_ROM\n\nTRACK MODE1\nDATAFILE "data.bin"\n\nTRACK AUDIO\nAUDIOFILE "track02.wav" 0\n' > "$G/tracks/game.toc"
    # 5a: a game folder, the TOC and its files in tracks/.
    mkvm "$W/5/vm"
    imp "$G" "$HD" > "$W/5a.out" 2>&1 || { fail "5a: the import failed: $(tail -3 "$W/5a.out")"; return; }
    local path toc
    path=$(cfgval cdrom_01_image_path)
    toc="$VM/$path"
    case "$path" in */game.toc) ;; *) fail "5a: game.toc should be in the drive, not '$path': $(cat "$W/5a.out")" ;; esac
    if [ -f "$toc" ]; then
        [ "$(tocfiles "$toc" | tr '\n' ' ')" = "data.bin track02.wav " ] || fail "5a: the .toc names: $(tocfiles "$toc")"
        samefile "$(dirname "$toc")/data.bin" "$G/tracks/data.bin" || fail "5a: data.bin should be next to the .toc"
        samefile "$(dirname "$toc")/track02.wav" "$G/tracks/track02.wav" || fail "5a: track02.wav should be next to the .toc"
    fi
    local hits
    hits=$(listing | grep -Ei '/(data\.bin|track02\.wav|game\.toc)$' || true)
    [ -z "$hits" ] || fail "5a: the CD's files reached C:: $hits"
    listing | grep -qi '/TOCGAME\.EXE$' || fail "5a: the program should be on C:"
    # 5b: a TOC naming its files in tracks/, another case than on disk.
    local S="$W/5/src"
    mkdir -p "$S/tracks"
    cp "$G/tracks/data.bin" "$G/tracks/track02.wav" "$S/tracks/"
    printf 'CD_ROM\n\nTRACK MODE1\nDATAFILE "tracks/DATA.BIN"\n\nTRACK AUDIO\nAUDIOFILE "tracks/Track02.wav" 0\n' > "$S/game.toc"
    mkvm "$W/5/vm2"
    imp "$S/game.toc" "$HD" > "$W/5b.out" 2>&1 || { fail "5b: the import failed: $(tail -3 "$W/5b.out")"; return; }
    [ "$(cfgval cdrom_01_image_path)" = cd/GAME/game.toc ] || fail "5b: game.toc should be in the drive: $(cat "$W/5b.out")"
    [ -f "$VM/cd/GAME/game.toc" ] || { fail "5b: no game.toc in cd/GAME"; return; }
    [ "$(tocfiles "$VM/cd/GAME/game.toc" | tr '\n' ' ')" = "data.bin track02.wav " ] ||
        fail "5b: the .toc should name the copies: $(tocfiles "$VM/cd/GAME/game.toc")"
    samefile "$VM/cd/GAME/data.bin" "$S/tracks/data.bin" || fail "5b: data.bin should be copied"
    samefile "$VM/cd/GAME/track02.wav" "$S/tracks/track02.wav" || fail "5b: track02.wav should be copied"
    grep -q "isn't there" "$W/5b.out" && fail "5b: a file is reported missing: $(grep "isn't there" "$W/5b.out")"
    true
}

# 6. Game folders named like CDs.
case6() {
    mkdir -p "$W/6/pack/CD-MAN"
    mz "$W/6/pack/CD-MAN/CDMAN.EXE" 6000
    echo "cdman data" > "$W/6/pack/CD-MAN/CDMAN.DAT"
    local ZIP="$W/6/CD-Man (1989)(Creative Dimensions).zip"
    (cd "$W/6/pack" && python3 -m zipfile -c "$ZIP" CD-MAN)
    mkvm "$W/6/vm"
    imp "$ZIP" "$HD" > "$W/6a.out" 2>&1 || { fail "6a: the import failed: $(tail -3 "$W/6a.out")"; return; }
    local l bat b
    l=$(listing)
    echo "$l" | grep -qi '/CDMAN\.EXE$' || fail "6a: CDMAN.EXE should be on C:: $l $(cat "$W/6a.out")"
    echo "$l" | grep -qi '/CDMAN\.DAT$' || fail "6a: CDMAN.DAT should be on C:"
    bat=""
    for b in $(echo "$l" | grep -Ei '^::/[^/]+\.BAT$'); do
        mtype -i "$C" "$b" | tr -d '\r' | grep -qix 'CDMAN\.EXE' && bat=$b
    done
    [ -n "$bat" ] || fail "6a: a batch file should start CDMAN.EXE"
    grep -q '^cdrom_01_image_path' "$CFG" && fail "6a: nothing should be in the drive: $(grep cdrom_01 "$CFG")"
    grep -q "is in the VM's CD-ROM drive" "$W/6a.out" && fail "6a: the report says a CD is in the drive"
    # 6b: SOUND\CD inside the game's folder, the folder given and zipped.
    local R="$W/6/Rally"
    mkdir -p "$R/SOUND/CD" "$R/LEVELS"
    mz "$R/RALLY.EXE" 7000
    echo level > "$R/LEVELS/L1.DAT"
    echo engine > "$R/SOUND/CD/ENGINE.VOC"
    echo crash > "$R/SOUND/CD/CRASH.VOC"
    (cd "$W/6" && python3 -m zipfile -c "$W/6/Rally.zip" Rally)
    local how src
    for how in folder zip; do
        src=$R
        [ $how = zip ] && src="$W/6/Rally.zip"
        mkvm "$W/6/vm-$how"
        imp "$src" "$HD" > "$W/6b-$how.out" 2>&1 || { fail "6b ($how): the import failed: $(tail -3 "$W/6b-$how.out")"; continue; }
        l=$(listing)
        echo "$l" | grep -qi '/RALLY\.EXE$' || fail "6b ($how): RALLY.EXE should be on C:"
        echo "$l" | grep -qi '/SOUND/CD/ENGINE\.VOC$' || fail "6b ($how): SOUND\\CD\\ENGINE.VOC should be on C:: $(echo "$l" | tr '\n' ' ')"
        echo "$l" | grep -qi '/SOUND/CD/CRASH\.VOC$' || fail "6b ($how): SOUND\\CD\\CRASH.VOC should be on C:"
        grep -q '^cdrom_01_image_path' "$CFG" && fail "6b ($how): nothing should be in the drive: $(grep cdrom_01 "$CFG")"
    done
    true
}

# 7. Unplayable loose music, no ffmpeg.
case7() {
    local S="$W/7/src" NOFF="$W/7/bin" t
    mkdir -p "$S" "$NOFF"
    iso "$S/game.iso" MONOCD
    mkwav "$S/Track02.wav" 1 22050 3
    for t in python3 mdir mcopy mmd mtype minfo mdeltree mformat mshortname mdu mattrib mdel mren mrd 7z bsdtar; do
        command -v $t > /dev/null && ln -s "$(command -v $t)" "$NOFF/$t"
    done
    mkvm "$W/7/vm"
    (ulimit -f 102400 && PATH="$NOFF" exec "$TIMEOUT" -k 5 "$TO" python3 -I "$IMPORT" "$S/game.iso" "$HD") > "$W/7.out" 2>&1 ||
        { fail "the import failed: $(tail -3 "$W/7.out")"; return; }
    [ "$(cfgval cdrom_01_image_path)" = cd/GAME/game.iso ] ||
        fail "the bare ISO should be in the drive: $(grep cdrom_01 "$CFG") $(cat "$W/7.out")"
    samefile "$VM/cd/GAME/game.iso" "$S/game.iso" || fail "cd/GAME/game.iso should be the ISO"
    [ -e "$VM/cd/GAME/game.cue" ] && fail "no cue sheet should be written: $(cat "$VM/cd/GAME/game.cue")"
    grep -qiE "without its music|music.*left out|left out.*music" "$W/7.out" || fail "a note should say the music is left out: $(cat "$W/7.out")"
    grep -qi "ffmpeg" "$W/7.out" || fail "the note should name ffmpeg"
    grep -q "wrote a cue sheet" "$W/7.out" && fail "the report claims a cue sheet"
    true
}

# 8. A GOG .ins whose files aren't there.
case8() {
    local S="$W/8/src"
    mkdir -p "$S"
    printf 'FILE "GAME.GOG" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\nFILE "GAME02.OGG" WAVE\n  TRACK 02 AUDIO\n    INDEX 01 00:00:00\n' > "$S/game.ins"
    mkvm "$W/8/vm"
    imp "$S/game.ins" "$HD" > "$W/8.out" 2>&1 || true
    grep -q 'Traceback' "$W/8.out" && { fail "a traceback: $(tail -3 "$W/8.out")"; return; }
    grep -q '^cdrom_01_image_path' "$CFG" && fail "nothing should be in the drive: $(grep cdrom_01 "$CFG")"
    local sheets
    sheets=$(find "$VM" -path "$VM/cd/*" -type f \( -iname '*.ins' -o -iname '*.cue' \) 2> /dev/null || true)
    [ -z "$sheets" ] || fail "no sheet should be left in cd/: $sheets"
    grep -qE "GAME\.GOG.*isn't there|can't go into the drive" "$W/8.out" || fail "a note should say the files aren't there: $(cat "$W/8.out")"
    grep -q "is in the VM's CD-ROM drive" "$W/8.out" && fail "the report says the CD is in the drive"
    true
}

# 9. Game data named .ISO.
case9() {
    local T="$W/9/Tactic"
    mkdir -p "$T/MAPS"
    mz "$T/TACTIC.EXE" 8000
    echo units > "$T/MAPS/UNITS.DAT"
    python3 -c '
import sys
open(sys.argv[1], "wb").write((b"TERRAIN-MAP-DATA" * 3200)[:51200])' "$T/MAPS/TERRAIN.ISO"
    mkvm "$W/9/vm"
    imp "$T" "$HD" > "$W/9.out" 2>&1 || { fail "the import failed: $(tail -3 "$W/9.out")"; return; }
    local l at
    l=$(listing)
    at=$(echo "$l" | grep -i '/MAPS/TERRAIN\.ISO$' | head -1)
    if [ -z "$at" ]; then
        fail "TERRAIN.ISO should be on C:: $(echo "$l" | tr '\n' ' ')"
    else
        mtype -i "$C" "$at" > "$W/9.terrain" 2> /dev/null || true
        cmp -s "$W/9.terrain" "$T/MAPS/TERRAIN.ISO" || fail "C:'s TERRAIN.ISO isn't the game's ($(wc -c < "$W/9.terrain") bytes)"
    fi
    echo "$l" | grep -qi '/MAPS/UNITS\.DAT$' || fail "UNITS.DAT should be on C:"
    grep -q '^cdrom_01_image_path' "$CFG" && fail "nothing should be in the drive: $(grep cdrom_01 "$CFG")"
    true
}

for CASE in 1 2 3 4 5 6 7 8 9; do
    echo "--- case $CASE"
    (set -e; "case$CASE")
    rc=$?
    [ $rc = 0 ] || fail "stopped by an unexpected error (exit $rc)"
done

if [ -s "$FAILS" ]; then
    echo "$(wc -l < "$FAILS") failure(s):"
    cat "$FAILS"
    exit 1
fi
echo "PASS"
