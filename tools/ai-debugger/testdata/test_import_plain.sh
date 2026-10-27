#!/bin/bash
# gog_import.py on plain DOS games (no DOSBox config, or one whose C: is
# missing): which program the batch file starts, and that DOS can find the
# folder and the program it names. Each case gets its own 20 MB VM disk.
# Programs are fake (MZ headers), never run. Needs mtools and Python.
#   1. Long names: DoomCol/Doom II/DOOM2.EXE: the cd uses the 8.3 name DOS
#      sees; 'Keen Dreams.exe' alone in KEEN: the batch runs KEENDR~1.EXE.
#   2. Wing Commander II, then Wing Commander: a batch file each; WC II
#      imported again reuses its folder and batch file.
#   3. UFO.BAT running INTRO, GEOSCAPE, TACTICAL is started (not DOSBox's).
#   4. Dungeon Master's DM.EXE is started; next to DOOM.EXE, DOOM.EXE is.
#   5. Only non-game programs: a batch file all the same; --run VIEW.EXE
#      then reuses the folder and the batch file.
#   6. Floppies whose program disk sorts after the data disk.
#   7. INSTALL.BAT driving DEICE.EXE is the installer run.
#   8. 'Données': the cd line's bytes are those DOS has (CP437).
#   9. Install floppies and a Scans folder: named after the game.
#  10. NOSOUND.BAT doesn't outrank WOLF3D.EXE.
#  11. A config with a missing C: whose commands run "game -nosound":
#      GAME.COM -nosound (DOS's order), not listed as another program.
#  12. --batch KEEN.BAT writes KEEN.BAT; --batch WOLFENSTEIN is refused.
#  13. A 12 MB game on a 20 MB disk imported again with --run.
#  14. A real ISO in the game's folder (SPACE/CD/space.iso): not on C:,
#      in the drive.
#  15. --dir 'GAMES\SR' and --dir GAMES/SR: the files in C:\GAMES\SR.
#  16. 'Star Game' with SoundSetup.exe: the sound hint names SOUNDS~1.EXE,
#      the name DOS has. A game bigger than the whole of C:: told so.
# Prints "ok: case N" or "FAIL: case N: why" for each, then PASS when all
# pass. KEEP=1 keeps the work folder.

HERE=$(cd "$(dirname "$0")" && pwd)
IMPORT=${IMPORT:-$HERE/../gog_import.py}  # IMPORT=... tests another copy
W=$(mktemp -d "${TMPDIR:-/tmp}/plain-games.XXXXXX") || exit 1
trap '[ -n "${KEEP:-}" ] && echo "kept $W" || rm -rf "$W"' EXIT
mkdir -p "$W/tmp"
# The importer's temporary files stay in the work folder; mtools as the
# importer runs it (CP437 short names, UTF-8 host names).
export TMPDIR="$W/tmp" LC_ALL=C.UTF-8 DEFAULT_CODEPAGE=437

# mkvm DIR: a VM with a 20 MB disk (one FAT16 partition at sector 63) and a
# CD drive; sets HD, C (the partition, for mtools) and VM.
mkvm() {
    VM="$1"
    mkdir -p "$VM"
    HD="$VM/hd.img"
    truncate -s $((40 * 16 * 63 * 512)) "$HD"
    python3 -c '
import struct, sys
mbr = bytearray(512)
mbr[446:462] = bytes([0x80, 1, 1, 0, 0x06, 15, 63, 39]) + struct.pack("<II", 63, 40 * 16 * 63 - 63)
mbr[510:] = b"\x55\xaa"
open(sys.argv[1], "r+b").write(mbr)' "$HD"
    mformat -i "$HD@@32256" -T $((40 * 16 * 63 - 63)) -h 16 -s 63 -H 63 :: > /dev/null
    printf '[Floppy and CD-ROM drives]\ncdrom_01_parameters = 1, atapi\ncdrom_01_ide_channel = 1:0\n' > "$VM/86box.cfg"
    C="$HD@@32256"
}

