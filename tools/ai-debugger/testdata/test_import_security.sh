#!/bin/bash
# Security tests for tools/ai-debugger/gog_import.py: what an untrusted
# download or folder can make the importer read from the host, hang on or
# crash on, and whether it refuses a VM that 86Box has open. Each case runs
# in its own subshell with set -e; a failed case is reported and the next
# one runs. Prints "FAIL: case N: why" for each failure, "PASS" when none.
#   1. A zip's dosbox.conf mounts C: and imgmounts D: by an absolute host
#      path, another by a long ../ chain, a third mounts C: inside the zip
#      but imgmounts D: (and E: as a CD folder) outside: no host secret
#      reaches C:, the VM's cd/ or 86box.cfg; the import falls back.
#   2. A zip with a game, an ISO and a .cue whose second FILE is a ../ chain
#      (and an absolute path) to a host secret, or ../(x20)dev/zero, and a
#      cdrdao .toc whose second DATAFILE is the ../ chain: nothing linked or
#      copied into cd/, the cue (or toc) in cd/ names nothing outside cd/
#      (86Box opens a relative FILE from the sheet's folder, an absolute one
#      as it is), and the import ends (timeout 60).
#   3. A zip's dosbox.conf mounts a host folder as a CD (mount d /abs -t
#      cdrom; mount e ../.. -t cdrom): not copied into cd/.
#   4. A folder with FIFOs named cdimage.iso, music.cue, goggame-1.info and
#      dosbox.conf next to a game: the import ends (timeout 60) and the game
#      is imported.
#   5. A 12-byte LEVEL.BIN starting with the CD sync pattern in a game's
#      folder; a loose Track02.aif with COMM exponent 0x7FFF next to an ISO:
#      no traceback.
#   6. --json with an early error (86Box running in the VM's folder, a
#      missing source, --offset 512 on a partitioned disk): stdout is JSON
#      with an "error" key.
#   7. Stand-in 86Box processes started from /: with a positional
#      <vm>/86box.cfg, with -Z <vm>, with lower-case -p <vm>; a process
#      of another name holding the disk image open; and one named 86Box
#      whose comm is qt_thread, as the Qt 86Box's: the import refuses each
#      time, names 86Box for the last, and changes nothing. The last needs a
#      C compiler (cc, from build-essential); it is skipped without one.
#   8. outer.zip with README.TXT and an inner zip named GAME (no extension):
#      no traceback, the game is imported.

HERE=$(cd "$(dirname "$0")" && pwd)
IMPORT=${IMPORT:-$HERE/../gog_import.py}  # IMPORT=... tests another copy
W=$(mktemp -d)
trap '[ -n "$KEEP" ] || rm -rf "$W"' EXIT
export TMPDIR="$W/tmp"  # the importer's own temporary files stay in here too
mkdir -p "$TMPDIR"
FAILS="$W/fails"
: > "$FAILS"
fail() { echo "FAIL: case $CASE: $*" | tee -a "$FAILS"; }
TO=120

# mz FILE [SIZE]: a fake DOS program.
mz() { python3 -c '
import sys
size = int(sys.argv[2]) if len(sys.argv) > 2 else 600
b = bytearray(size); b[0:2] = b"MZ"; b[0x18] = 0x1C
open(sys.argv[1], "wb").write(b)' "$@"; }

# imp ARGS...: the importer, never longer than $TO seconds (exit 124 then),
# and never writing a file over 100 MB (a device copied must not fill the
# disk).
imp() { (ulimit -f 102400 && exec timeout -k 5 "$TO" python3 -I "$IMPORT" "$@"); }

# mkvm DIR: a VM, a 20 MB disk with one FAT16 partition at sector 63 and a
# CD drive; C is its FAT volume for mtools.
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
}

# listing: every file and folder on C:, recursively.
listing() { mdir -/ -b -i "$C" ::/ 2> /dev/null || true; }

# mkzip ZIP DIR: DIR's contents in a zip.
mkzip() { (cd "$2" && python3 -m zipfile -c "$1" .); }

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

# check.py cues VM: every FILE a cue sheet under VM/cd names (as 86Box
# opens it: relative to the sheet's folder, or absolute) is inside VM/cd.
# check.py cfg VM: the CD image 86box.cfg names is inside VM.
# check.py json FILE: FILE is one JSON object with a non-empty "error".
cat > "$W/check.py" << 'EOF'
import json, os, re, sys

