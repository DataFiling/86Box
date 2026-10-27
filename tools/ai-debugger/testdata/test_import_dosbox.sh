#!/bin/bash
# gog_import.py and DOSBox configurations (GOG's and other packagers'):
# which commands reach the batch file and which files reach C:. Each case
# builds its own VM (a 20 MB disk with one FAT16 partition at sector 63,
# 86box.cfg with a CD drive) and fake programs (MZ headers, never run),
# imports with --json and checks the image: recursive listings, file
# contents, and the batch file walked through as DOS would (each cd must
# reach a folder on the image, each program must be in the folder it runs
# in).
#   1. GOG's config given by its path (GOG Games/Wolf/dosboxWolf_single.conf,
#      mount C ".." from DOSBOX\) next to another game: Wolf's files only.
#   2. Two eXoDOS-like dosbox.conf's: two batch files and folders, no DOSBOX.
#   3. C:\UW taken by another game: only UW goes into C:\NEW\UW, the cds
#      (cd \UW\DATA, cd \UW) reach their folders, and --run UW.EXE works.
#   4. 'Wolfenstein 3D [GOG]': its config is read (its CD attached); an
#      upper-case PACK/DOSBOX.CONF is found.
#   5. imgmount 2 ... -fs none, and imgmount e game.img (no -t): no CD.
#   6. --run 'loadfix TYRIAN.EXE': LOADFIX kept, TYRIAN.EXE resolved.
#   7. A game the old importer put into C:\TYRIAN (empty mark line): reused.
#   8. d:, cd \, c:, cd GAME, GAME.EXE: the second c: is kept.
#   9. server_backup/PACK/game.conf is read; dosbox-0.74-3.conf (no mounts)
#      next to game.conf doesn't hide it.
#  10. Keen4/dosbox.conf mounting C:\Games\Keen4: the config is used.
#  11. MOO2 with WIN95/setup_win95.exe (a PE, not Inno): a plain game.
#  12. noarch/ with dosbox_keen4.conf and dosbox_keen5.conf: one game's
#      commands, the other config reported.
#  13. A GOG config's 'intro' with GAME\INTRO.EXE: kept in the batch file.
#  14. A config with memsize=64 and cycles=fixed 3000 on a 32 MB VM: both
#      pointed out; memsize=16 and cycles=max: neither mentioned.
# Keeps going after a failed case; prints "FAIL: case N: why" for each and
# "PASS" only when all pass. KEEP=1 keeps the work folder.

set -u
HERE=$(cd "$(dirname "$0")" && pwd)
IMPORT=${IMPORT:-$HERE/../gog_import.py}  # IMPORT=... tests another copy
W=$(mktemp -d -t dosbox-test.XXXXXX)
trap '[ -n "${KEEP:-}" ] && echo "work folder: $W" || rm -rf "$W"' EXIT
: > "$W/fails"
N=0

fail() { echo "FAIL: case $N: $(printf '%s' "$*" | tr '\n' ' ')" | tee -a "$W/fails"; }

# newvm DIR: a VM folder with hd.img and 86box.cfg; sets VM, HD, C.
newvm() {
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
    mformat -i "$HD@@32256" -T $((40 * 16 * 63 - 63)) -h 16 -s 63 -H 63 ::
    printf '[Floppy and CD-ROM drives]\ncdrom_01_parameters = 1, atapi\ncdrom_01_ide_channel = 1:0\n' > "$VM/86box.cfg"
    C="$HD@@32256"
}