# mz FILE [SIZE]: a fake DOS program.
mz() { python3 -c '
import sys
size = int(sys.argv[2]) if len(sys.argv) > 2 else 600
b = bytearray(size); b[0:2] = b"MZ"; b[0x18] = 0x1C
open(sys.argv[1], "wb").write(b)' "$@"; }
imp() { timeout 120 python3 "$IMPORT" "$@"; }
bad() { echo "FAIL: case $N: $(printf '%s' "$*" | tr '\n' ' ' | cut -c1-700)"; exit 1; }
# bat_for GAME: the ::/X.BAT files whose mark line names exactly GAME.
bat_for() {
    mdir -b -i "$C" '::/*.BAT' 2> /dev/null | while IFS= read -r p; do
        [ -n "$p" ] && mtype -i "$C" "$p" | tr -d '\r' | grep -qxF "rem gog_import.py: $1" && echo "$p"
    done
    true
}
# one BATS GAME: exactly one batch file for GAME.
one() { [ -n "$1" ] && [ "$(printf '%s\n' "$1" | wc -l)" = 1 ] || bad "expected one batch file for '$2', found: [$1]"; }
lines() { mtype -i "$C" "$1" | tr -d '\r'; }
lastline() { lines "$1" | tail -1; }
cdline() { lines "$1" | grep -i '^cd ' | tail -1 | sed 's/^[cC][dD] //'; }
shown() { lines "$1" | tr '\n' '|'; }
tree() { mdir -/ -b -i "$C" ::/; }
# dos_finds BAT: DOS, given the batch file's bytes, finds its cd folder and
# its program (8.3 directory entries, CP437, read from the FAT directly).
dos_finds() {
    mtype -i "$C" "$1" > "$W/raw.bat"
    python3 "$W/dosfind.py" "$HD" 32256 "$W/raw.bat"
}

cat > "$W/dosfind.py" << 'EOF'
import struct, sys
img, off, bat = sys.argv[1], int(sys.argv[2]), sys.argv[3]
f = open(img, "rb")


def rd(pos, n):
    f.seek(off + pos)
    return f.read(n)


bs = rd(0, 512)
bps, spc, res, nfats, nroot, tot16, _, fatsz = struct.unpack_from("<HBHBHHBH", bs, 11)
total = tot16 or struct.unpack_from("<I", bs, 32)[0]
root_at = (res + nfats * fatsz) * bps
data_at = root_at + nroot * 32
fat12 = (total - data_at // bps) // spc < 4085


def nxt(cl):
    if fat12:
        v = struct.unpack("<H", rd(res * bps + cl * 3 // 2, 2))[0]
        v = v >> 4 if cl & 1 else v & 0xFFF
        return v if v < 0xFF8 else 0xFFFF
    return struct.unpack("<H", rd(res * bps + 2 * cl, 2))[0]


def entries(cl):
    """{8.3 name bytes: (attributes, first cluster)} of a folder (0: root)."""
    if cl == 0:
        data = rd(root_at, nroot * 32)
    else:
        data, seen = b"", set()
        while 2 <= cl < 0xFFF8 and cl not in seen:
            seen.add(cl)
            data += rd(data_at + (cl - 2) * spc * bps, spc * bps)
            cl = nxt(cl)
    out = {}
    for i in range(0, len(data), 32):
        e = data[i:i + 32]
        if e[0] == 0:
            break
        if e[0] == 0xE5 or e[11] == 0x0F or e[11] & 8:
            continue
        name, ext = e[:8].rstrip(b" "), e[8:11].rstrip(b" ")
        if name[:1] == b"\x05":
            name = b"\xe5" + name[1:]
        out[name + (b"." + ext if ext else b"")] = (e[11], struct.unpack_from("<H", e, 26)[0])
    return out


def up(b):  # DOS's upper case, code page 437
    return b.decode("cp437").upper().encode("cp437", "replace")


lines = [l for l in open(bat, "rb").read().split(b"\r\n") if l.strip()]
cl = 0
for line in lines:
    if line[:3].lower() != b"cd ":
        continue
    path = line[3:].strip()
    if path.startswith(b"\\"):
        cl = 0
    for part in [p for p in path.split(b"\\") if p and p != b"."]:
        there = entries(cl)
        hit = there.get(up(part))
        if not hit or not hit[0] & 0x10:
            sys.exit("%r: DOS finds no folder %r (there: %r)" % (line, part, sorted(there)))
        cl = hit[1]
prog = lines[-1].split()[0] if lines else b""
there = entries(cl)
want = [up(prog)] if b"." in prog else [up(prog) + e for e in (b".COM", b".EXE", b".BAT")]
if not any(w in there and not there[w][0] & 0x10 for w in want):
    sys.exit("%r: DOS finds no such program there (%r)" % (prog, sorted(there)))
EOF

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

# 1. Long folder and program names.
case_1() {
    mkvm "$W/1/vm"
    mkdir -p "$W/1/src/DoomCol/Doom II" "$W/1/src/DoomCol/Ultimate Doom" "$W/1/KEEN"
    mz "$W/1/src/DoomCol/Doom II/DOOM2.EXE" 40000
    echo wad > "$W/1/src/DoomCol/Ultimate Doom/DOOM.WAD"  # keeps DoomCol the game's folder
    (cd "$W/1/src" && python3 -m zipfile -c "$W/1/Doom Collection.zip" DoomCol)
    imp "$W/1/Doom Collection.zip" "$HD" > "$W/1/a.out" 2>&1 || bad "Doom Collection: import failed: $(cat "$W/1/a.out")"
    B=$(bat_for "Doom Collection")
    one "$B" "Doom Collection"
    long=$(tree | grep -i '/DOOM2\.EXE$')
    [ "$(printf '%s\n' "$long" | wc -l)" = 1 ] || bad "DOOM2.EXE should be on C: once: [$long]"
    short=$(mshortname -i "$C" "${long%/*}")  # ::/DOOMCOL/DOOMII~1
    want='\'$(printf '%s' "${short#::/}" | tr / '\\')
    [ "$(cdline "$B")" = "$want" ] || bad "Doom: the cd should be the 8.3 path DOS sees ($want): $(shown "$B")"
    mdir -b -i "$C" "$short" > /dev/null 2>&1 || bad "Doom: $short isn't a folder on C:"
    [ "$(lastline "$B")" = DOOM2.EXE ] || bad "Doom: the batch file should run DOOM2.EXE: $(shown "$B")"
    err=$(dos_finds "$B" 2>&1) || bad "Doom: $err"

    mz "$W/1/KEEN/Keen Dreams.exe" 30000
    imp "$W/1/KEEN" "$HD" > "$W/1/b.out" 2>&1 || bad "KEEN: import failed: $(cat "$W/1/b.out")"
    B=$(bat_for KEEN)
    one "$B" KEEN
    long=$(tree | grep -i '/Keen Dreams\.exe$')
    [ "$(printf '%s\n' "$long" | wc -l)" = 1 ] || bad "'Keen Dreams.exe' should be on C: once: [$long]"
    short=$(mshortname -i "$C" "$long")
    [ "$(lastline "$B")" = "${short##*/}" ] || bad "KEEN: the batch file should run ${short##*/}: $(shown "$B")"
    lastline "$B" | grep -q ' ' && bad "KEEN: the program line has a long name: $(shown "$B")"
    err=$(dos_finds "$B" 2>&1) || bad "KEEN: $err"
    true
}

# 2. Wing Commander II, then Wing Commander.
case_2() {
    mkvm "$W/2/vm"
    mkdir -p "$W/2/a/WC-2" "$W/2/b/WC-1"
    mz "$W/2/a/WC-2/WC2.EXE"
    echo two > "$W/2/a/WC-2/WC2.DAT"
    mz "$W/2/b/WC-1/WC.EXE"
    echo one > "$W/2/b/WC-1/WC.DAT"
    (cd "$W/2/a" && python3 -m zipfile -c "$W/2/Wing Commander II.zip" WC-2)
    (cd "$W/2/b" && python3 -m zipfile -c "$W/2/Wing Commander.zip" WC-1)
    imp "$W/2/Wing Commander II.zip" "$HD" > "$W/2/a.out" 2>&1 || bad "WC II: import failed: $(cat "$W/2/a.out")"
    imp "$W/2/Wing Commander.zip" "$HD" > "$W/2/b.out" 2>&1 || bad "WC: import failed: $(cat "$W/2/b.out")"
    B2=$(bat_for "Wing Commander II")
    one "$B2" "Wing Commander II"
    B1=$(bat_for "Wing Commander")
    one "$B1" "Wing Commander"
    [ "$B1" != "$B2" ] || bad "both games share $B1"
    [ "$(cdline "$B2")" = '\WC-2' ] && [ "$(lastline "$B2")" = WC2.EXE ] || bad "WC II's batch file: $(shown "$B2")"
    [ "$(cdline "$B1")" = '\WC-1' ] && [ "$(lastline "$B1")" = WC.EXE ] || bad "WC's batch file: $(shown "$B1")"
    imp "$W/2/Wing Commander II.zip" "$HD" > "$W/2/c.out" 2>&1 || bad "WC II again: import failed: $(cat "$W/2/c.out")"
    [ "$(bat_for "Wing Commander II")" = "$B2" ] || bad "WC II again: its batch file should stay $B2: [$(bat_for "Wing Commander II")]"
    grep -qF "copied to C:\\WC-2; run C:\\${B2#::/}." "$W/2/c.out" || bad "WC II again should reuse C:\\WC-2 and ${B2#::/}: $(cat "$W/2/c.out")"
    [ "$(tree | grep -ci '/WC2\.EXE$')" = 1 ] || bad "WC II again: a second copy: $(tree | grep -i 'WC2\.EXE')"
    [ "$(cdline "$B1")" = '\WC-1' ] && [ "$(lastline "$B1")" = WC.EXE ] || bad "WC's batch file changed: $(shown "$B1")"
    true
}

# 3. A launcher batch that runs the game's INTRO.
case_3() {
    mkvm "$W/3/vm"
    mkdir -p "$W/3/UFO"
    mz "$W/3/UFO/INTRO.EXE" 20000
    mz "$W/3/UFO/GEOSCAPE.EXE" 300000
    mz "$W/3/UFO/TACTICAL.EXE" 400000
    printf 'intro\r\n:loop\r\ngeoscape\r\ntactical\r\n' > "$W/3/UFO/UFO.BAT"
    imp "$W/3/UFO" "$HD" > "$W/3/a.out" 2>&1 || bad "import failed: $(cat "$W/3/a.out")"
    B=$(bat_for UFO)
    one "$B" UFO
    [ "$(lastline "$B")" = UFO.BAT ] || bad "UFO.BAT should be started: $(shown "$B") / $(cat "$W/3/a.out")"
    grep -q 'UFO.BAT (a batch file for DOSBox' "$W/3/a.out" && bad "UFO.BAT is called a DOSBox batch file: $(cat "$W/3/a.out")"
    err=$(dos_finds "$B" 2>&1) || bad "$err"
    true
}

# 4. DM.EXE: the game, but not next to DOOM.EXE.
case_4() {
    mkvm "$W/4/vm"
    mkdir -p "$W/4/a/DM" "$W/4/DOOM"
    mz "$W/4/a/DM/DM.EXE" 60000
    echo dungeon > "$W/4/a/DM/DUNGEON.DAT"
    (cd "$W/4/a" && python3 -m zipfile -c "$W/4/Dungeon Master (1992)(FTL).zip" DM)
    imp "$W/4/Dungeon Master (1992)(FTL).zip" "$HD" > "$W/4/a.out" 2>&1 || bad "Dungeon Master: import failed: $(cat "$W/4/a.out")"
    B=$(bat_for "Dungeon Master")
    one "$B" "Dungeon Master"
    [ "$(lastline "$B")" = DM.EXE ] || bad "Dungeon Master: DM.EXE should be started: $(shown "$B") / $(cat "$W/4/a.out")"
    mz "$W/4/DOOM/DOOM.EXE" 500000
    mz "$W/4/DOOM/DM.EXE" 30000
    imp "$W/4/DOOM" "$HD" > "$W/4/b.out" 2>&1 || bad "DOOM: import failed: $(cat "$W/4/b.out")"
    B=$(bat_for DOOM)
    one "$B" DOOM
    [ "$(lastline "$B")" = DOOM.EXE ] || bad "DOOM: DOOM.EXE should be started: $(shown "$B")"
    true
}

# 5. No program looks like the game; then --run.
case_5() {
    mkvm "$W/5/vm"
    mkdir -p "$W/5/HYPER"
    mz "$W/5/HYPER/HELP.EXE" 20000
    mz "$W/5/HYPER/VIEW.EXE" 30000
    echo pages > "$W/5/HYPER/HYPER.DAT"
    imp "$W/5/HYPER" "$HD" > "$W/5/a.out" 2>&1
    B1=$(bat_for HYPER)
    one "$B1" HYPER
    [ "$(cdline "$B1")" = '\HYPER' ] || bad "the first batch file should go into C:\\HYPER: $(shown "$B1")"
    for n in $(grep -o 'C:\\[A-Za-z0-9_~-]*\.BAT' "$W/5/a.out" | sort -u); do
        [ "$n" = "C:\\${B1#::/}" ] || bad "the first run names $n, but wrote ${B1#::/}: $(cat "$W/5/a.out")"
    done
    imp "$W/5/HYPER" "$HD" --run VIEW.EXE > "$W/5/b.out" 2>&1 || bad "--run VIEW.EXE: import failed: $(cat "$W/5/b.out")"
    B2=$(bat_for HYPER)
    [ "$B2" = "$B1" ] || bad "--run should rewrite $B1, not [$B2]: $(cat "$W/5/b.out")"
    [ "$(cdline "$B2")" = '\HYPER' ] && [ "$(lastline "$B2")" = VIEW.EXE ] || bad "--run VIEW.EXE's batch file: $(shown "$B2")"
    [ "$(tree | grep -ci '/VIEW\.EXE$')" = 1 ] || bad "a second copy: $(tree | grep -i 'VIEW\.EXE')"
    tree | grep -qi '^::/HYPER1' && bad "a second folder: $(tree | grep -i '^::/HYPER1')"
    true
}

# 6. The program disk sorts after the data disk.
case_6() {
    mkvm "$W/6/vm"
    mkdir -p "$W/6/src"
    P="$W/6/src/Pinball (1990)(Pub)(Program Disk).img"
    D="$W/6/src/Pinball (1990)(Pub)(Data Disk).img"
    mformat -C -f 1440 -i "$P" ::
    mformat -C -f 1440 -i "$D" ::
    mz "$W/6/PINBALL.EXE" 40000
    echo tables > "$W/6/TABLES.DAT"
    mcopy -i "$P" "$W/6/PINBALL.EXE" ::/PINBALL.EXE
    mcopy -i "$D" "$W/6/TABLES.DAT" ::/TABLES.DAT
    (cd "$W/6/src" && python3 -m zipfile -c "$W/6/Pinball (1990)(Pub).zip" "${P##*/}" "${D##*/}")
    imp "$W/6/Pinball (1990)(Pub).zip" "$HD" > "$W/6/a.out" 2>&1 || bad "import failed: $(cat "$W/6/a.out")"
    B=$(bat_for Pinball)
    one "$B" Pinball
    [ "$(lastline "$B")" = PINBALL.EXE ] || bad "PINBALL.EXE should be started: $(shown "$B")"
    err=$(dos_finds "$B" 2>&1) || bad "$err"
    true
}

# 7. INSTALL.BAT runs DEICE with its arguments.
case_7() {
    mkvm "$W/7/vm"
    mkdir -p "$W/7/src"
    mz "$W/7/src/DEICE.EXE" 20000
    printf 'deice dooms_19.1 C:\\DOOMS\r\n' > "$W/7/src/INSTALL.BAT"
    head -c 5000 /dev/zero | tr '\0' a > "$W/7/src/DOOMS_19.1"
    head -c 5000 /dev/zero | tr '\0' b > "$W/7/src/DOOMS_19.2"
    (cd "$W/7/src" && python3 -m zipfile -c "$W/7/doom19s.zip" DEICE.EXE INSTALL.BAT DOOMS_19.1 DOOMS_19.2)
    imp "$W/7/doom19s.zip" "$HD" > "$W/7/a.out" 2>&1 || bad "import failed: $(cat "$W/7/a.out")"
    B=$(bat_for doom19s)
    one "$B" doom19s
    [ "$(lastline "$B")" = INSTALL.BAT ] || bad "INSTALL.BAT should be run: $(shown "$B")"
    true
}

# 8. A non-ASCII folder name.
case_8() {
    mkvm "$W/8/vm"
    mkdir -p "$W/8/JEU/Données" "$W/8/JEU/MANUEL"
    mz "$W/8/JEU/Données/JEU.EXE"
    echo cfg > "$W/8/JEU/JEU.CFG"  # keeps JEU the game's folder
    echo page > "$W/8/JEU/MANUEL/PAGE1.DAT"
    imp "$W/8/JEU" "$HD" > "$W/8/a.out" 2>&1 || bad "import failed: $(cat "$W/8/a.out")"
    B=$(bat_for JEU)
    one "$B" JEU
    mtype -i "$C" "$B" > "$W/8/raw.bat"
    python3 -c '
import sys
cd = [l for l in open(sys.argv[1], "rb").read().split(b"\r\n") if l.lower().startswith(b"cd ")]
if len(cd) != 1 or b"\xc3" in cd[0] or b"?" in cd[0] or not cd[0].lower().startswith(b"cd \\jeu\\"):
    sys.exit("the cd line should be CP437 bytes: %r" % cd)' "$W/8/raw.bat" 2> "$W/8/err" || bad "$(cat "$W/8/err")"
    err=$(dos_finds "$B" 2>&1) || bad "$err ($(cat "$W/8/a.out"))"
    [ "$(lastline "$B")" = JEU.EXE ] || bad "JEU.EXE should be started: $(shown "$B")"
    true
}

# 9. Install floppies and a folder of scans.
case_9() {
    mkvm "$W/9/vm"
    mkdir -p "$W/9/src/Scans"
    mformat -C -f 1440 -i "$W/9/src/Disk1.img" ::
    mformat -C -f 1440 -i "$W/9/src/Disk2.img" ::
    mz "$W/9/INSTALL.EXE" 30000
    echo two > "$W/9/DATA2.DAT"
    mcopy -i "$W/9/src/Disk1.img" "$W/9/INSTALL.EXE" ::/INSTALL.EXE
    mcopy -i "$W/9/src/Disk2.img" "$W/9/DATA2.DAT" ::/DATA2.DAT
    echo jpg > "$W/9/src/Scans/box.jpg"
    echo png > "$W/9/src/Scans/codewheel.png"
    (cd "$W/9/src" && python3 -m zipfile -c "$W/9/Space Pirates (1993).zip" Disk1.img Disk2.img Scans)
    imp "$W/9/Space Pirates (1993).zip" "$HD" > "$W/9/a.out" 2>&1 || bad "import failed: $(cat "$W/9/a.out")"
    B=$(bat_for "Space Pirates")
    one "$B" "Space Pirates"
    [ "$B" = ::/SPACEPIR.BAT ] || bad "the batch file should be named after the game (SPACEPIR.BAT), not $B"
    [ "$(cdline "$B")" = '\SPACEPIR\DISK1' ] || bad "the cd should be \\SPACEPIR\\DISK1: $(shown "$B")"
    [ "$(lastline "$B")" = INSTALL.EXE ] || bad "DISK1's INSTALL.EXE should be run: $(shown "$B")"
    mdir -b -i "$C" ::/ | grep -qi '^::/SCANS' && bad "SCANS names something at C:\\: $(mdir -b -i "$C" ::/ | tr '\n' ' ')"
    err=$(dos_finds "$B" 2>&1) || bad "$err"
    true
}

# 10. A variant launcher.
case_10() {
    mkvm "$W/10/vm"
    mkdir -p "$W/10/WOLF3D"
    mz "$W/10/WOLF3D/WOLF3D.EXE" 110000
    mz "$W/10/WOLF3D/INSTALL.EXE" 40000
    printf 'wolf3d nosound\r\n' > "$W/10/WOLF3D/NOSOUND.BAT"
    imp "$W/10/WOLF3D" "$HD" > "$W/10/a.out" 2>&1 || bad "import failed: $(cat "$W/10/a.out")"
    B=$(bat_for WOLF3D)
    one "$B" WOLF3D
    [ "$(lastline "$B")" = WOLF3D.EXE ] || bad "WOLF3D.EXE should be started: $(shown "$B")"
    true
}

# 11. A DOSBox config whose C: is missing.
case_11() {
    mkvm "$W/11/vm"
    mkdir -p "$W/11/MYGAME"
    printf '\xb4\x4c\xcd\x21' > "$W/11/MYGAME/GAME.COM"
    mz "$W/11/MYGAME/GAME.EXE" 40000
    mz "$W/11/MYGAME/OTHER.EXE" 90000
    printf '[autoexec]\r\nmount c "D:\\Stuff\\nothere"\r\nc:\r\ncd mygame\r\ngame -nosound\r\n' > "$W/11/MYGAME/dosbox_game.conf"
    imp "$W/11/MYGAME" "$HD" > "$W/11/a.out" 2>&1 || bad "import failed: $(cat "$W/11/a.out")"
    B=$(bat_for MYGAME)
    one "$B" MYGAME
    [ "$(lastline "$B")" = "GAME.COM -nosound" ] || bad "GAME.COM -nosound should be run: $(shown "$B")"
    grep 'Other programs there' "$W/11/a.out" | grep -qi 'GAME\.COM' &&
        bad "GAME.COM is listed as another program: $(grep 'Other programs' "$W/11/a.out")"
    true
}

# 12. --batch.
case_12() {
    mkvm "$W/12/vm"
    mkdir -p "$W/12/KEEN4" "$W/12/WOLF3D"
    mz "$W/12/KEEN4/KEEN4E.EXE"
    mz "$W/12/WOLF3D/WOLF3D.EXE"
    imp "$W/12/KEEN4" "$HD" --batch KEEN.BAT > "$W/12/a.out" 2>&1 || bad "--batch KEEN.BAT: import failed: $(cat "$W/12/a.out")"
    [ "$(bat_for KEEN4)" = ::/KEEN.BAT ] || bad "--batch KEEN.BAT should write C:\\KEEN.BAT: [$(bat_for KEEN4)] $(mdir -b -i "$C" ::/ | tr '\n' ' ')"
    mdir -b -i "$C" ::/ | grep -qi 'BAT\.BAT\|KEENBA~' && bad "a KEEN.BAT.BAT was written"
    grep -qi 'BAT\.BAT' "$W/12/a.out" && bad "the report names KEEN.BAT.BAT: $(cat "$W/12/a.out")"
    grep -qF 'run C:\KEEN.BAT.' "$W/12/a.out" || bad "the report should say to run C:\\KEEN.BAT: $(cat "$W/12/a.out")"
    before=$(tree)
    imp "$W/12/WOLF3D" "$HD" --batch WOLFENSTEIN > "$W/12/b.out" 2>&1 && bad "--batch WOLFENSTEIN should be refused: $(cat "$W/12/b.out")"
    grep -q Traceback "$W/12/b.out" && bad "--batch WOLFENSTEIN: a traceback: $(cat "$W/12/b.out")"
    grep -q -- '--batch' "$W/12/b.out" || bad "the refusal should name --batch: $(cat "$W/12/b.out")"
    [ "$(tree)" = "$before" ] || bad "--batch WOLFENSTEIN changed C:: $(tree | tr '\n' ' ')"
    true
}

# 13. Imported again, with the space its first copy holds.
case_13() {
    mkvm "$W/13/vm"
    mkdir -p "$W/13/BIGGAME"
    mz "$W/13/BIGGAME/BIGGAME.EXE" 100000
    mz "$W/13/BIGGAME/OTHER.EXE" 50000
    head -c $((12 << 20)) /dev/zero > "$W/13/BIGGAME/DATA.BIN"
    imp "$W/13/BIGGAME" "$HD" > "$W/13/a.out" 2>&1 || bad "first import failed: $(cat "$W/13/a.out")"
    imp "$W/13/BIGGAME" "$HD" --run OTHER.EXE > "$W/13/b.out" 2>&1 || bad "--run OTHER.EXE failed: $(cat "$W/13/b.out")"
    B=$(bat_for BIGGAME)
    one "$B" BIGGAME
    [ "$(cdline "$B")" = '\BIGGAME' ] && [ "$(lastline "$B")" = OTHER.EXE ] || bad "the batch file should run OTHER.EXE in \\BIGGAME: $(shown "$B")"
    [ "$(tree | grep -ci '/DATA\.BIN$')" = 1 ] || bad "a second copy: $(tree | grep -i 'DATA\.BIN')"
    [ "$(mtype -i "$C" ::/BIGGAME/DATA.BIN | wc -c)" = $((12 << 20)) ] || bad "DATA.BIN isn't whole"
    true
}

# 14. A CD image inside the game's folder.
case_14() {
    mkvm "$W/14/vm"
    mkdir -p "$W/14/src/SPACE/CD" "$W/14/iso"
    mz "$W/14/src/SPACE/SPACE.EXE" 40000
    echo "cd data" > "$W/14/iso/space.dat"
    python3 "$W/mkiso.py" "$W/14/src/SPACE/CD/space.iso" SPACECD "SPACE.DAT=$W/14/iso/space.dat"
    (cd "$W/14/src" && python3 -m zipfile -c "$W/14/Space.zip" SPACE)
    imp "$W/14/Space.zip" "$HD" > "$W/14/a.out" 2>&1 || bad "import failed: $(cat "$W/14/a.out")"
    tree | grep -qi '\.iso$\|/CD/$' && bad "the CD reached C:: $(tree | tr '\n' ' ')"
    tree | grep -qi '/SPACE\.EXE$' || bad "SPACE.EXE should be on C:: $(tree | tr '\n' ' ')"
    path=$(sed -n 's/^cdrom_01_image_path = //p' "$VM/86box.cfg")
    case "$path" in
        cd/*/space.iso) ;;
        *) bad "the ISO should be in the drive: [$path] $(cat "$W/14/a.out")" ;;
    esac
    cmp -s "$VM/$path" "$W/14/src/SPACE/CD/space.iso" || bad "$VM/$path isn't the game's ISO"
    true
}

# 15. --dir with a nested folder, either separator.
case_15() {
    mkdir -p "$W/15/SR"
    mz "$W/15/SR/SECOND.EXE" 40000
    echo music > "$W/15/SR/MUSIC.DAT"
    k=0
    for d in 'GAMES\SR' GAMES/SR; do
        k=$((k + 1))
        mkvm "$W/15/vm$k"
        imp "$W/15/SR" "$HD" --dir "$d" > "$VM/out" 2>&1 || bad "--dir $d: import failed: $(cat "$VM/out")"
        [ "$(tree | grep -i 'SECOND\.EXE$\|MUSIC\.DAT$' | sort | tr '\n' ' ')" = "::/GAMES/SR/MUSIC.DAT ::/GAMES/SR/SECOND.EXE " ] ||
            bad "--dir $d: the files should be in C:\\GAMES\\SR only: $(tree | tr '\n' ' ')"
        B=$(bat_for SR)
        one "$B" SR
        [ "$(cdline "$B")" = '\GAMES\SR' ] && [ "$(lastline "$B")" = SECOND.EXE ] || bad "--dir $d: $(shown "$B")"
        err=$(dos_finds "$B" 2>&1) || bad "--dir $d: $err"
    done
    true
}

# 16. The sound setup by its DOS name; a game bigger than C:.
case_16() {
    mkvm "$W/16/vm"
    mkdir -p "$W/16/Star Game" "$W/16/HUGE"
    mz "$W/16/Star Game/STARGAME.EXE" 40000
    mz "$W/16/Star Game/SoundSetup.exe" 9000
    imp "$W/16/Star Game" "$HD" > "$W/16/a.out" 2>&1 || bad "import failed: $(cat "$W/16/a.out")"
    grep -qF 'type CD \STARGAME, then SOUNDS~1.EXE' "$W/16/a.out" ||
        bad "the sound setup should be named as DOS has it: $(cat "$W/16/a.out")"
    mz "$W/16/HUGE/HUGE.EXE" 40000
    truncate -s $((25 << 20)) "$W/16/HUGE/DATA.BIN"
    imp "$W/16/HUGE" "$HD" > "$W/16/b.out" 2>&1 && bad "a 25 MB game on a 20 MB C: should be refused: $(cat "$W/16/b.out")"
    grep -qF "doesn't fit on this VM's C:" "$W/16/b.out" || bad "it should say the game can't fit at all: $(cat "$W/16/b.out")"
    true
}

FAILS=()
for n in $(seq 1 16); do
    (
        set -e
        N=$n
        "case_$n"
    ) > "$W/case$n.log" 2>&1
    rc=$?
    if [ $rc = 0 ]; then
        echo "ok: case $n"
    else
        msg=$(grep -m1 "^FAIL: case $n:" "$W/case$n.log" ||
            echo "FAIL: case $n: a step failed (exit $rc): $(tail -5 "$W/case$n.log" | tr '\n' ' ')")
        echo "$msg"
        FAILS+=("$msg")
    fi
done
if [ ${#FAILS[@]} = 0 ]; then
    echo PASS
else
    echo "${#FAILS[@]} of 16 cases failed:"
    printf '%s\n' "${FAILS[@]}"
    exit 1
fi