what, arg = sys.argv[1], sys.argv[2]
bad = []
if what == "cues":
    cd = os.path.realpath(os.path.join(arg, "cd"))
    for dp, dns, fs in os.walk(cd):
        for f in fs:
            if not f.lower().endswith((".cue", ".toc")):
                continue
            text = open(os.path.join(dp, f), encoding="latin-1").read()
            for m in re.finditer(r'^\s*(?:DATA|AUDIO)?FILE\s+(?:"([^"]+)"|(\S+))', text, re.M | re.I):
                name = (m.group(1) or m.group(2)).replace("\\", "/")
                t = os.path.realpath(os.path.join(dp, name))
                if os.path.commonpath([t, cd]) != cd:
                    bad.append("%s names %s (%s)" % (os.path.relpath(os.path.join(dp, f), arg), name, t))
elif what == "cfg":
    vm = os.path.realpath(arg)
    for line in open(os.path.join(arg, "86box.cfg"), encoding="utf-8", errors="replace"):
        m = re.match(r"\s*cdrom_\d+_image_(?:path|history_\d+)\s*=\s*(.*)", line)
        if m and m.group(1).strip():
            t = os.path.realpath(os.path.join(arg, m.group(1).strip()))
            if os.path.commonpath([t, vm]) != vm:
                bad.append(line.strip())
elif what == "json":
    try:
        r = json.load(open(arg))
        if not isinstance(r, dict) or not isinstance(r.get("error"), str) or not r["error"]:
            bad.append("no error in %r" % (r,))
    except ValueError as e:
        bad.append("not JSON (%s): %r" % (e, open(arg).read()[:300]))
if bad:
    print("; ".join(bad))
    sys.exit(1)
EOF
chk() { python3 -I "$W/check.py" "$@"; }

# The host's secrets, outside every source: files, a folder, a CD image.
S="$W/host/hostsecret"
MARK=TOPSECRET-7f3c
mkdir -p "$S/KEYS"
echo "$MARK id_rsa" > "$S/KEYS/ID_RSA"
echo "$MARK notes" > "$S/NOTES.TXT"
echo "$MARK key" > "$S/KEY.TXT"
python3 "$W/mkiso.py" "$S/HOSTCD.ISO" HOSTCD "SECRET.TXT=$S/NOTES.TXT"
UP=$(printf '../%.0s' $(seq 30))  # far enough up to reach / from anywhere
REL="$UP${S#/}"                   # the secret folder by a ../ chain

# no_leak TAG: nothing of the host's secrets in the VM's folder: not on C: (its
# names, or their contents anywhere on the disk), not linked or copied into
# cd/, not named by 86box.cfg or by a cue sheet in cd/.
no_leak() {
    local tag=$1 hits f
    hits=$(grep -a -r -l "$MARK" "$VM" 2> /dev/null || true)
    [ -z "$hits" ] || fail "$tag: a host secret's contents reached the VM: $hits"
    for f in "$S/KEYS/ID_RSA" "$S/NOTES.TXT" "$S/KEY.TXT" "$S/HOSTCD.ISO"; do
        hits=$(find "$VM" -samefile "$f" 2> /dev/null || true)
        [ -z "$hits" ] || fail "$tag: $f is linked into the VM: $hits"
    done
    hits=$(listing | grep -i 'ID_RSA\|NOTES\.TXT\|KEY\.TXT\|HOSTCD\|/KEYS' || true)
    [ -z "$hits" ] || fail "$tag: host files on C:: $hits"
    hits=$(grep -i 'hostsecret\|hostcd\|id_rsa\|key\.txt\|zero' "$VM/86box.cfg" || true)
    [ -z "$hits" ] || fail "$tag: 86box.cfg names a host file: $hits"
    hits=$(chk cfg "$VM") || fail "$tag: 86box.cfg names a CD outside the VM: $hits"
    hits=$(chk cues "$VM") || fail "$tag: a cue sheet in the VM's cd/ names a file outside it: $hits"
}

no_traceback() {
    if grep -q "Traceback" "$1"; then fail "traceback: $(tail -3 "$1" | tr '\n' ' ')"; fi
}