# mz FILE [SIZE]: a fake DOS program. pe FILE: a fake Windows one.
mz() { python3 -c '
import sys
size = int(sys.argv[2]) if len(sys.argv) > 2 else 600
b = bytearray(size); b[0:2] = b"MZ"; b[0x18] = 0x1C
open(sys.argv[1], "wb").write(b)' "$@"; }
pe() { python3 -c '
import struct, sys
b = bytearray(1024); b[0:2] = b"MZ"; b[0x18] = 0x40
b[0x3C:0x40] = struct.pack("<I", 0x80); b[0x80:0x84] = b"PE\0\0"
open(sys.argv[1], "wb").write(b)' "$1"; }

# imp OUT SOURCE IMAGE [ARGS]: import with --json (OUT.json, OUT.err).
imp() {
    local out=$1
    shift
    timeout 120 python3 -I "$IMPORT" "$@" --json > "$out.json" 2> "$out.err"
}
# js OUT EXPR: a value from OUT.json's report r.
js() { python3 -c '
import json, sys
r = json.load(open(sys.argv[1]))
v = eval(sys.argv[2])
print(v if not isinstance(v, (list, dict)) else json.dumps(v))' "$1.json" "$2" 2> /dev/null || true; }
why() { echo "$(tr '\n' ' ' < "$1.err" | cut -c1-600)"; }

# ls_all: every file and folder on C:, recursively (upper case).
ls_all() { mdir -/ -b -i "$C" ::/ 2> /dev/null | tr 'a-z' 'A-Z'; }
# gog GOGDIR NAME CONF: GOG's layout: DOSBOX\, goggame-1.info running CONF from it.
gog() {
    mkdir -p "$1/DOSBOX"
    printf 'MZ fake DOSBox' > "$1/DOSBOX/DOSBox.exe"
    printf '{"gameId": "1", "name": "%s", "playTasks": [{"isPrimary": true, "arguments": "-conf \\"..\\\\%s\\"", "workingDir": "DOSBOX"}]}\n' \
        "$2" "$3" > "$1/goggame-1.info"
}
# conf FILE LINES...: a DOSBox config whose [autoexec] has LINES.
conf() {
    local f=$1
    shift
    { printf '[sblaster]\nsbtype=sb16\n[autoexec]\n'; printf '%s\n' "$@"; } > "$f"
}

# batcheck BAT: walk C:\BAT as DOS would. Prints "RAN <command> IN C:\<folder>"
# for each program line and "BAD ..." for a cd or program that isn't there;
# fails if any.
cat > "$W/batcheck.py" << 'EOF'
import re, subprocess, sys

target, bat = sys.argv[1], sys.argv[2]


def mt(*a):
    return subprocess.run(a, capture_output=True, text=True, errors="replace")


r = mt("mtype", "-i", target, "::/" + bat)
if r.returncode:
    print("BAD no C:\\%s" % bat)
    sys.exit(1)
lines = [l.strip() for l in r.stdout.replace("\r", "").split("\n") if l.strip()]
print("LINES " + " | ".join(lines))
SHORT = re.compile(r"[A-Za-z0-9!#$%&'()\-@^_`{}~]{1,8}(\.[A-Za-z0-9!#$%&'()\-@^_`{}~]{1,3})?$")


def entries(folder):
    """{DOS name: is a folder} in C:\\folder, or None."""
    r = mt("mdir", "-b", "-i", target, "::/" + "/".join(folder))
    if r.returncode:
        return None
    out = {}
    for e in r.stdout.split("\n"):
        e = e.strip()
        if not e:
            continue
        name = e.rstrip("/").split("/")[-1]
        if not SHORT.fullmatch(name):  # DOS sees its 8.3 name
            s = mt("mshortname", "-i", target, e.rstrip("/")).stdout.strip()
            name = s.rstrip("/").split("/")[-1] or name
        out[name.upper()] = e.endswith("/")
    return out


INTERNAL = {"echo", "@echo", "cls", "exit", "pause", "set", "rem", "path", "prompt", "ver", "type", "dir",
            "goto", "if", "for", "shift", "ctty", "mode"}
drive, cwd, bad = "C", [], 0
for l in lines:
    words = l.split()
    low = words[0].lower()
    if low in INTERNAL or low.startswith("echo."):
        continue
    if re.fullmatch(r"[A-Za-z]:", l):
        drive = l[0].upper()
        continue
    m = re.match(r"cd(?:\s+|(?=[\\.]))(.*)$", l, re.I)
    if m:
        arg, d = m.group(1).strip(), drive
        if re.match(r"[A-Za-z]:", arg):
            d, arg = arg[0].upper(), arg[2:]
        if d != "C" or not arg:
            continue
        new = [] if arg.startswith("\\") else list(cwd)
        ok = True
        for part in arg.split("\\"):
            if part in ("", "."):
                continue
            if part == "..":
                new = new[:-1]
                continue
            ents = entries(new)
            if not ents or not ents.get(part.upper()):
                print("BAD %s: no folder C:\\%s" % (l, "\\".join(new + [part])))
                bad = ok = 0
                break
            new.append(part.upper())
        if ok:
            cwd = new
        continue
    at = 1 if len(words) > 1 and low in ("loadfix", "call", "lh", "loadhigh") else 0
    if drive != "C":
        print("RAN %s ON %s:" % (l, drive))
        continue
    prog = words[at].split("\\")
    folder = list(cwd)
    for part in prog[:-1]:
        folder = folder[:-1] if part == ".." else folder + [part.upper()]
    ents = entries(folder) or {}
    name = prog[-1].upper()
    want = [name] if "." in name else [name + e for e in (".COM", ".EXE", ".BAT")]
    if not any(ents.get(n) is False for n in want):
        print("BAD %s: no %s in C:\\%s" % (l, prog[-1], "\\".join(folder)))
        bad = 1
    print("RAN %s IN C:\\%s" % (l, "\\".join(cwd)))
sys.exit(1 if bad else 0)
EOF
batcheck() { python3 "$W/batcheck.py" "$C" "$1"; }

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

# ---- the cases ------------------------------------------------------------------

# 1. GOG's config given by its path.
case1() {
    local G="$W/1/GOG Games"
    newvm "$W/1/vm"
    mkdir -p "$G/Wolf" "$G/Other Game/DATA"
    mz "$G/Wolf/WOLF3D.EXE"
    echo wolf > "$G/Wolf/VSWAP.WL6"
    echo save > "$G/Other Game/DATA/SAVE.DAT"
    mz "$G/Other Game/OTHER.EXE"
    gog "$G/Wolf" "Wolfenstein 3D" dosboxWolf_single.conf
    conf "$G/Wolf/dosboxWolf_single.conf" 'mount C ".."' 'c:' 'WOLF3D.EXE'
    imp "$W/1/a" "$G/Wolf/dosboxWolf_single.conf" "$HD" || { fail "the import failed: $(why "$W/1/a")"; return; }
    [ "$(js "$W/1/a" 'r["kind"]')" = dosbox ] || fail "not imported from the config: $(why "$W/1/a")"
    local all
    all=$(ls_all)
    echo "$all" | grep -q 'SAVE\.DAT\|OTHER\|/WOLF/' && fail "another game's files (or the folder above) reached C:: $(echo "$all" | tr '\n' ' ')"
    [ "$(echo "$all" | grep -c '/WOLF3D\.EXE$')" = 1 ] || fail "WOLF3D.EXE should be on C: once: $(echo "$all" | tr '\n' ' ')"
    echo "$all" | grep -q '/VSWAP\.WL6$' || fail "Wolf's data should be copied"
    local bat out
    bat=$(js "$W/1/a" 'r["batch"].split("\\")[-1]')
    out=$(batcheck "$bat") || fail "the batch file doesn't run: $out"
    echo "$out" | grep -q '^RAN WOLF3D.EXE IN ' || fail "the batch file should run WOLF3D.EXE: $out"
}

# 2. eXoDOS-like dosbox.conf's, two games.
case2() {
    newvm "$W/2/vm"
    mkdir -p "$W/2/Commander Keen 4" "$W/2/Duke Nukem"
    mz "$W/2/Commander Keen 4/KEEN4E.EXE"
    echo keen > "$W/2/Commander Keen 4/WHO.TXT"
    conf "$W/2/Commander Keen 4/dosbox.conf" 'mount c .' 'c:' 'KEEN4E.EXE'
    mz "$W/2/Duke Nukem/DUKE1.EXE"
    echo duke > "$W/2/Duke Nukem/WHO.TXT"
    conf "$W/2/Duke Nukem/dosbox.conf" 'mount c .' 'c:' 'DUKE1.EXE'
    imp "$W/2/k" "$W/2/Commander Keen 4/dosbox.conf" "$HD" || { fail "Keen's import failed: $(why "$W/2/k")"; return; }
    imp "$W/2/d" "$W/2/Duke Nukem/dosbox.conf" "$HD" || { fail "Duke's import failed: $(why "$W/2/d")"; return; }
    local kb db kd dd
    kb=$(js "$W/2/k" 'r["batch"]') db=$(js "$W/2/d" 'r["batch"]') kd=$(js "$W/2/k" 'r["dir"]') dd=$(js "$W/2/d" 'r["dir"]')
    [ -n "$kb" ] && [ "$kb" != "$db" ] || fail "the two games should have their own batch files: $kb, $db"
    [ -n "$kd" ] && [ "$kd" != "$dd" ] || fail "the two games should have their own folders: $kd, $dd"
    case "$kb $db $kd $dd" in *DOSBOX*) fail "a batch file or folder is named DOSBOX: $kb $db $kd $dd" ;; esac
    mdir -b -i "$C" ::/ | tr a-z A-Z | grep -q '^::/DOSBOX' && fail "C:\\DOSBOX(.BAT) made: $(mdir -b -i "$C" ::/ | tr '\n' ' ')"
    local out
    out=$(batcheck "${kb##*\\}") || fail "Keen's batch file doesn't run: $out"
    echo "$out" | grep -q '^RAN KEEN4E.EXE IN ' || fail "Keen's batch file should run KEEN4E.EXE: $out"
    out=$(batcheck "${db##*\\}") || fail "Duke's batch file doesn't run: $out"
    echo "$out" | grep -q '^RAN DUKE1.EXE IN ' || fail "Duke's batch file should run DUKE1.EXE: $out"
    [ "$(mtype -i "$C" "::/${kd#C:\\}/WHO.TXT" 2> /dev/null | tr -d '\r')" = keen ] || fail "Keen's WHO.TXT was overwritten"
    [ "$(mtype -i "$C" "::/${dd#C:\\}/WHO.TXT" 2> /dev/null | tr -d '\r')" = duke ] || fail "Duke's WHO.TXT is wrong"
}

# 3. C:\UW is another game's.
case3() {
    newvm "$W/3/vm"
    mkdir -p "$W/3/other/UW"
    mz "$W/3/other/UW/UW.EXE"
    echo other > "$W/3/other/UW/WHO.TXT"
    imp "$W/3/o" "$W/3/other" "$HD" || { fail "the other game's import failed: $(why "$W/3/o")"; return; }
    [ "$(js "$W/3/o" 'r["dir"]')" = 'C:\UW' ] || { fail "setup: the other game should be in C:\\UW: $(why "$W/3/o")"; return; }
    local G="$W/3/Ultima Underworld"
    mkdir -p "$G/UW/DATA" "$G/EXTRAS"
    mz "$G/UW/UW.EXE"
    mz "$G/UW/DATA/PREP.EXE"
    echo uw > "$G/UW/WHO.TXT"
    echo bonus > "$G/EXTRAS/BONUS.DAT"
    gog "$G" "Ultima Underworld" dosbox_uw.conf
    conf "$G/dosbox_uw.conf" 'mount C ".."' 'c:' 'cd \UW\DATA' 'PREP.EXE' 'cd \UW' 'UW.EXE'
    imp "$W/3/a" "$G" "$HD" || { fail "the import failed: $(why "$W/3/a")"; return; }
    local dir all
    dir=$(js "$W/3/a" 'r["dir"]')
    case "$dir" in 'C:\UW' | '') fail "the game should go into a new folder, not $dir: $(why "$W/3/a")"; return ;; esac
    all=$(ls_all)
    local new="::/${dir#C:\\}"
    echo "$all" | grep -qx "$new/UW/UW.EXE" || fail "UW.EXE should be in $dir\\UW: $(echo "$all" | tr '\n' ' ')"
    echo "$all" | grep -qx "$new/UW/DATA/PREP.EXE" || fail "PREP.EXE should be in $dir\\UW\\DATA"
    echo "$all" | grep -q 'BONUS\|EXTRAS' && fail "the C: mount's other folders were copied: $(echo "$all" | tr '\n' ' ')"
    [ "$(echo "$all" | grep "^$new/" | grep -v "^$new/UW/" | grep -vx "$new/\(UW/\)\?")" = "" ] ||
        fail "$dir should hold only UW: $(echo "$all" | grep "^$new/" | tr '\n' ' ')"
    [ "$(mtype -i "$C" ::/UW/WHO.TXT | tr -d '\r')" = other ] || fail "C:\\UW (the other game) was changed"
    echo "$all" | grep -q '^::/UW/DATA' && fail "files went into the other game's C:\\UW"
    local bat out
    bat=$(js "$W/3/a" 'r["batch"].split("\\")[-1]')
    out=$(batcheck "$bat") || fail "the batch file's cds or programs don't resolve: $out"
    echo "$out" | grep -qx "RAN PREP.EXE IN ${dir//\\/\\\\}\\\\UW\\\\DATA" || fail "PREP.EXE should run in $dir\\UW\\DATA: $out"
    echo "$out" | grep -qx "RAN UW.EXE IN ${dir//\\/\\\\}\\\\UW" || fail "UW.EXE should run in $dir\\UW: $out"
    # --run UW.EXE in that state
    imp "$W/3/r" "$G" "$HD" --run UW.EXE || { fail "--run UW.EXE failed: $(why "$W/3/r")"; return; }
    [ "$(js "$W/3/r" 'r["dir"]')" = "$dir" ] || fail "--run should reuse $dir, not $(js "$W/3/r" 'r["dir"]')"
    out=$(batcheck "$(js "$W/3/r" 'r["batch"].split("\\")[-1]')") || fail "--run's batch file doesn't run: $out"
    echo "$out" | grep -qx "RAN UW.EXE IN ${dir//\\/\\\\}\\\\UW" || fail "--run UW.EXE should run in $dir\\UW: $out"
    [ "$(ls_all | grep -c '/UW\.EXE$')" = 2 ] || fail "--run copied the game again: $(ls_all | grep UW.EXE | tr '\n' ' ')"
}

# 4. Brackets in the folder's name; an upper-case DOSBOX.CONF.
case4() {
    newvm "$W/4/vm"
    local G="$W/4/Wolfenstein 3D [GOG]"
    mkdir -p "$G/MANUAL" "$W/4/cdsrc"
    mz "$G/WOLF3D.EXE" 3000
    mz "$G/MANUAL/BIGVIEW.EXE" 90000
    mz "$W/4/cdsrc/w.bin"
    python3 "$W/mkiso.py" "$G/WOLF.ISO" WOLFCD "MUSIC.EXE=$W/4/cdsrc/w.bin"
    gog "$G" "Wolfenstein 3D" dosboxWolf.conf
    conf "$G/dosboxWolf.conf" 'mount C ".."' 'imgmount d "..\WOLF.ISO" -t iso' 'c:' 'WOLF3D.EXE'
    imp "$W/4/a" "$G" "$HD" || { fail "the import failed: $(why "$W/4/a")"; return; }
    [ "$(js "$W/4/a" 'r["kind"]')" = dosbox ] || fail "[GOG]: the config should be read: $(why "$W/4/a")"
    grep -q '^cdrom_01_image_path = cd/.*/WOLF\.ISO$' "$W/4/vm/86box.cfg" || fail "[GOG]: the config's CD should be attached: $(cat "$W/4/vm/86box.cfg" | tr '\n' ' ')"
    newvm "$W/4/vm2"
    mkdir -p "$W/4/PACK/GAMEDIR"
    mz "$W/4/PACK/GAMEDIR/XYZ.EXE"
    mz "$W/4/PACK/GAMEDIR/BIGTOOL.EXE" 90000
    printf '[autoexec]\r\nmount c GAMEDIR\r\nc:\r\nXYZ.EXE\r\n' > "$W/4/PACK/DOSBOX.CONF"
    imp "$W/4/b" "$W/4/PACK" "$HD" || { fail "PACK's import failed: $(why "$W/4/b")"; return; }
    [ "$(js "$W/4/b" 'r["kind"]')" = dosbox ] || fail "DOSBOX.CONF should be read: $(why "$W/4/b")"
    [ "$(js "$W/4/b" 'r["runs"][-1]')" = XYZ.EXE ] || fail "DOSBOX.CONF's XYZ.EXE should run: $(js "$W/4/b" 'r["runs"]')"
}

# 5. Disk images aren't CDs.
case5() {
    newvm "$W/5/vm"
    local G="$W/5/Disk Game"
    mkdir -p "$G/GAME"
    mz "$G/GAME/GAME.EXE"
    head -c 65536 /dev/zero > "$G/save.img"
    mformat -C -f 1440 -i "$G/game.img" ::
    gog "$G" "Disk Game" dosbox_g.conf
    conf "$G/dosbox_g.conf" 'mount C ".."' 'imgmount 2 "..\save.img" -size 512,16,4,64 -fs none' \
        'imgmount e "..\game.img"' 'c:' 'cd GAME' 'GAME.EXE'
    imp "$W/5/a" "$G" "$HD" || { fail "the import failed: $(why "$W/5/a")"; return; }
    [ "$(js "$W/5/a" 'r["kind"]')" = dosbox ] || fail "the config should be read: $(why "$W/5/a")"
    grep -q '^cdrom_01_image_path' "$W/5/vm/86box.cfg" && fail "a disk image went into the CD drive: $(grep cdrom_01_image_path "$W/5/vm/86box.cfg")"
    [ -z "$(find "$W/5/vm" -iname 'save.img' -o -iname 'game.img')" ] || fail "a disk image was copied into the VM's cd/: $(find "$W/5/vm" -iname 'save.img' -o -iname 'game.img' | tr '\n' ' ')"
    [ "$(js "$W/5/a" 'r.get("cd", "")')" = "" ] || fail "the report names a CD: $(js "$W/5/a" 'r.get("cd")')"
}

# 6. --run 'loadfix TYRIAN.EXE'.
case6() {
    newvm "$W/6/vm"
    local G="$W/6/Tyrian 2000"
    mkdir -p "$G/TYRIAN"
    mz "$G/TYRIAN/TYRIAN.EXE"
    mz "$G/TYRIAN/SETUP.EXE"
    gog "$G" "Tyrian 2000" dosboxTyrian.conf
    conf "$G/dosboxTyrian.conf" 'mount C ".."' 'c:' 'cd TYRIAN' 'TYRIAN.EXE'
    imp "$W/6/a" "$G" "$HD" --run 'loadfix TYRIAN.EXE' || { fail "--run 'loadfix TYRIAN.EXE' failed: $(why "$W/6/a")"; return; }
    local bat out
    bat=$(js "$W/6/a" 'r["batch"].split("\\")[-1]')
    out=$(batcheck "$bat") || fail "the batch file doesn't run: $out"
    echo "$out" | grep -qx 'RAN loadfix TYRIAN.EXE IN C:\\TYRIAN' || fail "the batch file should run 'loadfix TYRIAN.EXE' in C:\\TYRIAN: $out"
    imp "$W/6/b" "$G" "$HD" --run 'LOADFIX tyrian' || { fail "--run 'LOADFIX tyrian' failed: $(why "$W/6/b")"; return; }
    out=$(batcheck "$bat") || fail "the batch file doesn't run: $out"
    echo "$out" | grep -qx 'RAN LOADFIX TYRIAN.EXE IN C:\\TYRIAN' || fail "'LOADFIX tyrian' should become 'LOADFIX TYRIAN.EXE': $out"
}

# 7. Imported before by the old importer (empty mark line).
case7() {
    newvm "$W/7/vm"
    local G="$W/7/OLDGOG/app"
    mkdir -p "$G/TYRIAN" "$G/DOSBOX"
    mz "$G/TYRIAN/TYRIAN.EXE"
    mz "$G/TYRIAN/SETUP.EXE"
    echo data > "$G/TYRIAN/TYRIAN.DAT"
    conf "$G/dosboxTyrian.conf" 'mount C ".."' 'c:' 'cd TYRIAN' 'TYRIAN.EXE'
    # The old importer's result: C:\TYRIAN, and GAME.BAT with an empty mark.
    mmd -i "$C" ::/TYRIAN
    mcopy -i "$C" "$G/TYRIAN/TYRIAN.EXE" "$G/TYRIAN/SETUP.EXE" "$G/TYRIAN/TYRIAN.DAT" ::/TYRIAN/
    printf '@echo off\r\nrem gog_import.py: \r\nc:\r\ncd \\TYRIAN\r\nTYRIAN.EXE\r\n' > "$W/7/GAME.BAT"
    mcopy -i "$C" "$W/7/GAME.BAT" ::/GAME.BAT
    local before after
    before=$(mdir -b -i "$C" ::/ | grep '/$' | sort)
    imp "$W/7/a" "$W/7/OLDGOG" "$HD" || { fail "the import failed: $(why "$W/7/a")"; return; }
    after=$(mdir -b -i "$C" ::/ | grep '/$' | sort)
    [ "$before" = "$after" ] || fail "a second folder was made: $(echo "$after" | tr '\n' ' ') ($(why "$W/7/a"))"
    [ "$(js "$W/7/a" 'r["dir"]')" = 'C:\TYRIAN' ] || fail "C:\\TYRIAN should be reused, not $(js "$W/7/a" 'r["dir"]')"
    [ "$(ls_all | grep -c '/TYRIAN\.EXE$')" = 1 ] || fail "TYRIAN.EXE is on C: more than once: $(ls_all | tr '\n' ' ')"
    local out
    out=$(batcheck "$(js "$W/7/a" 'r["batch"].split("\\")[-1]')") || fail "the batch file doesn't run: $out"
    echo "$out" | grep -qx 'RAN TYRIAN.EXE IN C:\\TYRIAN' || fail "the batch file should run TYRIAN.EXE in C:\\TYRIAN: $out"
}

# 8. A c: after d: is kept.
case8() {
    newvm "$W/8/vm"
    local G="$W/8/Drive Game"
    mkdir -p "$G/GAME"
    mz "$G/GAME/GAME.EXE"
    conf "$G/game.conf" 'mount c .' 'mount d . -t cdrom' 'd:' 'cd \' 'c:' 'cd GAME' 'GAME.EXE'
    imp "$W/8/a" "$G" "$HD" || { fail "the import failed: $(why "$W/8/a")"; return; }
    [ "$(js "$W/8/a" 'r["kind"]')" = dosbox ] || fail "the config should be read: $(why "$W/8/a")"
    local out
    out=$(batcheck "$(js "$W/8/a" 'r["batch"].split("\\")[-1]')") || fail "the batch file doesn't run: $out"
    echo "$out" | grep -qx 'RAN GAME.EXE IN C:\\GAME' || fail "GAME.EXE should run on C: in C:\\GAME: $out"
    echo "$out" | grep '^LINES' | grep -qi '| d: | .*| c: | .*cd GAME' || fail "the batch file should switch back to c: after d:: $out"
}

# 9. A 'server' parent folder; a mount-less dosbox-0.74-3.conf.
case9() {
    newvm "$W/9/vm"
    local P="$W/9/server_backup/PACK"
    mkdir -p "$P/GAMEDIR"
    mz "$P/GAMEDIR/XYZ.EXE"
    mz "$P/GAMEDIR/BIGTOOL.EXE" 90000
    printf '[autoexec]\nmount c GAMEDIR\nc:\nXYZ.EXE\n' > "$P/game.conf"
    imp "$W/9/a" "$P" "$HD" || { fail "server_backup/PACK's import failed: $(why "$W/9/a")"; return; }
    [ "$(js "$W/9/a" 'r["kind"]')" = dosbox ] || fail "server_backup/PACK/game.conf should be read: $(why "$W/9/a")"
    [ "$(js "$W/9/a" 'r["runs"][-1]')" = XYZ.EXE ] || fail "server_backup: XYZ.EXE should run: $(js "$W/9/a" 'r["runs"]')"
    newvm "$W/9/vm2"
    local Q="$W/9/Pack"
    mkdir -p "$Q/GAMEDIR"
    mz "$Q/GAMEDIR/XYZ.EXE"
    mz "$Q/GAMEDIR/BIGTOOL.EXE" 90000
    printf 'MZ' > "$Q/DOSBox.exe"
    printf '[sdl]\nfullscreen=false\n[dosbox]\nmachine=svga_s3\n[autoexec]\n# Lines in this section will be run at startup.\n# You can put your MOUNT lines here.\n' > "$Q/dosbox-0.74-3.conf"
    printf '[autoexec]\nmount c GAMEDIR\nc:\nXYZ.EXE\n' > "$Q/game.conf"
    imp "$W/9/b" "$Q" "$HD" || { fail "Pack's import failed: $(why "$W/9/b")"; return; }
    [ "$(js "$W/9/b" 'r["kind"]')" = dosbox ] || fail "game.conf next to dosbox-0.74-3.conf should be read: $(why "$W/9/b")"
    [ "$(js "$W/9/b" 'r["runs"][-1]')" = XYZ.EXE ] || fail "Pack: XYZ.EXE should run: $(js "$W/9/b" 'r["runs"]')"
    local out
    out=$(batcheck "$(js "$W/9/b" 'r["batch"].split("\\")[-1]')") || fail "Pack's batch file doesn't run: $out"
}

# 10. A mount c "C:\Games\Keen4" in Keen4 itself.
case10() {
    newvm "$W/10/vm"
    local K="$W/10/Keen4"
    mkdir -p "$K/DATA"
    mz "$K/KEEN4E.EXE"
    mz "$K/KEENEDIT.EXE" 90000
    echo lvl > "$K/DATA/LEVEL.DAT"
    printf '[autoexec]\nmount c "C:\\Games\\Keen4"\nc:\nKEEN4E.EXE\n' > "$K/dosbox.conf"
    imp "$W/10/a" "$K" "$HD" || { fail "the import failed: $(why "$W/10/a")"; return; }
    [ "$(js "$W/10/a" 'r["kind"]')" = dosbox ] || fail "the config should be used: $(why "$W/10/a")"
    grep -q 'mounts no folder' "$W/10/a.err" && fail "the config was dropped: $(why "$W/10/a")"
    local out
    out=$(batcheck "$(js "$W/10/a" 'r["batch"].split("\\")[-1]')") || fail "the batch file doesn't run: $out"
    echo "$out" | grep -q '^RAN KEEN4E.EXE IN ' || fail "the batch file should run KEEN4E.EXE: $out"
}

# 11. A Windows setup_*.exe that isn't GOG's installer.
case11() {
    newvm "$W/11/vm"
    mkdir -p "$W/11/MOO2/WIN95"
    mz "$W/11/MOO2/ORION2.EXE" 3000
    pe "$W/11/MOO2/WIN95/setup_win95.exe"
    imp "$W/11/a" "$W/11/MOO2" "$HD" || { fail "the folder's import failed: $(why "$W/11/a")"; return; }
    [ "$(js "$W/11/a" 'r["kind"]')" = plain ] || fail "the folder should import as a plain game: $(why "$W/11/a")"
    grep -qi 'innoextract\|GOG.s installer' "$W/11/a.err" && fail "the folder was taken for GOG's installer: $(why "$W/11/a")"
    [ "$(js "$W/11/a" 'r["runs"][-1]')" = ORION2.EXE ] || fail "ORION2.EXE should run: $(js "$W/11/a" 'r["runs"]')"
    (cd "$W/11" && python3 -m zipfile -c "$W/11/MOO2.zip" MOO2)
    newvm "$W/11/vm2"
    imp "$W/11/b" "$W/11/MOO2.zip" "$HD" || { fail "the zip's import failed: $(why "$W/11/b")"; return; }
    [ "$(js "$W/11/b" 'r["kind"]')" = plain ] || fail "the zip should import as a plain game: $(why "$W/11/b")"
    grep -qi 'innoextract\|GOG.s installer' "$W/11/b.err" && fail "the zip was taken for GOG's installer: $(why "$W/11/b")"
    [ "$(js "$W/11/b" 'r["runs"][-1]')" = ORION2.EXE ] || fail "zip: ORION2.EXE should run: $(js "$W/11/b" 'r["runs"]')"
    command -v innoextract > /dev/null || echo "(no innoextract: case 11 can't fail on it)"
}

# 12. A config per game, no play task.
case12() {
    newvm "$W/12/vm"
    local G="$W/12/noarch"
    mkdir -p "$G/data/KEEN4" "$G/data/KEEN5"
    echo "Commander Keen Complete Pack" > "$G/gameinfo"
    mz "$G/data/KEEN4/KEEN4E.EXE"
    mz "$G/data/KEEN5/KEEN5E.EXE"
    conf "$G/dosbox_keen4.conf" 'mount C "data"' 'c:' 'cd KEEN4' 'KEEN4E.EXE' 'exit'
    conf "$G/dosbox_keen5.conf" 'mount C "data"' 'c:' 'cd KEEN5' 'KEEN5E.EXE' 'exit'
    imp "$W/12/a" "$G" "$HD" || { fail "the import failed: $(why "$W/12/a")"; return; }
    local runs
    runs=$(js "$W/12/a" 'r["runs"]')
    case "$runs" in *KEEN4E.EXE*) ;; *) fail "the batch file should run one game (KEEN4E.EXE): $runs" ;; esac
    case "$runs" in *KEEN5*) fail "the batch file runs both configs' commands: $runs" ;; esac
    grep -q 'dosbox_keen5\.conf' "$W/12/a.err" || fail "the other config (dosbox_keen5.conf) should be reported: $(why "$W/12/a")"
    ls_all | grep -q 'KEEN5E' && fail "KEEN5 was copied as part of KEEN4's import"
    local out
    out=$(batcheck "$(js "$W/12/a" 'r["batch"].split("\\")[-1]')") || fail "the batch file doesn't run: $out"
}

# 13. The game's own INTRO.EXE.
case13() {
    newvm "$W/13/vm"
    local G="$W/13/Intro Game"
    mkdir -p "$G/GAME"
    mz "$G/GAME/INTRO.EXE"
    mz "$G/GAME/MAIN.EXE"
    gog "$G" "Intro Game" dosbox_ig.conf
    conf "$G/dosbox_ig.conf" 'mount C ".."' 'c:' 'cd GAME' 'intro' 'main'
    imp "$W/13/a" "$G" "$HD" || { fail "the import failed: $(why "$W/13/a")"; return; }
    local out
    out=$(batcheck "$(js "$W/13/a" 'r["batch"].split("\\")[-1]')") || fail "the batch file doesn't run: $out"
    echo "$out" | grep -qx 'RAN intro IN C:\\GAME' || fail "the batch file should keep intro (INTRO.EXE in C:\\GAME): $out"
    echo "$out" | grep -A1 '^RAN intro' | grep -qx 'RAN main IN C:\\GAME' || fail "intro should come before main: $out"
}

# 14. DOSBox's memory and speed: said only when they need something done.
case14() {
    newvm "$W/14/vm"
    printf '[Machine]\nmem_size = 32768\n' >> "$VM/86box.cfg"
    local G="$W/14/Slow"
    mkdir -p "$G"
    mz "$G/SLOW.EXE"
    printf '[dosbox]\nmemsize=64\n[cpu]\ncycles=fixed 3000\n[autoexec]\nmount c "C:\\Games\\Slow"\nc:\nSLOW.EXE\n' > "$G/dosbox.conf"
    imp "$W/14/a" "$G" "$HD" || { fail "the import failed: $(why "$W/14/a")"; return; }
    grep -qF "DOSBox gives this game 64 MB of memory, and the VM has 32 MB" "$W/14/a.err" ||
        fail "64 MB in DOSBox against the VM's 32 MB should be pointed out: $(why "$W/14/a")"
    grep -qF "fixed speed (cycles=3000)" "$W/14/a.err" || fail "a fixed DOSBox speed should be pointed out: $(why "$W/14/a")"
    printf '[dosbox]\nmemsize=16\n[cpu]\ncycles=max\n[autoexec]\nmount c "C:\\Games\\Slow"\nc:\nSLOW.EXE\n' > "$G/dosbox.conf"
    imp "$W/14/b" "$G" "$HD" || { fail "the import failed: $(why "$W/14/b")"; return; }
    grep -qi "memory\|cycles" "$W/14/b.err" && fail "16 MB and cycles=max need nothing done: $(why "$W/14/b")"
    true
}

# ---- run them -------------------------------------------------------------------

for n in 1 2 3 4 5 6 7 8 9 10 11 12 13 14; do
    N=$n
    before=$(grep -c "^FAIL: case $n:" "$W/fails")
    (
        set -eE
        trap 'echo "FAIL: case $N: a command failed (line $LINENO): $BASH_COMMAND" | tee -a "$W/fails"' ERR
        "case$n"
    ) > "$W/case$n.log" 2>&1
    cat "$W/case$n.log"
    if [ "$(grep -c "^FAIL: case $n:" "$W/fails")" = "$before" ]; then
        echo "case $n: ok"
    fi
done

if [ -s "$W/fails" ]; then
    echo
    sort -s -k3,3n "$W/fails" | awk '!seen[$0]++'
    exit 1
fi
echo "PASS"