# ---------------------------------------------------------------------------
# 1. DOSBox config mounts outside the archive.
case1() {
    local d rc
    # a: absolute host paths; b: a ../ chain to the same folder.
    for v in a b; do
        mkvm "$W/1$v/vm"
        d="$W/1$v/src"
        mkdir -p "$d"
        mz "$d/EVIL.EXE"
        if [ $v = a ]; then
            printf '[autoexec]\nmount c "%s"\nimgmount d "%s/HOSTCD.ISO" -t iso\nc:\nEVIL.EXE\n' "$S" "$S" > "$d/dosbox.conf"
        else
            printf '[autoexec]\nmount c "%s"\nimgmount d "%s/HOSTCD.ISO" -t iso\nc:\nEVIL.EXE\n' "$REL" "$REL" > "$d/dosbox.conf"
        fi
        mkzip "$W/1$v/evil$v.zip" "$d"
        rc=0
        imp "$W/1$v/evil$v.zip" "$HD" > "$W/1$v.out" 2>&1 || rc=$?
        no_traceback "$W/1$v.out"
        [ $rc = 0 ] || fail "1$v: the import should fall back and finish (exit $rc): $(tail -3 "$W/1$v.out")"
        grep -q "mounts no folder here as C:" "$W/1$v.out" || fail "1$v: the C: mount outside the zip should be reported missing: $(cat "$W/1$v.out")"
        listing | grep -qi '/EVIL\.EXE$' || fail "1$v: the game in the zip should be imported: $(listing | tr '\n' ' ')"
        no_leak "1$v"
    done
    # c: C: inside the zip, D: an image and E: a CD folder outside it.
    mkvm "$W/1c/vm"
    d="$W/1c/src"
    mkdir -p "$d"
    mz "$d/EVIL.EXE"
    printf '[autoexec]\nmount c .\nimgmount d "%s/HOSTCD.ISO" -t iso\nmount e "%s" -t cdrom\nc:\nEVIL.EXE\n' "$S" "$REL" > "$d/dosbox.conf"
    mkzip "$W/1c/evilc.zip" "$d"
    rc=0
    imp "$W/1c/evilc.zip" "$HD" > "$W/1c.out" 2>&1 || rc=$?
    no_traceback "$W/1c.out"
    [ $rc = 0 ] || fail "1c: the import should finish (exit $rc): $(tail -3 "$W/1c.out")"
    listing | grep -qi '/EVIL\.EXE$' || fail "1c: the game should be imported: $(cat "$W/1c.out")"
    grep -q "HOSTCD.ISO.*\(left out\|isn't there\)" "$W/1c.out" || fail "1c: the CD outside the zip should be reported left out: $(cat "$W/1c.out")"
    no_leak 1c
}

# 2. Cue sheets naming host files.
case2() {
    local d rc v name
    for v in a b c d; do
        case $v in
            a | d) name="$REL/KEY.TXT" ;;
            b) name="$S/KEY.TXT" ;;
            c) name="$(printf '../%.0s' $(seq 20))dev/zero" ;;
        esac
        mkvm "$W/2$v/vm"
        d="$W/2$v/src"
        mkdir -p "$d/Evil"
        mz "$d/Evil/EVIL.EXE"
        mz "$W/2$v/inst.bin"
        python3 "$W/mkiso.py" "$d/evil.iso" EVILCD "INSTALL.EXE=$W/2$v/inst.bin"
        if [ $v = d ]; then
            printf 'CD_ROM\n\nTRACK MODE1\nDATAFILE "evil.iso"\n\nTRACK MODE1\nDATAFILE "%s"\n' "$name" > "$d/evil.toc"
        else
            printf 'FILE "evil.iso" BINARY\n  TRACK 01 MODE1/2048\n    INDEX 01 00:00:00\nFILE "%s" BINARY\n  TRACK 02 MODE1/2048\n    INDEX 01 00:00:00\n' "$name" > "$d/evil.cue"
        fi
        mkzip "$W/2$v/evil$v.zip" "$d"
        rc=0
        TO=60 imp "$W/2$v/evil$v.zip" "$HD" > "$W/2$v.out" 2>&1 || rc=$?
        [ $rc != 124 ] && [ $rc != 137 ] || fail "2$v: the import hung (killed after 60 s)"
        no_traceback "$W/2$v.out"
        [ $rc = 0 ] || fail "2$v: the import should finish (exit $rc): $(tail -3 "$W/2$v.out")"
        if [ -d "$VM/cd" ]; then
            hits=$(find "$VM/cd" -type f -size +1M)
            [ -z "$hits" ] || fail "2$v: big files in cd/ (a device copied?): $hits"
            hits=$(find "$VM/cd" ! -type f ! -type d)
            [ -z "$hits" ] || fail "2$v: special files in cd/: $hits"
            hits=$(find "$VM/cd" -iname 'key.txt' -o -name 'zero')
            [ -z "$hits" ] || fail "2$v: the cue's host file was put into cd/: $hits"
        fi
        listing | grep -qi '/EVIL\.EXE$' || fail "2$v: the game should be imported: $(cat "$W/2$v.out")"
        no_leak "2$v"
    done
}

# 3. A host folder mounted as a CD.
case3() {
    local d rc
    mkvm "$W/3/vm"
    d="$W/3/src"
    mkdir -p "$d/Evil"
    mz "$d/Evil/EVIL.EXE"
    printf '[autoexec]\nmount c .\nmount d "%s" -t cdrom -label EVILCD\nmount e "%s" -t cdrom\nc:\ncd evil\nEVIL.EXE\n' "$S" "$REL" > "$d/dosbox.conf"
    mkzip "$W/3/evil.zip" "$d"
    rc=0
    imp "$W/3/evil.zip" "$HD" > "$W/3.out" 2>&1 || rc=$?
    no_traceback "$W/3.out"
    [ $rc = 0 ] || fail "the import should finish (exit $rc): $(tail -3 "$W/3.out")"
    listing | grep -qi '/EVIL/EVIL\.EXE$' || fail "the game should be imported: $(cat "$W/3.out")"
    hits=$(find "$VM" -iname 'EVILCD' -o -iname 'hostsecret' -o -iname 'KEYS')
    [ -z "$hits" ] || fail "the host folder was copied into the VM as a CD: $hits"
    if grep -q "cdrom_01_image_path" "$VM/86box.cfg"; then fail "nothing should go into the CD drive: $(grep cdrom_01_image "$VM/86box.cfg")"; fi
    grep -q "Drive D:.*isn't there\|Drive D:.*left out" "$W/3.out" || fail "the D: mount outside the zip should be reported: $(cat "$W/3.out")"
    no_leak 3
}

# 4. FIFOs with scanned names in a folder source.
case4() {
    local d rc
    mkvm "$W/4/vm"
    d="$W/4/FifoGame"
    mkdir -p "$d"
    mz "$d/GAME.EXE"
    echo data > "$d/LEVEL.DAT"
    for n in cdimage.iso music.cue goggame-1.info dosbox.conf; do mkfifo "$d/$n"; done
    rc=0
    TO=60 imp "$d" "$HD" > "$W/4.out" 2>&1 || rc=$?
    [ $rc != 124 ] && [ $rc != 137 ] || fail "the import hung on a FIFO (killed after 60 s): $(tail -3 "$W/4.out")"
    no_traceback "$W/4.out"
    [ $rc = 0 ] || fail "the import should finish (exit $rc): $(tail -3 "$W/4.out")"
    listing | grep -qi '/FIFOGAME/GAME\.EXE$' || fail "the game should be imported: $(listing | tr '\n' ' ')"
    listing | grep -qi '/FIFOGAME/LEVEL\.DAT$' || fail "the game's data should be imported"
    hits=$(listing | grep -i 'CDIMAGE\|MUSIC\|GOGGAME\|DOSBOX' || true)
    [ -z "$hits" ] || fail "a FIFO reached C:: $hits"
    if grep -q "cdrom_01_image_path" "$VM/86box.cfg"; then fail "a FIFO went into the CD drive: $(grep cdrom_01_image "$VM/86box.cfg")"; fi
    for n in cdimage.iso music.cue goggame-1.info dosbox.conf; do
        [ -p "$d/$n" ] || fail "the user's FIFO $n was changed"
    done
}

# 5. Malformed CD data and music.
case5() {
    local d rc
    mkvm "$W/5a/vm"
    d="$W/5a/LevelGam"
    mkdir -p "$d"
    mz "$d/GAME.EXE"
    printf '\x00\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\x00' > "$d/LEVEL.BIN"
    [ "$(stat -c %s "$d/LEVEL.BIN")" = 12 ] || fail "setup: LEVEL.BIN should be 12 bytes"
    rc=0
    imp "$d" "$HD" > "$W/5a.out" 2>&1 || rc=$?
    no_traceback "$W/5a.out"
    [ $rc = 0 ] || fail "5a: a short LEVEL.BIN should not stop the import (exit $rc): $(tail -3 "$W/5a.out")"
    listing | grep -qi '/LEVELGAM/LEVEL\.BIN$' || fail "5a: LEVEL.BIN is the game's data and should be on C:: $(listing | tr '\n' ' ')"

    mkvm "$W/5b/vm"
    d="$W/5b/src"
    mkdir -p "$d"
    mz "$W/5b/inst.bin"
    python3 "$W/mkiso.py" "$d/game.iso" GAMECD "INSTALL.EXE=$W/5b/inst.bin"
    python3 -c '
import struct, sys
comm = struct.pack(">hIhHQ", 2, 44100, 16, 0x7FFF, 0x8000000000000000)
ssnd = struct.pack(">II", 0, 0) + bytes(4 * 100)
body = b"COMM" + struct.pack(">I", len(comm)) + comm + b"SSND" + struct.pack(">I", len(ssnd)) + ssnd
open(sys.argv[1], "wb").write(b"FORM" + struct.pack(">I", 4 + len(body)) + b"AIFF" + body)' "$d/Track02.aif"
    rc=0
    imp "$d/game.iso" "$HD" > "$W/5b.out" 2>&1 || rc=$?
    no_traceback "$W/5b.out"
    [ $rc = 0 ] || fail "5b: an AIFF with a huge exponent should not stop the import (exit $rc): $(tail -3 "$W/5b.out")"
    grep -q '^cdrom_01_image_path = cd/' "$VM/86box.cfg" || fail "5b: the ISO should still go into the drive: $(cat "$W/5b.out")"
}

# 6. --json and errors before the import starts.
case6() {
    local rc pid
    mkvm "$W/6/vm"
    mkdir -p "$W/6/KEEN"
    mz "$W/6/KEEN/KEEN.EXE"
    mkdir -p "$W/fake6"
    cp "$(command -v sleep)" "$W/fake6/86Box"
    (cd "$VM" && exec "$W/fake6/86Box" 60) &
    pid=$!
    trap 'kill $pid 2> /dev/null || true' EXIT
    for _ in $(seq 50); do [ "$(readlink "/proc/$pid/exe" 2> /dev/null)" = "$W/fake6/86Box" ] && break; sleep 0.1; done
    rc=0
    imp "$W/6/KEEN" "$HD" --json > "$W/6a.json" 2> "$W/6a.err" || rc=$?
    kill $pid 2> /dev/null || true
    wait $pid 2> /dev/null || true
    [ $rc != 0 ] || fail "6a: the import should refuse while 86Box runs in the VM's folder"
    out=$(chk json "$W/6a.json") || fail "6a: 86Box running: $out"
    grep -q "running" "$W/6a.json" || fail "6a: the JSON error should say 86Box is running: $(cat "$W/6a.json")"
    rc=0
    imp "$W/6/missing" "$HD" --json > "$W/6b.json" 2> "$W/6b.err" || rc=$?
    [ $rc != 0 ] || fail "6b: a missing source should fail"
    out=$(chk json "$W/6b.json") || fail "6b: missing source: $out"
    rc=0
    imp "$W/6/KEEN" "$HD" --offset 512 --json > "$W/6c.json" 2> "$W/6c.err" || rc=$?
    [ $rc != 0 ] || fail "6c: --offset 512 should fail"
    out=$(chk json "$W/6c.json") || fail "6c: --offset 512: $out"
    no_traceback "$W/6a.err"
    no_traceback "$W/6b.err"
    no_traceback "$W/6c.err"
}

# 7. 86Box started in other ways, from /.
case7() {
    local rc pid v sum cfgsum stub=$W/fake7/86Box
    mkvm "$W/7/vm"
    mkdir -p "$W/7/KEEN" "$W/fake7"
    mz "$W/7/KEEN/KEEN.EXE"
    # A stand-in that takes any arguments (sleep takes only numbers).
    if command -v cc > /dev/null; then
        printf '#include <unistd.h>\nint main(void) { sleep(120); return 0; }\n' > "$W/fake7/stub.c"
        cc -o "$stub" "$W/fake7/stub.c"
    fi
    sum=$(md5sum < "$HD")
    cfgsum=$(md5sum < "$VM/86box.cfg")
    for v in a b c d; do
        case $v in
            a) set -- "$VM/86box.cfg" ;;
            b) set -- -Z "$VM" ;;
            c) set -- -p "$VM" ;;
        esac
        if [ $v = d ]; then  # not named 86Box: holds the disk open
            (cd / && exec -a holder bash -c 'exec 3< "$1"; exec sleep 120' holder "$HD") &
        elif [ -x "$stub" ]; then
            (cd / && exec "$stub" "$@") &
        else
            (cd / && exec -a "$stub" python3 - "$@" <<< 'import time; time.sleep(120)') &
        fi
        pid=$!
        trap 'kill $pid 2> /dev/null || true' EXIT
        up=
        for _ in $(seq 50); do
            if [ $v = d ]; then
                ls -l "/proc/$pid/fd" 2> /dev/null | grep -qF "$HD" && up=1 && break
            else
                [ "$(tr '\0' '\n' < "/proc/$pid/cmdline" 2> /dev/null | head -1)" = "$stub" ] && up=1 && break
            fi
            sleep 0.1
        done
        [ -n "$up" ] || fail "7$v: setup: the stand-in did not start"
        rc=0
        out=$(imp "$W/7/KEEN" "$HD" 2>&1) || rc=$?
        echo "$out" > "$W/7$v.out"
        kill $pid 2> /dev/null || true
        wait $pid 2> /dev/null || true
        case $v in
            a) what="86Box $VM/86box.cfg" ;;
            b) what="86Box -Z $VM" ;;
            c) what="86Box -p $VM" ;;
            d) what="a process holding hd.img open" ;;
        esac
        [ $rc != 0 ] || fail "7$v: the import should refuse with $what running: $out"
        case "$out" in *"is running this VM"*) ;; *) fail "7$v: the refusal should say why ($what): $out" ;; esac
        [ "$(md5sum < "$HD")" = "$sum" ] || { fail "7$v: the disk was written with $what running"; sum=$(md5sum < "$HD"); }
        [ "$(md5sum < "$VM/86box.cfg")" = "$cfgsum" ] || { fail "7$v: 86box.cfg was changed"; cfgsum=$(md5sum < "$VM/86box.cfg"); }
    done
    # e: as the Qt 86Box, which renames its main thread (its comm) to
    # qt_thread: the refusal still names 86Box.
    if ! command -v cc > /dev/null; then
        echo "(no cc: case 7e skipped)"
        return
    fi
    mkdir -p "$W/fake7/qt"
    printf '#include <fcntl.h>\n#include <sys/prctl.h>\n#include <unistd.h>\nint main(int c, char **v) { int fd = open(v[1], O_RDONLY); prctl(PR_SET_NAME, "qt_thread"); sleep(120); return fd < 0 || c < 2; }\n' > "$W/fake7/qt/stub.c"
    cc -o "$W/fake7/qt/86Box" "$W/fake7/qt/stub.c"
    (cd / && exec "$W/fake7/qt/86Box" "$HD") &
    pid=$!
    trap 'kill $pid 2> /dev/null || true' EXIT
    up=
    for _ in $(seq 50); do
        [ "$(cat "/proc/$pid/comm" 2> /dev/null)" = qt_thread ] && ls -l "/proc/$pid/fd" 2> /dev/null | grep -qF "$HD" && up=1 && break
        sleep 0.1
    done
    [ -n "$up" ] || fail "7e: setup: the stand-in did not start"
    rc=0
    out=$(imp "$W/7/KEEN" "$HD" 2>&1) || rc=$?
    echo "$out" > "$W/7e.out"
    kill $pid 2> /dev/null || true
    wait $pid 2> /dev/null || true
    [ $rc != 0 ] || fail "7e: the import should refuse with the Qt 86Box running: $out"
    case "$out" in "86Box (process $pid) is running this VM"*) ;; *) fail "7e: the refusal should name 86Box: $out" ;; esac
}

# 8. An inner zip without an extension.
case8() {
    local rc d
    mkvm "$W/8/vm"
    d="$W/8/src"
    mkdir -p "$d"
    python3 -c '
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1], "w")
z.writestr("SPACEX.EXE", b"MZ" + bytes(598))
z.writestr("LEVEL.DAT", b"level")
z.close()' "$d/GAME"
    echo "unzip GAME" > "$d/README.TXT"
    mkzip "$W/8/outer.zip" "$d"
    rc=0
    imp "$W/8/outer.zip" "$HD" > "$W/8.out" 2>&1 || rc=$?
    no_traceback "$W/8.out"
    [ $rc = 0 ] || fail "the import should finish (exit $rc): $(tail -3 "$W/8.out")"
    listing | grep -qi '/SPACEX\.EXE$' || fail "the inner zip's game should be imported: $(cat "$W/8.out")"
    listing | grep -qi '/LEVEL\.DAT$' || fail "the inner zip's data should be imported"
    hits=$(listing | grep -i 'ARCHIVE' || true)
    [ -z "$hits" ] || fail "the inner archive itself reached C:: $hits"
}

for CASE in 1 2 3 4 5 6 7 8; do
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
