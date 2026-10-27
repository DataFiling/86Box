#!/usr/bin/env python3
"""Put a DOS game onto an 86Box disk image, to run and debug it there.

GOG sells DOS games wrapped in DOSBox: the installer (setup_*.exe, an Inno
Setup installer) unpacks the game's files next to a DOSBox copy, DOSBox
configuration files whose [autoexec] section mounts the game's folder as C:
(and sometimes a CD image as D:) and starts it, and goggame-*.info, which
says which configurations the "Play" button uses.

This reads those, copies what DOSBox would have mounted as C: onto a FAT
disk image (with mtools; the emulator must not be running on it), writes a
batch file that runs the game's own commands, puts the game's CD (an image
or a folder DOSBox mounted) into the VM's CD-ROM drive when the image is a
VM's disk (86box.cfg next to it), and reports what else the VM needs (sound
card settings other than the game VM's).

A game without a DOSBox configuration (a zip or 7z archive, or a folder, of
a DOS game) is copied whole, and the batch file runs the program that looks
like the game (--run names another); CD images in it go into the CD-ROM
drive, and floppy images are unpacked into DISK1, DISK2... next to it. A CD
image on its own goes into the drive, and the importer says what to run
from it (the game's installer, usually).

  gog_import.py setup_game.exe IMAGE [--offset BYTES] [--dir NAME] [--run CMD] [--json]
  gog_import.py gog_game.sh IMAGE ...            (GOG's Linux installer)
  gog_import.py "C:/GOG Games/Game" IMAGE ...    (an installed game, or any game's folder)
  gog_import.py game.zip IMAGE ...               (a zip or 7z archive; also rar, lzh, arj with 7-Zip)
  gog_import.py dosbox.conf IMAGE ...            (a DOSBox configuration that mounts the game)
  gog_import.py game.iso IMAGE                   (a CD: .iso, .cue, .ccd, .mds, .chd, raw .bin)

--offset: the FAT partition's byte offset in the image; by default the first
FAT partition in a hard disk image's partition table, or 0 for a floppy or
an image without one. --dir: the directory on the image (default: the folder
the game runs in, under C:\\). --run: the command the batch file runs in
the game's folder. --json: the report as JSON on stdout (the text goes to
stderr), for scripts and AI agents. Needs mtools, innoextract for Windows
installers, and 7-Zip (7zz or 7z) or bsdtar for 7z archives.
"""

import argparse
import glob
import json
import math
import os
import re
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import zipfile
import zlib

# GOG's own files around the game, never part of it.
SKIP_DIRS = {"dosbox", "__support", "__redist", "commonappdata", "tmp", "app", "cloud_saves"}
SKIP_FILES = re.compile(r"^(goggame-.*|gog\.ico|gfw_high.*|support\.ico|unins\d+\..*|webcache\.zip|"
                        r"dosbox.*\.conf|launch.*\.(bat|lnk)|eula.*|.*\.lnk)$", re.I)
# Left by the archivers of other systems.
JUNK_DIRS = {"__MACOSX"}
JUNK_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}
# What download sites put next to a game's folder: their notes, scans and
# manuals, never the game.
EXTRAS = re.compile(r"\.(nfo|diz|url|lnk|website|txt|pdf|jpe?g|png|gif|bmp|webp|html?|md|rtf|docx?|sfv|md5|"
                    r"sha1|m3u|ico)$", re.I)

# The report: printed as text, or with --json as one object for scripts and
# agents (the text then goes to stderr).
REPORT = {"warnings": [], "skipped": []}
JSON_OUT = False
TEMP_DIRS = []  # removed at the end, also when stopped


def say(text):
    print(text, file=sys.stderr if JSON_OUT else sys.stdout)


def warn(text):
    REPORT["warnings"].append(text)
    say(text)


# Files named inside an untrusted source (a DOSBox config's mounts, a cue
# sheet's tracks) must stay inside it: BOUND is the folder given or
# unpacked (None: anywhere, for a config the user named).
BOUND = None


def inside(path, root):
    """path is root or under it (links resolved)."""
    path, root = os.path.realpath(path), os.path.realpath(root)
    return os.path.commonpath([path, root]) == root


def allowed(path):
    """A file or folder named by a source may be used: inside BOUND."""
    return BOUND is None or inside(path, BOUND)


def regular(path):
    """A regular file (a FIFO or device would block or never end when read)."""
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except OSError:
        return False


def read_head(path, size):
    """The first bytes of a regular file; b"" for anything else."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return b""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return b""
        with os.fdopen(fd, "rb") as f:
            fd = None
            return f.read(size)
    finally:
        if fd is not None:
            os.close(fd)


def read_sheet(path, size=1 << 20):
    """A cue sheet's or config's text: UTF-8 (after a byte order mark), else
    Windows' code page (as cue sheets made on Windows are)."""
    data = read_head(path, size)
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace")


# ---- the host -----------------------------------------------------------------

# mtools: CP437 short names, as DOS has them (mtools defaults to 850), and
# UTF-8 host names (in the C locale it turns other characters into "_").
MTOOLS_ENV = dict(os.environ, LC_ALL="C.UTF-8", DEFAULT_CODEPAGE="437")


def mtools(*args, check=True, fail=None):
    try:
        r = subprocess.run(list(args), capture_output=True, text=True, errors="replace", env=MTOOLS_ENV,
                           stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        r = subprocess.CompletedProcess(args, 127, "", "%s isn't installed (it comes with mtools)" % args[0])
    if check and r.returncode:
        detail = (r.stderr or r.stdout).strip() or "exit code %d" % r.returncode
        sys.exit((fail or "%s failed: %%s" % args[0]) % detail)
    return r


# 86Box's options that take a value (src/86box.c), any case.
VALUE_OPTIONS = {"-a", "--assetpath", "-c", "--config", "-e", "--vmmpath", "-g", "--lang", "-h", "--hwnd", "-i",
                 "--image", "-j", "--instrument", "-l", "--logfile", "-o", "--global", "-p", "--vmpath", "-r",
                 "--rompath", "-v", "--vmname"}


def proc_name(pid):
    """A process's program name: its executable's (the Qt 86Box renames
    its main thread, and so its comm, to qt_thread), else its comm."""
    try:
        return os.path.basename(os.readlink("/proc/%s/exe" % pid)).replace(" (deleted)", "")
    except OSError:
        try:
            with open("/proc/%s/comm" % pid) as f:
                return f.read().strip()
        except OSError:
            return ""


def vm_running(vmdir, image):
    """(name, process ID) of a process that has the VM's disk image open,
    or of an 86Box whose command line names the VM's folder (on Linux);
    None. A running 86Box holds the disk and rewrites 86box.cfg from memory."""
    want, disk = os.path.realpath(vmdir), os.path.realpath(image)
    if not os.path.isdir("/proc"):
        return None
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            fds = os.listdir("/proc/%s/fd" % pid)
        except OSError:
            continue
        for fd in fds:
            try:
                if os.path.realpath(os.readlink("/proc/%s/fd/%s" % (pid, fd))) == disk:
                    return proc_name(pid) or "a process", int(pid)
            except OSError:
                pass
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                args = [os.fsdecode(x) for x in f.read().split(b"\0") if x]
            cwd = os.readlink("/proc/%s/cwd" % pid)
        except OSError:
            continue
        if not args or "86box" not in os.path.basename(args[0]).lower():
            continue
        # As 86Box reads it: -P/--vmpath DIR, -C/--config FILE (relative
        # to the VM's folder), a trailing config file, or with -Z a
        # trailing VM folder; else the current folder.
        vm, cfg, last, k = cwd, None, False, 1
        while k < len(args):
            o = args[k].lower()
            if o in VALUE_OPTIONS and k + 1 < len(args):
                if o in ("-p", "--vmpath"):
                    vm = os.path.join(cwd, args[k + 1])
                elif o in ("-c", "--config"):
                    cfg = args[k + 1]
                k += 2
                continue
            if o in ("-z", "--lastvmpath"):
                last = True
            elif not o.startswith("-"):
                if last:
                    vm = os.path.join(cwd, args[k])
                else:
                    cfg = args[k]
            k += 1
        if cfg:
            vm = os.path.dirname(os.path.join(vm, cfg))
        if os.path.realpath(vm) == want:
            return "86Box", int(pid)
    return None


def check_space(need, where, what):
    free = shutil.disk_usage(where).free
    if need > free - (64 << 20):
        sys.exit("%s needs %d MB, and %s has %d MB free." % (what, need >> 20, where, free >> 20))


def on_signal(signum, frame):
    raise SystemExit("Stopped by signal %d." % signum)  # so the unpacking folder is removed


# ---- archives -----------------------------------------------------------------

SEVENZIP_MAGIC = b"7z\xbc\xaf\x27\x1c"


def archive_kind(path):
    """"zip", "7z", "rar", "lha" or "arj" by content, else None. (A
    self-extracting zip is a zip; so is GOG's Linux installer.)"""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return None
    if head.startswith(SEVENZIP_MAGIC):
        return "7z"
    if head.startswith(b"Rar!\x1a\x07"):
        return "rar"
    if head.startswith(b"\x60\xea"):
        return "arj"
    if len(head) > 6 and head[2:4] == b"-l" and head[6:7] == b"-":  # "-lh5-"
        return "lha"
    if zipfile.is_zipfile(path):
        return "zip"
    return None


def seven_zip():
    """7-Zip: 7zz (the current one, where both are packaged) before p7zip's 7z."""
    return next((t for t in ("7zz", "7z", "7za") if shutil.which(t)), None)


def unpack_zip(path, dest):
    """Unpack a zip into dest by hand: backslashes are separators (zips made
    on Windows), absolute paths, drive letters and ".." are dropped, and links
    and other special entries are left out (zipfile would write a link as a
    file holding its target). Compression methods zipfile lacks (Deflate64,
    and PKZIP 1's Shrink, Reduce and Implode) are left to 7-Zip."""
    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            check_space(sum(i.file_size for i in infos), dest, "Unpacking %s" % os.path.basename(path))
            for info in infos:
                name = info.filename.replace("\\", "/")
                parts = [p for p in name.split("/") if p not in ("", ".", "..")]
                if parts and re.fullmatch(r"[A-Za-z]:", parts[0]):
                    parts = parts[1:]
                if not parts:
                    continue
                mode = info.external_attr >> 16
                target = os.path.join(dest, *parts)
                if name.endswith("/") or stat.S_ISDIR(mode):
                    os.makedirs(target, exist_ok=True)
                    continue
                if stat.S_IFMT(mode) and not stat.S_ISREG(mode):
                    REPORT["skipped"].append("%s (a link or special file)" % name)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                if os.path.isdir(target):
                    REPORT["skipped"].append("%s (a folder has that name)" % name)
                    continue
                with z.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
    except NotImplementedError:  # a compression method zipfile lacks
        shutil.rmtree(dest)
        os.makedirs(dest)
        unpack_7z(path, dest, "zip")
    except RuntimeError as e:  # "File ... is encrypted, password required"
        sys.exit("%s: %s. Unpack it yourself and give the folder." % (path, e))
    except (zipfile.BadZipFile, zlib.error, EOFError, OSError) as e:
        sys.exit("%s could not be unpacked: %s" % (path, e))


def unpack_7z(archive, dest, kind="7z"):
    """Unpack a 7z (or a zip zipfile can't, a rar, lzh or arj) into dest with
    7-Zip, or bsdtar. 7-Zip's listing is checked first: absolute paths, "..",
    links, special files and encryption stop it. (sanitize_tree checks the
    result as well, which covers bsdtar.)"""
    tool = seven_zip()
    if tool is None and (kind not in ("7z", "rar") or not shutil.which("bsdtar")):
        sys.exit("7-Zip is needed to unpack %s (sudo apt install 7zip, or p7zip-full; or bsdtar, from "
                 "libarchive-tools, for 7z and rar), or unpack it yourself and give the folder." % archive)
    if tool is None:  # --passphrase: bsdtar would ask on the terminal for an encrypted entry
        r = subprocess.run(["bsdtar", "-x", "--passphrase", "x", "-f", archive, "-C", dest],
                           stdin=subprocess.DEVNULL, capture_output=True)
    else:
        r = subprocess.run([tool, "l", "-slt", "-p", archive], stdin=subprocess.DEVNULL, capture_output=True)
        if r.returncode == 0:
            listing = r.stdout.decode("utf-8", "surrogateescape")
            size = 0
            for e in listing.split("\n----------\n", 1)[-1].split("\n\n"):
                fields = dict(l.split(" = ", 1) for l in e.splitlines() if " = " in l)
                path = fields.get("Path", "")
                if fields.get("Encrypted") == "+":
                    sys.exit("%s is encrypted: unpack it yourself and give the folder." % archive)
                if path.startswith(("/", "\\")) or re.match(r"[A-Za-z]:", path) or \
                        ".." in re.split(r"[\\/]", path) or re.search(r"(^|\s)[lpcbs][r-][w-]", fields.get("Attributes", "")):
                    sys.exit("%s holds %s, which would land outside the folder it unpacks to, or is a link or "
                             "special file: not unpacking it." % (archive, path))
                size += int(fields.get("Size", "0") or 0) if fields.get("Folder") != "+" else 0
            check_space(size, dest, "Unpacking %s" % os.path.basename(archive))
        # -p: an empty password, so an encrypted entry fails instead of asking.
        r = subprocess.run([tool, "x", "-y", "-p", "-bd", "-o" + dest, archive], stdin=subprocess.DEVNULL,
                           capture_output=True)
    msg = (r.stderr or r.stdout).decode("utf-8", "replace").strip()
    if tool and kind == "rar" and r.returncode > 1 and shutil.which("bsdtar") and \
            re.search(r"unsupported method|can not open|cannot open", msg, re.I):
        # Debian's and Ubuntu's 7-Zip have no RAR decoder (it is in 7zip-rar); libarchive has one.
        shutil.rmtree(dest)
        os.makedirs(dest)
        tool = None
        r = subprocess.run(["bsdtar", "-x", "--passphrase", "x", "-f", archive, "-C", dest],
                           stdin=subprocess.DEVNULL, capture_output=True)
        msg = (r.stderr or r.stdout).decode("utf-8", "replace").strip()
    if r.returncode > (1 if tool else 0):  # 7-Zip's 1 is a warning
        if re.search(r"password|encrypt|passphrase", msg, re.I):
            sys.exit("%s is encrypted: unpack it yourself and give the folder." % archive)
        sys.exit("%s could not unpack %s:\n%s" % (tool or "bsdtar", archive, msg[-2000:]))
    if kind == "zip":
        fix_cp437_names(dest)


def fix_cp437_names(top):
    """7-Zip writes the names of DOS zips (CP437, without the UTF-8 flag) as
    their raw bytes: give them their characters."""
    for dirpath, dirnames, filenames in os.walk(top, topdown=False):
        for n in dirnames + filenames:
            if any("\udc80" <= ch <= "\udcff" for ch in n):
                os.rename(os.path.join(dirpath, n),
                          os.path.join(dirpath, os.fsencode(n).decode("cp437")))


def sanitize_tree(top):
    """Remove what isn't a plain file or folder from an unpacked archive: a
    link could point at the host's own files, and reading a FIFO hangs. Also
    makes everything readable (7-Zip applies the modes stored in it)."""
    for dirpath, dirnames, filenames in os.walk(top):
        for n in dirnames + filenames:
            p = os.path.join(dirpath, n)
            st = os.lstat(p)
            if stat.S_ISDIR(st.st_mode):
                os.chmod(p, st.st_mode | 0o700)
            elif stat.S_ISREG(st.st_mode):
                os.chmod(p, st.st_mode | 0o600)
            else:
                os.unlink(p)
                REPORT["skipped"].append("%s (a link or special file)" % os.path.relpath(p, top))
        dirnames[:] = [d for d in dirnames if os.path.isdir(os.path.join(dirpath, d))]


def unpack(installer, dest):
    """Unpack a GOG installer or an archive into dest: a zip (GOG's Linux
    installers are a shell script with one appended) by hand, a 7z, rar, lzh
    or arj archive with 7-Zip, a Windows installer with innoextract. Then
    keep only plain files and folders."""
    kind = archive_kind(installer)
    if kind == "zip":
        unpack_zip(installer, dest)
    elif kind:
        unpack_7z(installer, dest, kind)
    else:
        with open(installer, "rb") as f:
            magic = f.read(2)
        if magic != b"MZ":
            sys.exit("%s is no GOG installer, archive or CD image this can read: unpack it yourself and give "
                     "the folder." % installer)
        if shutil.which("innoextract") is None:
            sys.exit("innoextract is needed to unpack Windows installers (apt install innoextract), "
                     "or give the installed game's folder instead.")
        r = subprocess.run(["innoextract", "--gog", "--silent", "--output-dir", dest, installer],
                           capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL)
        if r.returncode:
            v = subprocess.run(["innoextract", "--version"], capture_output=True, text=True).stdout.splitlines()
            sys.exit("innoextract could not unpack %s:\n%s\n\n"
                     "Give it GOG's Windows offline installer (setup_*.exe, with its .bin parts next to it) or "
                     "Linux installer (*.sh), or the folder of the installed game. This innoextract (%s) %s. A "
                     "newer installer needs innoextract's development version, built from its source "
                     "(github.com/dscharrer/innoextract; no release reads it yet); or install the game with Wine "
                     "and give its folder (SETUP-LINUX.md, troubleshooting)." % (
                         installer, (r.stderr or r.stdout).strip(), v[0] if v else "?",
                         v[1][0].lower() + v[1][1:] if len(v) > 1 else "may be too old"))
    sanitize_tree(dest)


def gog_installer(top):
    """A GOG installer in an unpacked archive (at its top or one folder
    down): setup_*.exe (Windows) or a .sh with a zip in it (Linux)."""
    found = []
    for d in [top] + [os.path.join(top, n) for n in sorted(os.listdir(top)) if os.path.isdir(os.path.join(top, n))]:
        found += files_like(d, r"setup_.*\.exe|.*\.sh")
    for p in found:
        n = os.path.basename(p).lower()
        if n.endswith(".sh") and zipfile.is_zipfile(p):
            return p
        if n.endswith(".exe") and windows_program(p) and shutil.which("innoextract") and subprocess.run(
                ["innoextract", "--list", "--silent", p], capture_output=True, stdin=subprocess.DEVNULL).returncode == 0:
            return p  # an Inno Setup installer, not a game's own Windows setup
    return None


def unpack_inner(top, label):
    """An archive (label) holding only archives (and notes): unpack them,
    each into a folder named after it, once. True if it did."""
    d = top
    while True:  # through lone folders
        names = [n for n in os.listdir(d) if n not in JUNK_DIRS | JUNK_FILES]
        dirs = [n for n in names if os.path.isdir(os.path.join(d, n))]
        if len(dirs) == 1 and len(names) == 1:
            d = os.path.join(d, dirs[0])
            continue
        break
    files = [n for n in names if os.path.isfile(os.path.join(d, n)) and not EXTRAS.search(n)]
    inner = [n for n in files if archive_kind(os.path.join(d, n)) and not PROGRAM.search(n)]
    if dirs or not inner or len(inner) != len(files):
        return False
    for n in inner:
        p = os.path.join(d, n)
        aside = p + ".archive"  # out of the way: "GAME" unpacks into GAME/
        os.rename(p, aside)
        out = os.path.join(d, os.path.splitext(n)[0])
        os.makedirs(out, exist_ok=True)
        say("%s holds %s: unpacking that too." % (label, n))
        unpack(aside, out)
        os.unlink(aside)
    return True


# ---- disk images ----------------------------------------------------------------

FAT_TYPES = {0x01, 0x04, 0x06, 0x0B, 0x0C, 0x0E}


def partition_offset(image):
    """The byte offset of the first FAT partition in a hard disk image's
    partition table; 0 for a floppy image or a FAT volume without one."""
    with open(image, "rb") as f:
        sector = f.read(512)
    size = os.path.getsize(image)
    if size <= 2949120 or len(sector) < 512 or sector[510:] != b"\x55\xaa" or \
            b"FAT" in (sector[0x36:0x39], sector[0x52:0x55]):  # a floppy, or a FAT boot sector
        return 0
    for i in range(4):
        entry = sector[446 + 16 * i:462 + 16 * i]
        start, count = struct.unpack("<II", entry[8:])
        if entry[0] in (0, 0x80) and entry[4] in FAT_TYPES and start and (start + count) * 512 <= size:
            return start * 512
    return 0


def image_space(target):
    """(free bytes, cluster bytes) of the FAT volume at target."""
    free = re.findall(r"([\d ]+) bytes free", mtools("mdir", "-i", target, "::/", check=False).stdout)
    info = mtools("minfo", "-i", target, check=False).stdout
    sector = re.search(r"sector size: (\d+)", info)
    cluster = re.search(r"cluster size: (\d+)", info)
    return (int(free[-1].replace(" ", "")) if free else None,
            int(sector.group(1)) * int(cluster.group(1)) if sector and cluster else 4096)


# ---- DOSBox configurations ------------------------------------------------------

def find_ci(root, rel):
    """A path under root, matching each component case-insensitively (DOSBox
    configs use Windows paths and case); None if it doesn't exist."""
    path = root
    for part in [p for p in re.split(r"[\\/]+", rel) if p not in ("", ".")]:
        if part == "..":
            path = os.path.dirname(path)
            continue
        try:
            names = os.listdir(path)
        except OSError:
            return None
        match = next((n for n in names if n.lower() == part.lower()), None)
        if match is None:
            return None
        path = os.path.join(path, match)
    return path


def find_tail(top, rel):
    """A path under top ending in the last names of rel, an absolute Windows
    path from the PC a config was written on (C:\\GAMES\\KEEN4)."""
    parts = [p.lower() for p in re.split(r"[\\/]+", rel) if p and not re.fullmatch(r"[A-Za-z]:", p)]
    for n in (2, 1):
        tail = parts[-n:]
        if len(tail) < n:
            continue
        if [x.lower() for x in os.path.realpath(top).split(os.sep)][-n:] == tail:
            return top  # the folder given is the one it names
        for dirpath, dirnames, filenames in os.walk(top):
            for name in dirnames + filenames:
                p = os.path.join(dirpath, name)
                if [x.lower() for x in os.path.relpath(p, top).split(os.sep)][-n:] == tail:
                    return p
    return None


def is_dosbox_conf(path):
    """A DOSBox configuration whatever its name (Steam's, other packagers'):
    a small .conf or .cfg whose [autoexec] mounts a drive."""
    if not re.search(r"\.(conf|cfg)$", path, re.I) or not regular(path) or os.path.getsize(path) > 262144:
        return False
    text = read_text(path, 262144)
    return bool(re.search(r"^\s*\[autoexec\]", text, re.M | re.I) and
                re.search(r"^\s*@?\s*(img)?mount\s", text, re.M | re.I))


def find_game_root(top):
    """The folder under top holding goggame-*.info or the DOSBox configs,
    the shallowest first: top itself (an installed game, a current Windows
    installer), app/ (older Windows installers) or data/noarch/ (Linux
    installers)."""
    level = [top]
    for _ in range(4):
        for d in level:
            names = [n for n in os.listdir(d) if regular(os.path.join(d, n))]
            if any(re.match(r"(goggame-.*\.info|dosbox.*\.conf)$", n, re.I) for n in names) or \
                    any(is_dosbox_conf(os.path.join(d, n)) for n in names):
                return d
        level = [os.path.join(d, n) for d in level for n in sorted(os.listdir(d))
                 if os.path.isdir(os.path.join(d, n)) and n not in JUNK_DIRS]
    return top


def play_task(root):
    """(game name, [config paths], DOSBox's working directory) from
    goggame-*.info's primary play task. Paths in the task, and the mounts in
    the configs, are relative to that working directory."""
    for info in files_like(root, r"goggame-.*\.info"):
        try:
            data = json.loads(read_head(info, 1 << 20).decode("utf-8-sig"))
        except ValueError:
            continue
        tasks = data.get("playTasks", []) if isinstance(data, dict) else []
        task = next((t for t in tasks if t.get("isPrimary")), tasks[0] if tasks else None)
        if task is None:
            continue
        workdir = find_ci(root, task.get("workingDir", "")) or root
        if not allowed(workdir):
            workdir = root
        confs = []
        for rel in re.findall(r'-conf\s+"([^"]+)"|-conf\s+(\S+)', task.get("arguments", "")):
            p = find_ci(workdir, rel[0] or rel[1])
            if p and regular(p) and allowed(p):
                confs.append(p)
        return data.get("name", ""), confs, workdir
    return "", [], None


def files_like(folder, pattern):
    """The regular files in folder whose names match pattern (any case),
    sorted: glob would take "[GOG]" in a path for a pattern, and goes by case."""
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    return [os.path.join(folder, n) for n in names if re.fullmatch(pattern, n, re.I) and
            regular(os.path.join(folder, n))]


def read_conf(paths):
    """Settings ({section: {key: value}}) and [autoexec] lines of DOSBox
    configs, later ones overriding earlier ones, as DOSBox reads them."""
    settings, autoexec = {}, []
    for path in paths:
        section = None
        for raw in read_sheet(path).splitlines():
            line = raw.strip()
            m = re.fullmatch(r"\[(\w+)\]", line)
            if m:
                section = m.group(1).lower()
                continue
            if line.startswith(("#", "%")):  # comments, in every section
                continue
            if section == "autoexec":
                if line:
                    autoexec.append(line)
            elif section and "=" in line:
                k, v = line.split("=", 1)
                settings.setdefault(section, {})[k.strip().lower()] = v.strip()
    return settings, autoexec


def has_autoexec(path):
    """A config whose [autoexec] runs something."""
    return bool(read_conf([path])[1])


def split_args(line):
    return [a.strip('"') for a in re.findall(r'"[^"]*"|\S+', line)]


# DOSBox's own commands: "mount", "imgmount" and "config -..." never mean
# anything else; the others also name programs games have (INTRO.EXE), and
# count as DOSBox's only when the game has no such program.
DOSBOX_ALWAYS = ("mount", "imgmount", "config", "loadfix -f")
DOSBOX_BUILTIN = ("intro", "boot", "core", "mixer", "cycles", "rescan", "keyb")
CD_EXTS = (".iso", ".cue", ".ins", ".gog", ".ccd", ".mds", ".mdx", ".toc", ".chd")


def parse_autoexec(lines, bases, top=None, trusted=False):
    """Mounts {drive: (kind, host path or [images][, CD label])} and the
    commands run after them (DOS commands for the batch file). Mounted paths
    are relative to DOSBox's working directory, the first of bases; the
    others are tried if it has no such path, then a Windows path's last
    names under top. Unless trusted (a config the user named), a mount must
    stay inside BOUND: a config in a download can't mount the host's own
    folders. A missing one is kept as written, for the report."""
    mounts, commands = {}, []

    def find(rel):
        found = [find_ci(b, rel) for b in bases]
        if trusted and os.path.isabs(rel):
            found.append(rel)
        if top and re.match(r"[A-Za-z]:[\\/]", rel):
            found.append(find_tail(top, rel))
        for p in found:
            if p and os.path.exists(p) and (trusted or allowed(p)):
                if trusted and BOUND and not inside(p, BOUND):
                    warn("The config mounts %s, outside the folder it is in." % p)
                return p
        return None

    for line in lines:
        words = split_args(line.lstrip("@"))
        if not words:
            continue
        cmd = words[0].lower()
        if cmd in ("mount", "imgmount"):
            # Options can come anywhere (imgmount d -t iso game.ins); those
            # below take a value.
            args, opts, i = [], {}, 1
            while i < len(words):
                w = words[i]
                if w.startswith("-") and len(w) > 1:
                    has_value = w.lower() in ("-t", "-fs", "-label", "-size", "-freesize", "-ide", "-usecd", "-o",
                                              "-reservecyl", "-chs", "-lba", "-el")
                    opts[w.lower()] = words[i + 1] if has_value and i + 1 < len(words) else ""
                    i += 2 if has_value else 1
                else:
                    args.append(w)
                    i += 1
            mtype = opts.get("-t", "").lower()
            if not args:
                continue
            drive = args[0].rstrip(":").upper()
            if "-u" in opts:  # unmount
                mounts.pop(drive, None)
                continue
        if cmd == "mount" and len(args) >= 2:
            if mtype == "overlay":
                continue  # GOG's cloud-save overlay on top of C:
            path = find(args[1])
            if not path or not os.path.isdir(path):
                mounts.setdefault(drive, ("missing", args[1]))
            elif mtype == "cdrom":  # a folder DOSBox presents as a CD, perhaps with a volume label
                mounts.setdefault(drive, ("cdfolder", path, opts.get("-label") or None))
            else:
                mounts.setdefault(drive, ("dir", path))
        elif cmd == "imgmount" and len(args) >= 2:
            images = [find(w) or w for w in args[1:]]
            # A CD: -t iso or cdrom, or no -t and a CD image (DOSBox's default
            # is a hard disk); a drive number, -fs none or -size: a disk.
            cd = mtype in ("iso", "cdrom") or (not mtype and drive.isalpha() and "-fs" not in opts and
                                               "-size" not in opts and all(
                os.path.splitext(w)[1].lower() in CD_EXTS or (os.path.exists(w) and cd_kind(w) == "read")
                for w in images))
            mounts.setdefault(drive, ("image" if cd else "disk", images))
        elif cmd in ("mount", "imgmount"):
            continue
        elif cmd in ("exit", "cls", "echo", "@echo") or cmd.startswith("config"):
            continue
        else:
            commands.append(line.lstrip("@"))  # DOSBOX_BUILTIN ones are sorted out where the game's folder is known
    return mounts, commands


def dosbox_builtin(command, folders):
    """A DOSBox built-in (INTRO, BOOT, MIXER...) with no program of that
    name in the folders the game runs in."""
    words = command.split()
    if not words or words[0].lower() not in DOSBOX_BUILTIN:
        return False
    return not any(find_ci(f, words[0] + e) for f in folders if f for e in (".COM", ".EXE", ".BAT"))


def dosbox_batch(path, folder):
    """The DOSBox-only command a batch file uses (mount, imgmount, config -,
    or a built-in no program in folder is named like), or None."""
    for line in read_text(path, 65536).splitlines():
        m = DOSBOX_ONLY.match(line)
        if m:
            return m.group(1).split()[0].lower()
        words = line.strip().lstrip("@").split()
        if words and dosbox_builtin(" ".join(words), [folder]):
            return words[0].lower()
    return None


def strip_bom(text):
    """A UTF-8 cue sheet's byte order mark, read as Latin-1, removed (86Box
    and DOSBox skip it too)."""
    return text[3:] if text.startswith("\xef\xbb\xbf") else text


def cue_track(base, name):
    """The file a cue sheet's FILE line names: as written (case aside), else
    by its name alone (the sheet kept the ripper's folders), else with the
    same name and another audio extension (tracks converted after ripping)."""
    found = [find_ci(base, name), find_ci(base, re.split(r"[\\/]", name)[-1])]
    if re.search(r"\.(wav|mp3|ogg|flac|aiff?)$", name, re.I):
        stem = os.path.splitext(re.split(r"[\\/]", name)[-1])[0]
        found += [find_ci(base, stem + e) for e in (".ogg", ".mp3", ".flac", ".wav")]
    # Only a regular file inside the source: a sheet in a download can't
    # name the host's files ("../../.ssh/id_rsa") or a device (/dev/zero).
    return next((p for p in found if p and regular(p) and allowed(p)), None)


def cue_files(image):
    """The files a cue sheet (GOG's .ins, or .cue) refers to: the CD's data
    and audio tracks."""
    head = read_sheet(image, 65536)
    if not re.search(r"^\s*FILE\s", head, re.M | re.I):
        return []
    found = []
    for m in re.finditer(r'^\s*FILE\s+(?:"([^"]+)"|(\S+))', head, re.M | re.I):
        p = cue_track(os.path.dirname(image), m.group(1) or m.group(2))
        if p:
            found.append(p)
    return found


# CD images 86Box reads (an unknown extension is read as an ISO, and a CHD
# is told by its content), and some it doesn't.
CD_IMAGES = (".iso", ".cue", ".ins", ".ccd", ".mds", ".mdx", ".toc", ".chd")
CD_UNREAD = (".nrg", ".cdi", ".b5t", ".b6t", ".pdi", ".isz", ".uif", ".daa", ".ecm")
RAW_SYNC = b"\x00" + b"\xff" * 10 + b"\x00"  # starts each raw (2352-byte) CD sector


def companions(image):
    """The files a CD image's descriptor goes with: a cue sheet's tracks, a
    CloneCD .ccd's .img and .sub, an Alcohol .mds's .mdf, a cdrdao .toc's
    files."""
    ext = os.path.splitext(image)[1].lower()
    if ext == ".toc":
        names = re.findall(TOC_FILE, read_sheet(image, 65536), re.M | re.I)
        found = [cue_track(os.path.dirname(image), n[1]) for n in names]
        return list(dict.fromkeys(p for p in found if p))
    if ext in (".ccd", ".mds"):
        stem = os.path.splitext(os.path.basename(image))[0]
        found = [find_ci(os.path.dirname(image), stem + e) for e in ((".img", ".sub") if ext == ".ccd" else (".mdf",))]
        return [p for p in found if p and regular(p) and allowed(p)]
    return cue_files(image)


TOC_FILE = r'^(\s*(?:DATA|AUDIO)?FILE\s+")([^"]+)(")'  # a cdrdao .toc's files


def raw_sectors(path):
    """(sector size, offset of the 2048 data bytes in it) of a CD data track
    file: (2048, 0) for an ISO; 2352 (2448 with subchannel data) and 16 for
    raw MODE1 sectors, 24 for MODE2 ones."""
    head = read_head(path, 2448 + 16)
    if len(head) < 16 or head[:12] != RAW_SYNC:
        return 2048, 0
    return 2448 if head[2448:2460] == RAW_SYNC else 2352, 24 if head[15] == 2 else 16


def data_track(image):
    """The file holding a CD image's first data track."""
    ext = os.path.splitext(image)[1].lower()
    if ext in (".cue", ".ins") or (ext not in (".ccd", ".mds", ".toc") and cue_files(image)):
        name = None
        for line in read_sheet(image, 65536).splitlines():
            m = re.match(r'\s*FILE\s+(?:"([^"]+)"|(\S+))', line, re.I)
            if m:
                name = m.group(1) or m.group(2)
            elif re.match(r"\s*TRACK\s+\d+\s+MODE", line, re.I) and name:
                return cue_track(os.path.dirname(image), name)
        return None
    if ext in (".ccd", ".mds", ".toc"):
        return next(iter(companions(image)), None)
    return image


class CdFs:
    """A CD's ISO 9660 (or High Sierra) file system, as DOS's CD extensions
    show it (primary names, no Joliet), read from its first data track."""

    def __init__(self, track):
        if not regular(track):
            raise ValueError("not a regular file")
        self.size, self.offset = raw_sectors(track)
        self.f = open(track, "rb")
        pvd = self.block(16)
        if pvd[:6] == b"\x01CD001":
            self.hs, label, root, blocks = False, pvd[40:72], pvd[156:190], pvd[80:84]
        elif pvd[8:14] == b"\x01CDROM":
            self.hs, label, root, blocks = True, pvd[48:80], pvd[180:214], pvd[88:92]
        else:
            self.f.close()
            raise ValueError("no ISO 9660 or High Sierra volume")
        self.label = label.decode("ascii", "replace").strip()
        self.blocks = struct.unpack("<I", blocks)[0]
        self.root = self.record(root)

    def block(self, lba):
        self.f.seek(lba * self.size + self.offset)
        return self.f.read(2048)

    def record(self, rec):
        """(name, is a folder, extent, size) of a directory record."""
        name = rec[33:33 + rec[32]].decode("ascii", "replace").split(";")[0].rstrip(".")
        flags = rec[24 if self.hs else 25]
        return name, bool(flags & 2), struct.unpack_from("<I", rec, 2)[0], struct.unpack_from("<I", rec, 10)[0]

    def listdir(self, entry=None):
        name, is_dir, extent, length = entry or self.root
        found = []
        for n in range(min((length + 2047) // 2048, 64)):
            if extent + n >= max(self.blocks, 17):
                break
            data, i = self.block(extent + n), 0
            while i + 33 < len(data) and data[i] >= 34:  # a 0 length pads to the next sector
                rec = data[i:i + data[i]]
                if rec[32:34] not in (b"\x01\x00", b"\x01\x01"):  # "." and ".."
                    found.append(self.record(rec))
                i += data[i]
        return found

    def head(self, entry, n=4096):
        data = b""
        for k in range((min(n, entry[3]) + 2047) // 2048):
            data += self.block(entry[2] + k)
        return data[:min(n, entry[3])]

    def close(self):
        self.f.close()


def cd_root(image):
    """(volume label, names in the root folder, folders ending in \\) of a CD
    as DOS sees it, from an image's first data track or a folder used as a
    CD; None if it can't be read."""
    if os.path.isdir(image):
        return os.path.basename(image.rstrip("/")).upper(), sorted(
            n.upper() + ("\\" if os.path.isdir(os.path.join(image, n)) else "") for n in os.listdir(image))
    track = data_track(image)
    try:
        fs = CdFs(track)
    except (TypeError, ValueError, OSError, struct.error, IndexError):
        return None
    try:
        return fs.label, [e[0] + ("\\" if e[1] else "") for e in fs.listdir()]
    except (OSError, struct.error, IndexError):
        return None
    finally:
        fs.close()


# 86Box's CD-ROM drive reads ISO images (2048-byte sectors, or raw 2352- and
# 2448-byte ones), cue sheets (with BINARY data tracks and WAVE/AIFF/MP3
# audio tracks, opened with libsndfile, which also reads OGG and FLAC under
# "WAVE", and MP3 from version 1.1 on), CHD, and folders (as a "virtual
# ISO"). Audio tracks must be 44.1 kHz stereo, or 86Box refuses the whole
# disc.
CUE_TYPES = ("BINARY", "MOTOROLA", "WAVE", "AIFF", "MP3")
AUDIO = re.compile(r"\.(wav|mp3|ogg|flac|aiff?|opus)$", re.I)


def audio_format(path):
    """(channels, sample rate) of a WAV, AIFF, OGG (Vorbis, Opus) or FLAC file;
    None if it can't be told (MP3, or another format)."""
    head = read_head(path, 65536)
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        i = 12
        while i + 16 <= len(head):
            size = struct.unpack_from("<I", head, i + 4)[0]
            if head[i:i + 4] == b"fmt ":
                return struct.unpack_from("<H", head, i + 10)[0], struct.unpack_from("<I", head, i + 12)[0]
            i += 8 + size + (size & 1)
    elif head[:4] == b"OggS":
        j = head.find(b"\x01vorbis")
        if j >= 0 and j + 16 <= len(head):
            return head[j + 11], struct.unpack_from("<I", head, j + 12)[0]
        j = head.find(b"OpusHead")
        if j >= 0 and j + 10 <= len(head):
            return head[j + 9], 48000  # Opus always decodes at 48 kHz
    elif head[:4] == b"fLaC" and len(head) >= 26:
        rate = (head[18] << 12) | (head[19] << 4) | (head[20] >> 4)
        return ((head[20] >> 1) & 7) + 1, rate
    elif head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
        i = 12
        while i + 26 <= len(head):
            size = struct.unpack_from(">I", head, i + 4)[0]
            if head[i:i + 4] == b"COMM":  # rate: an 80-bit float
                exp, mant = struct.unpack_from(">HQ", head, i + 16)
                try:
                    rate = int(math.ldexp(mant, (exp & 0x7FFF) - 16383 - 63))
                except (OverflowError, ValueError):
                    return None
                channels = struct.unpack_from(">H", head, i + 8)[0]
                return (channels, rate) if channels and 0 < rate < 1000000 else None
            i += 8 + size + (size & 1)
    return None


def loose_tracks(image, others=()):
    """Music tracks next to a CD image that has no cue sheet: named after the
    image ("game 03.ogg", "Game (Disc 1) (Track 02).wav"), or "Track02.mp3"
    when no other image's name (others) is in theirs. [(track number,
    file)], one file per number (WAV before FLAC, OGG, MP3)."""
    folder, stem = os.path.dirname(image), os.path.splitext(os.path.basename(image))[0].lower()
    other = [os.path.splitext(os.path.basename(o))[0].lower() for o in others if o != image]
    rank = {".wav": 0, ".flac": 1, ".ogg": 2, ".opus": 3, ".mp3": 4}
    found = {}
    for n in os.listdir(folder):
        m = AUDIO.search(n)
        if not m or not regular(os.path.join(folder, n)):
            continue
        s = n[:m.start()].lower()
        t = re.fullmatch(r"%s[ _-]*\(?(?:track)?[ _-]*0*(\d{1,2})\)?" % re.escape(stem), s)
        if t is None and not any(o and o in s for o in other):
            t = re.fullmatch(r"(?:.*\b)?track[ _-]*0*(\d{1,2})\)?", s)
        if t and int(t.group(1)) >= 2:
            k = int(t.group(1))
            if k in found:
                keep, drop = sorted([found[k], n], key=lambda x: rank.get(os.path.splitext(x)[1].lower(), 9))
                warn("Track %d is there twice (%s, %s): using %s." % (k, keep, drop, keep))
                found[k] = keep
            else:
                found[k] = n
    return [(k, os.path.join(folder, found[k])) for k in sorted(found)]


def silence(path, seconds=2):
    """A silent CD audio track (44.1 kHz stereo WAV)."""
    import wave
    w = wave.open(path, "wb")
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(44100)
    w.writeframes(bytes(44100 * 4 * seconds))
    w.close()


def link_or_copy(src, dst):
    """A hard link where it can (no second copy of a big track), else a copy;
    nothing when dst is src already (a disc imported again from the VM's own
    copy). Never a device or FIFO."""
    if not regular(src):
        raise SystemExit("%s is no regular file: not copying it." % src)
    if os.path.lexists(dst):
        if os.path.exists(dst) and os.path.samefile(src, dst):
            return
        os.unlink(dst)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def overlaps(a, b):
    """True when folder a is folder b or holds it, or the other way round."""
    a, b = os.path.realpath(a), os.path.realpath(b)
    return os.path.commonpath([a, b]) in (a, b)


def volume_label(label):
    """The folder name that gives 86Box's virtual ISO a volume label: its
    label is the folder's name, upper case, other characters made "_"."""
    return re.sub(r"[^A-Z0-9_]", "_", label.upper())[:32]


def convert_track(track, dest, name):
    """dest/name: track converted to 44.1 kHz stereo WAV with ffmpeg, written
    to another name first and then moved over, so it never writes through a
    hard link into the user's own file. False without ffmpeg or on failure."""
    if not shutil.which("ffmpeg"):
        return False
    out = os.path.join(dest, name)
    part = out + ".part"
    ok = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", track, "-ac", "2", "-ar", "44100",
                         "-c:a", "pcm_s16le", "-f", "wav", part], capture_output=True).returncode == 0
    if ok:
        os.replace(part, out)
    elif os.path.exists(part):
        os.unlink(part)
    return ok


def bare_name(name):
    """A file name without its folders (or ".." or "/"), for a sheet that
    names a file that isn't there."""
    n = re.split(r"[\\/]", name)[-1]
    return n if n not in ("", ".", "..") else "missing"


def mds_paths(mds):
    """The paths an Alcohol .mds names for its data (86Box opens them as
    written, "../" and "/x" too): names with a folder in them."""
    data = read_head(mds, 1 << 20)
    names = [m.group(1).decode("latin-1") for m in re.finditer(rb"([\x20-\x7e]{3,})\x00", data)]
    names += [m.group(1).decode("utf-16-le", "replace") for m in re.finditer(rb"((?:[\x20-\x7e]\x00){3,})\x00\x00", data)]
    return [n for n in names if re.search(r"[\\/]|\.\.", n) and re.search(r"\.\w{2,4}$", n)]


def playable(track):
    """86Box will play the track as it is, or ffmpeg can make it so."""
    return audio_format(track) == (2, 44100) or bool(shutil.which("ffmpeg"))


def rewrite_cue(lines, base, dest, stem, notes, known=None):
    """A cue sheet as 86Box needs it, written to dest/stem.cue: FILE names as
    they are on disk (found in base, or in known: {name: path}), file types
    86Box knows, its tracks hard-linked next to it, and audio it can play
    (converted with ffmpeg). Returns (its path, how many of its files were
    found, how many it names)."""
    out, found, named = [], 0, 0
    for line in lines:
        m = re.match(r'(\s*)FILE\s+(?:"([^"]+)"|(\S+))\s*(\S*)', line, re.I)
        if not m:
            out.append(line)
            continue
        indent, name, ftype = m.group(1), m.group(2) or m.group(3), m.group(4).upper()
        named += 1
        track = (known or {}).get(name) or cue_track(base, name)
        if track is None:  # written as a bare name: 86Box would follow "../" or "/x" out of the VM's folder
            notes.append("the cue sheet names %s, which isn't there: 86Box will refuse the disc" % name)
            out.append('%sFILE "%s" %s' % (indent, bare_name(name), ftype or "BINARY"))
            continue
        found += 1
        new = os.path.basename(track)
        if ftype not in ("BINARY", "MOTOROLA"):
            # 86Box plays a track only at 44.1 kHz stereo, and an MP3 only
            # with libsndfile 1.1 or later: other tracks, and those whose
            # format can't be told, are converted to WAV when ffmpeg is there.
            fmt = audio_format(track)
            ftype = ftype if ftype in ("WAVE", "AIFF", "MP3") else "WAVE"
            if fmt != (2, 44100):
                wav = os.path.splitext(new)[0] + ".wav"
                what = "%d channels, %d Hz" % fmt if fmt else "format unknown"
                if convert_track(track, dest, wav):
                    notes.append("converted %s (%s) to 44.1 kHz stereo" % (new, what))
                    new, ftype, track = wav, "WAVE", None
                elif fmt:
                    notes.append("%s is %d channels at %d Hz; 86Box plays only 44.1 kHz stereo and refuses the "
                                 "disc: install ffmpeg (sudo apt install ffmpeg) and import the game again, "
                                 "which converts it" % (new, fmt[0], fmt[1]))
                else:
                    notes.append("can't tell whether 86Box plays %s (it needs 44.1 kHz stereo, and an MP3 needs "
                                 "libsndfile 1.1): if the drive stays empty, install ffmpeg (sudo apt install "
                                 "ffmpeg) and import the game again, which converts it" % new)
        if track is not None:
            link_or_copy(track, os.path.join(dest, new))
        out.append('%sFILE "%s" %s' % (indent, new, ftype))
    to = os.path.join(dest, stem + ".cue")
    text = "\n".join(out) + "\n"
    # File names as on disk: UTF-8 on Linux, which needs the byte order
    # mark for 86Box on Windows to read them so.
    with open(to, "w", encoding="utf-8-sig" if not text.isascii() else "ascii", errors="surrogateescape",
              newline="\r\n") as f:
        f.write(text)
    return to, found, named


def prepare_cd(source, dest, label=None, tracks=()):
    """Put a CD (an image, a cue sheet with its tracks, or a folder) into the
    folder dest for 86Box: a cue sheet is renamed .cue (86Box goes by the
    extension), names the tracks as they are on disk (case), uses file types
    86Box knows, and has audio tracks 86Box can play (converted with ffmpeg
    when needed and possible). An image with loose music tracks ([(number,
    file)]) gets a cue sheet for them. A folder is copied to one named for
    its volume label (DOSBox's -label). Returns (path of what to put in the
    drive, or None if nothing of it can be, notes)."""
    notes = []
    os.makedirs(dest, exist_ok=True)
    if os.path.isdir(source):
        name = volume_label(label) if label else os.path.basename(source.rstrip("/"))
        if label and name != label.upper():
            notes.append("DOSBox labels this CD %s; in the VM it is %s (86Box's folder CDs take the folder's "
                         "name, in ISO 9660 characters)" % (label, name))
        to = os.path.join(dest, name)
        if overlaps(to, source):
            return source, notes  # already in the VM's folder: used where it is
        if os.path.exists(to):
            shutil.rmtree(to)
        shutil.copytree(source, to, copy_function=lambda a, b: link_or_copy(a, b),
                        ignore=lambda d, names: [n for n in names if not plain(os.path.join(d, n))])
        return to, notes
    stem, ext = os.path.splitext(os.path.basename(source))
    if tracks and not all(playable(t) for _, t in tracks):
        notes.append("its music tracks (%s) need converting, which takes ffmpeg (sudo apt install ffmpeg): the "
                     "disc goes in without its music until you install it and import the game again" %
                     ", ".join(os.path.basename(t) for _, t in tracks))
        tracks = ()
    if tracks:  # an image and its music, without a cue sheet: write one
        size, offset = raw_sectors(source)
        lines = ['FILE "%s" BINARY' % os.path.basename(source),
                 "  TRACK 01 MODE%d/%d" % (2 if offset == 24 else 1, size), "    INDEX 01 00:00:00"]
        known, numbers = {}, dict(tracks)
        for n in range(2, max(numbers) + 1):
            t = numbers.get(n)
            if t is None:  # a silent track keeps the later ones at their numbers
                t = os.path.join(dest, "silence%02d.wav" % n)
                silence(t)
                notes.append("track %d isn't there: a silent one stands in for it" % n)
            known[os.path.basename(t)] = t
            lines += ['FILE "%s" %s' % (os.path.basename(t), "MP3" if t.lower().endswith(".mp3") else "WAVE"),
                      "  TRACK %02d AUDIO" % n, "    PREGAP 00:02:00", "    INDEX 01 00:00:00"]
        known[os.path.basename(source)] = source
        notes.append("wrote a cue sheet for it and its music (%s)" % ", ".join(
            os.path.basename(t) for _, t in tracks))
        return rewrite_cue(lines, os.path.dirname(source), dest, stem, notes, known)[0], notes
    if ext.lower() == ".toc":  # its files next to it, named as copied
        text = read_sheet(source, 1 << 20)

        def place(m):
            f = cue_track(os.path.dirname(source), m.group(2))
            if f is None:
                notes.append("the .toc names %s, which isn't there: 86Box will refuse the disc" % m.group(2))
                return m.group(1) + bare_name(m.group(2)) + m.group(3)
            link_or_copy(f, os.path.join(dest, os.path.basename(f)))
            return m.group(1) + os.path.basename(f) + m.group(3)
        text = re.sub(TOC_FILE, place, text, flags=re.M | re.I)
        to = os.path.join(dest, os.path.basename(source))
        with open(to, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write(text)
        return to, notes
    if ext.lower() not in (".cue", ".ins") and not cue_files(source):
        to = os.path.join(dest, os.path.basename(source))  # an ISO, or another image with its files
        link_or_copy(source, to)
        if ext.lower() == ".mds" and mds_paths(source):
            os.unlink(to)
            notes.append("it names its data as %s, a path 86Box would follow out of the VM's folder: convert the "
                         "image to an ISO or BIN/CUE" % mds_paths(source)[0])
            return None, notes
        if ext.lower() in (".ccd", ".mds"):
            for f in companions(source):  # 86Box opens a .ccd's or .mds's files by its stem, in lower case
                link_or_copy(f, os.path.join(dest, stem + os.path.splitext(f)[1].lower()))
        if ext.lower() in CD_UNREAD:
            notes.append("86Box doesn't read %s images: convert it to an ISO or a cue sheet (BIN/CUE)" % ext)
        elif ext.lower() not in CD_IMAGES or ext.lower() == ".iso":
            more = music_after_data(source)
            if more:
                notes.append("it holds %d MB more than its data track: CD music, probably, which 86Box plays "
                             "only with the cue sheet that came with it" % more)
        return to, notes  # (86Box finds raw 2352- and 2448-byte sectors in an image without a cue sheet)
    to, found, named = rewrite_cue(read_sheet(source).splitlines(), os.path.dirname(source), dest, stem, notes)
    if named and not found:
        os.unlink(to)
        notes.append("none of the files its cue sheet names is there: it can't go into the drive")
        return None, notes
    return to, notes


def music_after_data(image):
    """MB in a lone image after its data track's file system (CD music
    tracks, ripped without their cue sheet), or 0."""
    try:
        fs = CdFs(image)
    except (ValueError, OSError, struct.error, IndexError):
        return 0
    used = fs.blocks * fs.size
    fs.close()
    extra = os.path.getsize(image) - used
    return extra >> 20 if extra > (2 << 20) else 0


def vm_memory_mb(cfg):
    """The VM's memory in MB (86box.cfg's mem_size, in KB), or None."""
    try:
        with open(cfg, encoding="utf-8", errors="replace") as f:
            m = re.search(r"^\s*mem_size\s*=\s*(\d+)", f.read(), re.M)
    except OSError:
        return None
    return int(m.group(1)) // 1024 if m else None


def set_cd_image(cfg, image, others=()):
    """Put image into the VM's first CD-ROM drive in 86box.cfg (as a path
    relative to the VM's folder), with others (more discs) in the drive's
    recent images (86Box's Media menu), and the discs already there (an
    earlier game's) after them. Returns those earlier discs; None if the VM
    has no CD-ROM drive."""
    with open(cfg, encoding="utf-8") as f:
        lines = f.read().splitlines()
    sec = next((i for i, l in enumerate(lines) if l.strip().lower() == "[floppy and cd-rom drives]"), None)
    if sec is None:
        return None
    end = next((i for i in range(sec + 1, len(lines)) if lines[i].startswith("[")), len(lines))
    keys = [l.split("=", 1)[0].strip() for l in lines[sec + 1:end]]
    if "cdrom_01_parameters" not in keys or "none" in lines[sec + 1 + keys.index("cdrom_01_parameters")]:
        return None
    vmdir = os.path.dirname(os.path.abspath(cfg))
    old = sorted(((k, l.split("=", 1)[1].strip()) for k, l in zip(keys, lines[sec + 1:end])
                  if re.match(r"cdrom_01_image_(path|history_\d+)$", k)), key=lambda kv: kv[0] != "cdrom_01_image_path")
    body = [l for k, l in zip(keys, lines[sec + 1:end])
            if not re.match(r"cdrom_01_image_(path|history_\d+)$", k)]
    rel = [os.path.relpath(p, vmdir) for p in [image] + list(others)]
    where = {os.path.normpath(os.path.join(vmdir, r)) for r in rel}
    earlier = list(dict.fromkeys(v for _, v in old if v and os.path.normpath(os.path.join(vmdir, v)) not in where))
    history = (rel[1:] + earlier)[:10]
    at = next((i + 1 for i, l in enumerate(body) if l.split("=", 1)[0].strip() == "cdrom_01_ide_channel"),
              next((i + 1 for i, l in enumerate(body) if l.split("=", 1)[0].strip() == "cdrom_01_parameters")))
    body[at:at] = ["cdrom_01_image_path = %s" % rel[0]] + [
        "cdrom_01_image_history_%02d = %s" % (n + 1, r) for n, r in enumerate(history)]
    lines[sec + 1:end] = body
    with open(cfg, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return [e for e in earlier if e in history]


CD_MARK = ".gog_import"  # in each game's folder under cd/: the game's name


def disc_id(what):
    """What tells a disc from another with the same file names: a folder's
    name, an image's volume label and data size."""
    if os.path.isdir(what):
        return os.path.basename(what.rstrip("/")).lower()
    root, track = cd_root(what), data_track(what)
    return "%s %d" % (root[0] if root else "", os.path.getsize(track) if track and regular(track) else 0)


def cd_folder(vmdir, short, game, ident):
    """The game's folder under the VM's cd/: SHORT, or SHORT with a digit
    when another game (or another disc of the same name) has that one; its
    mark file names the game and the disc."""
    for n in range(10):
        d = os.path.join(vmdir, "cd", short if n == 0 else short[:7] + str(n))
        mark = os.path.join(d, CD_MARK)
        if not os.path.exists(d):
            os.makedirs(d)
            with open(mark, "w", encoding="utf-8") as f:
                f.write(game + "\n" + ident + "\n")
            return d
        lines = read_head(mark, 4096).decode("utf-8", "replace").splitlines() if regular(mark) else []
        if lines and lines[0].strip().lower() == game.lower() and (len(lines) < 2 or lines[1].strip() == ident):
            return d
    sys.exit("cd/%s and nine more folders like it hold other games' CDs." % short)


def attach_cds(discs, cfg, short, game):
    """Put the game's CD (the first of discs, [(DOSBox's drive letter or
    None, image or folder, volume label or None, loose music tracks)]) into
    the VM's CD-ROM drive when the image is a VM's disk (86box.cfg next to
    it), copied to the game's folder under its cd/ (in CD1, CD2... when
    there are several); further discs go into the drive's recent images.
    Else say what to attach."""
    if not discs:
        return
    if not os.path.isfile(cfg):
        for drive, what, _, _ in discs:
            say("%s: %s. Put it in the VM's CD-ROM drive (86Box's Media menu)." % (
                "Drive %s: is a CD in DOSBox" % drive if drive else "The game's CD", what))
        REPORT["cd"] = discs[0][1]
        return
    vmdir = os.path.dirname(os.path.abspath(cfg))
    found = []
    for drive, what, label, tracks in discs:
        if not os.path.exists(what):
            warn("Drive %s: is the CD %s in DOSBox, which isn't there." % (drive, what))
        elif not (os.path.isdir(what) or regular(what)) or not allowed(what):
            warn("The CD %s isn't a file or folder in what was given: left out." % what)
        else:
            found.append((drive, what, label, tracks))
    ready, dest = [], None
    for n, (drive, what, label, tracks) in enumerate(found):
        if inside(what, os.path.join(vmdir, "cd")):
            ready.append((drive, what))  # the VM's own copy, from an earlier import: used where it is
            continue
        dest = dest or cd_folder(vmdir, short, game, disc_id(found[0][1]))
        to, notes = prepare_cd(what, os.path.join(dest, "CD%d" % (n + 1)) if len(found) > 1 else dest, label,
                               tracks)
        for note in notes:
            warn("CD %s: %s." % (os.path.basename(to or what), note))
        if to:
            ready.append((drive, to))
    if not ready:
        return
    earlier = set_cd_image(cfg, ready[0][1], [to for _, to in ready[1:]])
    if earlier is None:
        warn("The VM has no CD-ROM drive: it was made from a kit older than CD support. Unpack the current kit "
             "into another folder and copy its boot.img and 86box.cfg over this VM's (a drive added in 86Box's "
             "Settings alone gives DOS no D:: the CD driver is on the new boot.img); hd.img and its games stay. "
             "Then import the game again, or put %s in the drive from 86Box's Media menu." % ready[0][1])
        return
    REPORT["cd"] = os.path.relpath(ready[0][1], vmdir)
    REPORT["other_discs"] = [os.path.relpath(to, vmdir) for _, to in ready[1:]]
    REPORT["earlier_discs"] = earlier
    say("CD: %s is in the VM's CD-ROM drive, D: in DOS (%s)." % (
        os.path.basename(ready[0][1]), "copied to " + os.path.dirname(ready[0][1]) if ready[0][1] != found[0][1]
        else "the VM's own copy"))
    if ready[0][0] not in ("D", None):
        warn("DOSBox gave the game its CD as %s:. If the game needs that letter, change the VM's "
             "A:\\AUTOEXEC.BAT line to SHSUCDX /D:?CDROM1,%s (SETUP-LINUX.md, \"Change the boot floppy\")."
             % (ready[0][0], ready[0][0]))
    if len(ready) > 1:
        say("More discs: %s, in 86Box's Media menu (CD-ROM 1, recent images) to change discs." % ", ".join(
            os.path.relpath(to, vmdir) for _, to in ready[1:]))
    if earlier:
        say("The VM has one CD-ROM drive: the CDs that were in it stay in its recent images (86Box's Media menu, "
            "CD-ROM 1), for the games that need them: %s." % ", ".join(
                earlier[:3] + (["%d more" % (len(earlier) - 3)] if len(earlier) > 3 else [])))


def cd_kind(path):
    """"read" for a CD image 86Box reads, "unread" for one it doesn't, None
    for another file. Extensions games also use for their own data (.bin,
    .img, .toc...) are checked by content."""
    ext = os.path.splitext(path)[1].lower()
    if ext in CD_UNREAD:
        return "unread"
    if not regular(path):
        return None
    head = read_head(path, 4096)
    text = strip_bom(head.decode("latin-1"))
    if ext in (".cue", ".ins"):
        ok = re.search(r"^\s*FILE\s", text, re.M | re.I) and re.search(r"^\s*TRACK\s", text, re.M | re.I)
    elif ext == ".toc":  # cdrdao's
        ok = re.search(r"^\s*(CD_ROM|CD_ROM_XA|CD_DA|CD_I)\s*$|^\s*TRACK\s+(AUDIO|MODE)", text, re.M)
    elif ext == ".ccd":
        ok = "[CloneCD]" in text
    elif ext in (".mds", ".mdx"):
        ok = head.startswith(b"MEDIA DESCRIPTOR")
    elif ext == ".chd":
        ok = head.startswith(b"MComprHD")
    elif ext == ".iso":
        ok = True
    elif ext in (".bin", ".img", ".gog", ".mdf"):  # a data track, or a CD without its descriptor
        ok = raw_sectors(path)[0] != 2048 or cd_root(path) is not None
    else:
        ok = False
    return "read" if ok else None


def disc_order(path):
    """Sorts "Disc 2" before "Disc 10"."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", path)]


CD_FOLDER = re.compile(r"(cd|cdrom|cd-?rom|cd[ _-]?\d|cd-(us|uk|en|eu|de|fr|it|es|nl|jp))$", re.I)


def holds_programs(folder):
    try:
        return any(PROGRAM.search(n) and regular(os.path.join(folder, n)) for n in os.listdir(folder))
    except OSError:
        return False


def find_cds(top):
    """The CDs under top, disc 1 first: images (one per disc: a .cue before
    a .ccd or .mds for the same files; an image with loose music tracks gets
    them), and folders named like a CD (CD, CDROM, CD1, CD-US) holding files
    but no images, beside the game's folder (not inside a folder of
    programs, and not the game's own programs). Returns ([(image or folder,
    music tracks)], the real paths of all their files, the images 86Box
    can't read)."""
    found, data, unread, folders = [], [], [], []
    for dirpath, dirnames, filenames in os.walk(top):
        dirnames[:] = [d for d in dirnames if d not in JUNK_DIRS]
        for n in filenames:
            p, ext = os.path.join(dirpath, n), os.path.splitext(n)[1].lower()
            if not regular(p):
                continue
            if ext in (".bin", ".img", ".gog", ".mdf", ".iso"):
                data.append(p)  # data tracks, or a CD without a descriptor: after the descriptors
            elif ext in CD_IMAGES + CD_UNREAD:
                kind = cd_kind(p)
                (found if kind == "read" else unread if kind else []).append(p)
        if dirpath != top and CD_FOLDER.match(os.path.basename(dirpath)) and filenames:
            folders.append(dirpath)
    rank = {".cue": 0, ".ins": 1, ".ccd": 2, ".mds": 3, ".toc": 4}
    images, parts = [], {os.path.realpath(p) for p in unread}
    for p in sorted(found, key=lambda p: rank.get(os.path.splitext(p)[1].lower(), 5)):
        mine = {os.path.realpath(c) for c in companions(p)}
        if os.path.realpath(p) in parts or mine & parts:
            continue  # another descriptor of a disc already found
        images.append(p)
        parts |= mine | {os.path.realpath(p)}
    for p in data:  # inside a game's folder an .iso, too, must hold a CD's file system
        if os.path.realpath(p) not in parts and (raw_sectors(p)[0] != 2048 or cd_root(p) is not None):
            images.append(p)
            parts.add(os.path.realpath(p))
    discs = []
    for p in images:
        tracks = loose_tracks(p, images) if os.path.splitext(p)[1].lower() in (".iso", ".bin", ".img", ".gog") else []
        parts |= {os.path.realpath(t) for _, t in tracks}
        discs.append((p, tracks))
    for d in folders:
        files = [os.path.join(dp, f) for dp, _, fs in os.walk(d) for f in fs]
        above = os.path.dirname(d)
        nested = False
        while above != top and inside(above, top):  # inside a folder of programs: the game's own data
            nested = nested or holds_programs(above)
            above = os.path.dirname(above)
        if os.path.dirname(d) != top:  # (a CD folder right beside the programs at the top is still a CD)
            nested = nested or holds_programs(top)
        own = holds_programs(d) and not any(holds_programs(dp) for dp, _, _ in os.walk(top)
                                            if not inside(dp, d))
        if nested or own or any(os.path.realpath(f) in parts for f in files) or \
                any(overlaps(d, other) for other, _ in discs if os.path.isdir(other)):
            continue
        discs.append((d, []))
        parts |= {os.path.realpath(f) for f in files}
        warn("%s looks like the game's CD (a folder named so): it goes into the CD-ROM drive, not onto "
             "C:." % os.path.relpath(d, top))
    return sorted(discs, key=lambda d: disc_order(d[0])), parts, unread


# ---- floppy images -------------------------------------------------------------

FLOPPY_SIZES = {163840, 184320, 327680, 368640, 737280, 1228800, 1474560, 1720320, 1763328, 2949120}


def floppy_image(path):
    """A FAT floppy disk image (an install disk, often): its size, and a
    boot sector with 512-byte sectors and a floppy's media byte."""
    if not regular(path) or os.path.getsize(path) not in FLOPPY_SIZES or \
            not re.search(r"\.(img|ima|vfd|flp|dsk|144|720|360)$", path, re.I):
        return False
    boot = read_head(path, 512)
    return len(boot) == 512 and struct.unpack_from("<H", boot, 11)[0] == 512 and boot[21] >= 0xF0


def unpack_floppies(images, into):
    """Each floppy image's files into into/DISK1, DISK2... (disk order by
    name); returns those folders."""
    out = []
    for n, img in enumerate(sorted(images, key=disc_order)):
        d = os.path.join(into, "DISK%d" % (n + 1))
        os.makedirs(d, exist_ok=True)
        r = mtools("mcopy", "-s", "-n", "-i", img, "::*", d + "/", check=False)
        if r.returncode or not os.listdir(d):
            warn("%s has no files mtools can read (a booter disk needs 86Box's floppy drive and a reboot)."
                 % os.path.basename(img))
        out.append(d)
    return out


# ---- copying -------------------------------------------------------------------

BAD_NAME = re.compile(r'[\x00-\x1f*?:<>|"\\/]')
DEVICE_NAME = re.compile(r"(con|prn|aux|nul|com\d|lpt\d|clock\$)(\..*)?$", re.I)
SHORT_NAME = re.compile(r"[A-Za-z0-9!#$%&'()\-@^_`{}~\x80-\uffff]{1,8}(\.[A-Za-z0-9!#$%&'()\-@^_`{}~\x80-\uffff]{1,3})?$")


def shown(rel, name):
    """A file's path for the report, control characters made visible."""
    return re.sub(r"[\x00-\x1f]", "?", name if rel == "." else os.path.join(rel, name))


def plain(path, root=None):
    """A plain file or folder to copy: not a special file, and not a link
    (one inside root, to a plain file, counts as that file)."""
    st = os.lstat(path)
    if stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode):
        return True
    if stat.S_ISLNK(st.st_mode) and root and os.path.isfile(path):
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(root)]) == os.path.realpath(root)
    return False


def copy_tree(src, image, offset, dest, exclude=(), gog=True, owners=None, game=None):
    """Copy src's game files to ::dest on the image, leaving out GOG's own
    files at its top (with gog), manuals (PDF), the files and folders in
    exclude (CD images, which the VM mounts as a drive), links and special
    files, and names FAT can't hold. Checks the free space first; on a
    failure removes what it copied. Returns (files, bytes)."""
    exclude = {os.path.realpath(p) for p in exclude}
    target = "%s@@%d" % (image, offset)
    plan, dirs, longnames = [], [], []
    for dirpath, dirnames, filenames in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        if rel == ".":  # GOG's own folders; DOSBox for Windows, next to other games
            dirnames[:] = [d for d in dirnames if (d.lower() not in SKIP_DIRS if gog else
                                                   not d.lower().startswith("dosbox"))]
        keep, seen = [], set()
        for d in sorted(dirnames):
            p = os.path.join(dirpath, d)
            if os.path.realpath(p) in exclude or d in JUNK_DIRS or not plain(p) or \
                    all(os.path.realpath(os.path.join(dp, f)) in exclude for dp, _, fs in os.walk(p) for f in fs) \
                    and any(fs for _, _, fs in os.walk(p)):
                continue
            if BAD_NAME.search(d) or DEVICE_NAME.match(d) or d.lower() in seen:
                REPORT["skipped"].append("%s (%s)" % (shown(rel, d), "a name DOS can't have" if d.lower() not in seen
                                                      else "the same name as another but for case"))
                continue
            seen.add(d.lower())
            keep.append(d)
        dirnames[:] = keep
        for d in keep:
            dirs.append("/".join(p for p in (dest, rel, d) if p and p != "."))
        for name in sorted(filenames):
            p = os.path.join(dirpath, name)
            if (rel == "." and gog and SKIP_FILES.match(name)) or name.lower().endswith(".pdf") or \
                    name in JUNK_FILES or os.path.realpath(p) in exclude:
                continue
            if not plain(p, src):
                REPORT["skipped"].append("%s (a link or special file)" % shown(rel, name))
                continue
            if BAD_NAME.search(name) or DEVICE_NAME.match(name) or name.lower() in seen:
                REPORT["skipped"].append("%s (%s)" % (shown(rel, name), "a name DOS can't have" if name.lower() not in
                                                      seen else "the same name as another but for case"))
                continue
            seen.add(name.lower())
            if not SHORT_NAME.match(name):
                longnames.append(os.path.join(rel, name) if rel != "." else name)
            plan.append((p, "/".join(x for x in (dest, rel, name) if x and x != "."), os.path.getsize(p)))
    free, cluster = image_space(target)
    need = sum(-(-size // cluster) * cluster for _, _, size in plan) + (len(dirs) + 1) * cluster
    existed = mtools("mdir", "-b", "-i", target, "::" + dest, check=False).returncode == 0
    earlier = 0  # this game's copy from before, which this one replaces
    if existed:
        used = re.match(r"\S+\s+(\d+)", mtools("mdu", "-s", "-i", target, "::" + dest, check=False).stdout.strip())
        earlier = int(used.group(1)) * cluster if used else 0
    if free is not None and need > free + earlier:
        sys.exit("C: has %d MB free%s, and the game needs %d MB: remove a game from it (SETUP-LINUX.md, \"Remove "
                 "a game\"), or give this game a VM of its own (unpack the kit into another folder; the kit's C: "
                 "is already about as big as FAT16 allows here)." % ((free + earlier) >> 20, " (with this game's earlier copy)"
                                                    if earlier else "", -(-need // (1 << 20))))
    parts = [p for p in dest.split("/") if p]
    for k in range(1, len(parts) + 1):  # each folder on the way, then the game's folders
        r = mtools("mmd", "-D", "s", "-i", target, "::/" + "/".join(parts[:k]), check=False)  # -D s: one there is fine
        if r.returncode and mtools("mdir", "-b", "-i", target, "::/" + "/".join(parts[:k]), check=False).returncode:
            sys.exit("mmd could not make C:\\%s: %s" % ("\\".join(parts[:k]), (r.stderr or r.stdout).strip()))
    for d in dirs:
        mtools("mmd", "-D", "s", "-i", target, "::" + d, check=False)
    for p, to, size in plan:
        r = mtools("mcopy", "-o", "-i", target, p, "::" + to, check=False)
        if r.returncode:
            if not existed:
                mtools("mdeltree", "-i", target, "::" + dest, check=False)
            sys.exit("mcopy could not copy %s to C:%s: %s%s" % (
                p, to.replace("/", "\\"), (r.stderr or r.stdout).strip() or "exit code %d" % r.returncode,
                "" if existed else " (C:%s removed again)" % dest.replace("/", "\\")))
    if longnames:
        say("%d file names are longer than DOS's 8.3; DOS sees them shortened (NAME~1.EXT): %s%s." % (
            len(longnames), ", ".join(longnames[:5]), ", ..." if len(longnames) > 5 else ""))
    return len(plan), sum(size for _, _, size in plan)


# ---- programs ------------------------------------------------------------------

PROGRAM = re.compile(r"\.(exe|com|bat)$", re.I)
# Programs next to DOS games that aren't the game: its installer, setup and
# sound setup, multiplayer and modem setup, DOS extenders and other run-time
# parts, sound and video drivers' tools, DOS's own tools, unpackers, viewers
# for the documents, order forms, editors, cheats, launchers for Windows.
NOT_GAME = re.compile(
    r"(install.*|instal|inst|instalar|hdinst.*|dosinst|.*inst|uninst.*|deinstal|deice|bootdisk|makeboot|"
    r"setup.*|.*setup|setmain|setsound|soundset|sndset.*|setsnd|setmusic|setmidi|setd|setm|sound|"
    r"config.*|cfg|setcfg|sndcfg|soundcfg|modem.*|commit|dwango|"
    r"dos4gw?|dos32a|pmodew?|cwsdpmi|cwsdstub|cwstub|stub32a|go32|emx|rsx|run386|32rtm|rtm|wdosx|hdpmi32|"
    r"dpmild32|univbe|uvconfig|vbetest|s3vbe|m64vbe|ultrinit|ultramid|iwinit|diagnose|sbconfig|ctcm|ctcu|"
    r"midpak|soundrv|mouse|ctmouse|mscdex|shsucdx|smartdrv|himem|emm386|loadfix|setver|mem|choice|jchoice|"
    r"xcopy|deltree|attrib|move|keyb|mode|moslo|"
    r"pkunzip|pkunzjr|pkzip|pkzipfix|pkunpak|pkxarc|pksfx|zip2exe|makesfx|unzip|zip|arj|unarj|lha|lharc|"
    r"lhice|ice|rar|unrar|zoo|arc|arce|pak|ha|uc2|lzexe|pklite|diet|unp|expand|extract|"
    r"readme.*|read|readit|read_me|help|helpme|.*help|manual|manuel|docs?|info|notes|whatsnew|list|view|"
    r"viewer|hint.*|clue.*|solution|soluce|walkthru|acrodos|acroread|order.*|ordform|register|registr|"
    r"vendor|vendinfo|dealer|catalog.*|ads?|advert|promo|preview.*|bbs|file_id|"
    r"editor|mapedit|levedit|editart|kextract|kgroup|deu|cheat.*|trainer|autorun|gogwrap|dosbox.*|q95)$", re.I)
SETUP_PROGRAM = re.compile(r"(setup|setmain|setsound|soundset|sndset.*|setsnd|setmusic|setmidi|setd|setm|sound|"
                           r"config|configur|cfg|setcfg|sndcfg|soundcfg|.*setup|install)$", re.I)
INSTALLER = re.compile(r"(instal.*|inst|hdinst.*|dosinst|.*inst|deice)$", re.I)
GENERIC_START = {"start", "go", "run", "runme", "play", "game", "main", "menu", "loader", "startup", "begin",
                 "launch", "!start", "-start-"}
ENGINE_LAUNCHERS = {"sierra", "koei", "dinamic", "ere"}
LOW = re.compile(r"(intro|ending|credits?|movie|playflc|demo|.*demo)$", re.I)
# Launchers of a variant of the game: last among those that start it.
VARIANT = re.compile(r"(nosound|nosnd|nomusic|safe|safemode|lowmem|noems|noxms|ipx|net|netgame|serial|modem|"
                     r"null|nojoy|debug|loadgame|loadsave)$", re.I)
# Commands only DOSBox has: a batch file using them fails in DOS.
DOSBOX_ONLY = re.compile(r"^\s*@?\s*(imgmount|mount|config\s+-|loadfix\s+-f)\b", re.I)


def windows_program(path):
    """True for a Windows (NE or PE) program, such as DOSBox or a launcher:
    its DOS stub only says it needs Windows."""
    head = read_head(path, 64)
    if len(head) < 64 or head[:2] != b"MZ" or struct.unpack_from("<H", head, 0x18)[0] < 0x40:
        return False
    at = struct.unpack_from("<I", head, 0x3C)[0]
    try:
        with open(path, "rb") as f:
            f.seek(at)
            sig = f.read(4)
    except (OSError, OverflowError, ValueError):
        return False
    return sig == b"PE\0\0" or sig[:2] == b"NE"


def windows_header(head):
    """The same test on a program's first bytes (from a CD image)."""
    if len(head) < 64 or head[:2] != b"MZ" or struct.unpack_from("<H", head, 0x18)[0] < 0x40:
        return False
    at = struct.unpack_from("<I", head, 0x3C)[0]
    return head[at:at + 4] == b"PE\0\0" or head[at:at + 2] == b"NE"


def extended_program(path):
    """A 32-bit DOS-extended program (LE/LX, or a DOS extender's name in it):
    the game's engine, usually."""
    head = read_head(path, 65536)
    if head[:2] == b"MZ" and len(head) >= 64:
        at = struct.unpack_from("<I", head, 0x3C)[0]
        if head[at:at + 2] in (b"LE", b"LX"):
            return True
    return any(s in head for s in (b"DOS/4G", b"CWSDPMI", b"PMODE/W", b"DOS/32A", b"go32"))


def self_extractor(path):
    """A self-extracting archive (PKSFX, LHA's, ARJ's, RAR's)."""
    if not path.lower().endswith(".exe") or not regular(path):
        return False
    head = read_head(path, 65536)
    return zipfile.is_zipfile(path) or b"Rar!\x1a\x07" in head or re.search(rb"-lh[0-7d]-", head) is not None \
        or b"aRJsfX" in head


def read_text(path, size=8192):
    return read_head(path, size).decode("latin-1")


def dos_program(path):
    """A program DOS can run: not a Windows one, and not a batch file that
    starts DOSBox or uses Windows' cmd.exe."""
    if path.lower().endswith(".bat"):
        return not re.search(r"dosbox|%~|\bstart\s+\"|\bcd\s+/d\b", read_text(path), re.I)
    return not windows_program(path)


def candidates(folder, names):
    """A folder's programs that may start the game, and why each other one
    isn't (installer, setup, helper, DOSBox batch file, self-extractor)."""
    lower = {n.lower() for n in names}
    games, why = [], {}
    for n in names:
        stem = os.path.splitext(n)[0].lower()
        p = os.path.join(folder, n)
        if NOT_GAME.fullmatch(stem):
            why[n] = "installer" if INSTALLER.fullmatch(stem) else "setup" if SETUP_PROGRAM.fullmatch(stem) \
                else "not the game"
        elif (stem == "dm" and any(x.startswith("doom") for x in lower)) or \
                (stem == "build" and lower & {"duke3d.exe", "blood.exe", "sw.exe"}):
            why[n] = "not the game"  # Doom's DeathManager, the Build engine's editor
        elif n.lower().endswith(".bat") and dosbox_batch(p, folder):
            why[n] = "a batch file for DOSBox (it uses %s)" % dosbox_batch(p, folder).upper()
        elif self_extractor(p):
            why[n] = "a self-extracting archive"
        else:
            games.append(n)
    return games, why


def find_programs(top, exclude=()):
    """The folder under top, nearest it, holding programs that may start the
    game (installers and helpers alone don't count: the search goes deeper,
    and a DOS folder before WIN ones), with all its DOS programs; else the
    nearest one with any (an installer, say). (folder, [programs])."""
    level, fallback = [top], (None, [])
    for _ in range(5):
        for d in sorted(level, key=lambda d: bool(re.match(r"win", os.path.basename(d), re.I))):
            try:
                names = sorted(n for n in os.listdir(d) if PROGRAM.search(n) and os.path.isfile(os.path.join(d, n))
                               and os.path.realpath(os.path.join(d, n)) not in exclude
                               and dos_program(os.path.join(d, n)))
            except OSError:
                continue
            if names and candidates(d, names)[0]:
                return d, names
            if names and fallback[0] is None:
                fallback = (d, names)
        level = [os.path.join(d, n) for d in level for n in sorted(os.listdir(d))
                 if os.path.isdir(os.path.join(d, n)) and n not in JUNK_DIRS and
                 os.path.realpath(os.path.join(d, n)) not in exclude]
    return fallback


def initials(title):
    words = [w for w in re.findall(r"[a-z0-9]+", title.lower()) if w not in ("the", "of", "and", "a", "an")]
    return "".join(w[0] for w in words if not w.isdigit()) + "".join(w for w in words if w.isdigit())


def rank_programs(folder, games, hints, title):
    """The game's programs, best first, each with its score and reasons: a
    batch file that runs one of the others (the game's own launcher), a
    launcher's name (START, PLAY...), Sierra's launcher, a name like the
    game's (prefix, title initials, a title word), a 32-bit DOS-extended
    program, the VGA build and the English one; intros and demos last; then
    the biggest."""
    stems = {os.path.splitext(p)[0].lower() for p in games}
    batches = sum(p.lower().endswith(".bat") for p in games)
    keys = [re.sub(r"[^a-z0-9]", "", h.lower()) for h in hints if h]
    words = [w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) >= 3 and w not in ("the", "and")]
    inits = initials(title)
    ranked = []
    for p in games:
        stem = os.path.splitext(p)[0].lower()
        key = re.sub(r"[^a-z0-9]", "", stem)
        path = os.path.join(folder, p)
        score, why = 0, []
        if VARIANT.fullmatch(stem):
            score, why = score - 50, why + ["a variant (no sound, network...)"]
        elif p.lower().endswith(".bat"):
            text = read_text(path)
            runs = [s for s in stems if s != stem and
                    re.search(r"(^|[\s\\/:@])%s(\.(exe|com|bat))?(\s|$)" % re.escape(s), text, re.I | re.M)]
            if runs and (stem in GENERIC_START or batches == 1 or
                         max((len(os.path.commonprefix([key, k])) for k in keys), default=0) >= 3):
                score, why = score + 100, why + ["a batch file that starts %s" % runs[0].upper()]
        if stem in GENERIC_START:
            score, why = score + 60, why + ["a launcher's name"]
        if stem in ENGINE_LAUNCHERS:
            score, why = score + 50, why + ["the engine's launcher"]
        prefix = max((len(os.path.commonprefix([key, k])) for k in keys), default=0)
        if key and (prefix >= 3 or (prefix == len(key) and len(key) >= 2)):
            score, why = score + 40, why + ["named like the game"]
        elif len(key) >= 2 and len(inits) >= 2 and (key == inits or inits.startswith(key) or key.startswith(inits)):
            score, why = score + 35, why + ["the title's initials"]
        elif any(w in key for w in words):
            score, why = score + 30, why + ["a word of the title"]
        if p.lower().endswith(".exe") and extended_program(path):
            score, why = score + 15, why + ["a 32-bit DOS-extended program"]
        if re.search(r"vga|mcga", key):
            score, why = score + 5, why + ["the VGA build"]
        elif re.search(r"^(ega|cga|tga|tan|herc)|(ega|cga|tga|herc)$", key):
            score -= 4
        if re.search(r"^(eng|english|us)$|eng$", key):
            score, why = score + 3, why + ["the English one"]
        elif re.search(r"deutsch|german|francais|french|espanol|spanish|italiano", key):
            score -= 10
        if LOW.fullmatch(stem):
            score, why = score - 40, why + ["an intro or demo"]
        ranked.append((score, os.path.getsize(path), p, why or ["the only one" if len(games) == 1 else "the biggest"]))
    ranked.sort(key=lambda r: (r[0], r[1]), reverse=True)
    return [(p, s, why) for s, _, p, why in ranked]


def find_program(folder, name):
    """A program in folder by its name, with or without extension (DOS's
    order: .COM, .EXE, .BAT); None."""
    p = find_ci(folder, name) if os.path.splitext(name)[1] else next(
        (q for q in (find_ci(folder, name + e) for e in (".COM", ".EXE", ".BAT")) if q), None)
    return p if p and regular(p) else None


def resolve_run(folder, cmd, batch=None):
    """--run's command as ([words before the program], program, [its
    arguments]): the program as named in folder (GAME gives GAME.EXE, as DOS
    would pick; a name with spaces is the longest run of words naming a
    file; after LOADFIX, CALL or LH, it is the second word). A command whose
    program isn't there is kept as given (DOS's own commands), except one
    that would start the batch file being written (DOS would find
    C:\\BATCH.BAT on the PATH, again and again)."""
    words = cmd.split()
    at = 1 if len(words) > 1 and words[0].lower() in ("loadfix", "call", "lh", "loadhigh") else 0
    for end in range(len(words), at, -1):
        found = find_program(folder, " ".join(words[at:end]).replace("\\", "/"))
        if found:
            return words[:at], os.path.relpath(found, folder), words[end:]
    prog = words[at]
    if batch and os.path.splitext(prog)[0].lower() == batch.lower():
        sys.exit("--run %s: there is no %s in the game's folder, and DOS would start C:\\%s.BAT itself "
                 "instead, again and again." % (cmd, prog, batch))
    warn("--run %s: %s isn't a program in the game's folder; written as given." % (cmd, prog))
    return words[:at], prog, words[at + 1:]


# ---- names ---------------------------------------------------------------------

def game_name(path):
    """A game's name from its archive's or folder's name: "Commander Keen 4
    (1991)(id Software) [!].zip" gives "Commander Keen 4"."""
    name = os.path.basename(path.rstrip("/\\"))
    if os.path.isfile(path):
        name = re.sub(r"(\.tar)?\.[A-Za-z0-9]{1,4}$", "", name)
    name = re.sub(r"\s*[(\[][^)\]]*[)\]]", "", name).replace("_", " ")
    name = re.sub(r"\b(dos|en|eng|rip|iso[ -]version|full)\b\s*$", "", name, flags=re.I)
    return re.sub(r"\s+", " ", name).strip(" -") or "Game"


def short_name(name):
    return re.sub(r"[^A-Z0-9_]", "", name.upper())[:8] or "GAME"


# Folder names too common to name a game's folder on C:.
GENERIC_DIRS = re.compile(r"(game|games|dos|bin|data|files|disk\d*|disc\d*|cd\d*|install|exe|program|"
                          r"prog|run|play|main|new folder)$", re.I)
DOS_DIR = re.compile(r"[A-Za-z0-9!#$%&'()\-@^_`{}~]{1,8}$")
BAT_MARK = "rem gog_import.py: "  # the batch file's second line names the game it starts


def bat_owner(text):
    """The game a batch file this importer wrote starts (its mark line), or
    None for another batch file."""
    m = re.search(r"^%s(.*)$" % re.escape(BAT_MARK), text, re.M | re.I)
    return m.group(1).strip() if m else None


def free_name(target, base, game):
    """base, or base with a digit, whose batch file on the image is unused or
    starts this game: games whose names begin alike (Wing Commander, Wing
    Commander II) get names of their own."""
    for n in range(10):
        short = base if n == 0 else base[:7] + str(n)
        r = mtools("mtype", "-i", target, "::/%s.BAT" % short, check=False)
        if r.returncode or (bat_owner(r.stdout) or "\0").casefold() == (game or "").strip().casefold():
            return short
    sys.exit("C:\\%s.BAT and nine more names like it are taken: give --batch and --dir." % base)


def dir_owners(target):
    """{folder on C: (upper case): game} from the batch files this importer
    wrote at C:\\ (their mark line, and the C: folder they go into)."""
    owners = {}
    listing = mtools("mdir", "-b", "-i", target, "::/*.BAT", check=False).stdout.split()
    for path in listing:
        text = mtools("mtype", "-i", target, path, check=False).stdout
        game = bat_owner(text)
        if game is None:
            continue
        here, first, drive = [], None, "C"
        for line in text.splitlines():  # "cd \X", or "cd \" then "cd X", while on C:
            line = line.strip()
            if re.fullmatch(r"[A-Za-z]:", line):
                drive = line[0].upper()
            cd = re.match(r"cd\s+(\S.*)$", line, re.I)
            if cd and drive == "C":
                arg = cd.group(1).strip()
                parts = [x for x in arg.split("\\") if x and x != "."]
                here = parts if arg.startswith("\\") else here + parts
                first = first or (here[0] if here else None)
        if first:
            owners.setdefault(first.upper(), game)
    return owners


def same_files(target, d, src):
    """The folder ::/d holds what src holds (its names, any case): the same
    game, imported before under another name (an older importer's, or one
    taken from another source's name)."""
    if not src or not os.path.isdir(src):
        return False
    want = {n.lower() for n in os.listdir(src) if n not in JUNK_DIRS | JUNK_FILES}
    there = {os.path.basename(p.rstrip("/")).lower()
             for p in mtools("mdir", "-b", "-i", target, "::/" + d, check=False).stdout.split("\n") if p.strip()}
    return bool(want) and want <= there


def free_dir(target, base, game, owners, src=None):
    """base, or base with a digit, for the game's folder on C:: one that
    isn't there, or that this game has (imported before: its batch file
    names it, or the folder holds what src holds)."""
    for n in range(10):
        d = base if n == 0 else base[:7] + str(n)
        there = mtools("mdir", "-b", "-i", target, "::/" + d, check=False).returncode == 0
        owner = owners.get(d.upper())
        if not there or (owner or "").casefold() == (game or "").strip().casefold() or \
                (owner in (None, "") and same_files(target, d, src)):
            return d
    sys.exit("C:\\%s and nine more folders like it are taken: give --dir." % base)


def write_batch(target, bat, name, commands):
    """C:\\BAT: the game's commands, run on C: (the image's drive), in DOS's
    code page; returns its lines after "@echo off"."""
    lines = ["@echo off", "c:"] + commands[1:] if commands and commands[0].strip().lower() == "c:" else \
        ["@echo off", "c:"] + commands
    text = "\n".join(lines[:1] + [BAT_MARK + (name or "")] + lines[1:]) + "\n"
    try:
        data = text.replace("\n", "\r\n").encode("cp437")
    except UnicodeEncodeError:
        data = text.replace("\n", "\r\n").encode("cp437", "replace")
        warn("The batch file names something DOS's code page can't hold; it may not work: %s" % " | ".join(
            lines[2:]))
    with tempfile.NamedTemporaryFile("wb", suffix=".bat", delete=False) as f:
        f.write(data)
    try:
        mtools("mcopy", "-o", "-i", target, f.name, "::/" + bat)
    finally:
        os.unlink(f.name)
    return lines[1:]


def dos_path(target, path):
    """The name DOS sees for ::path (its 8.3 short name, for a long one)."""
    try:  # it prints the short name in DOS's code page, whatever the locale
        r = subprocess.run(["mshortname", "-i", target, "::" + path], capture_output=True, env=MTOOLS_ENV,
                           stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return path
    out = r.stdout.decode("cp437").strip()
    return out[2:] if r.returncode == 0 and out.startswith("::") else path


# ---- sound ---------------------------------------------------------------------

SOUND_CONFIG = re.compile(r"(sound\.(cfg|ini)|setup\.(cfg|ini)|config\.ini|mdi\.ini|dig\.ini|resource\.cfg|"
                          r"hmiset\.cfg|imuse\.ini|default\.cfg|soundrv\.com|setup\.spd|uw\.cfg)$", re.I)


def sound_setup(folder, programs, target, dest):
    """What the game's sound needs: an SB16 folder of setup files (archive.org
    copies ship one) is copied over the game; setup files already there say
    it was set up (for DOSBox's Sound Blaster 16, which the VM has); else its
    setup program is named. Returns the setup program to run first, or None."""
    sub = next((os.path.join(folder, n) for n in os.listdir(folder)
                if n.lower() in ("sb16", "sb", "sbpro") and os.path.isdir(os.path.join(folder, n))), None)
    if sub:
        files = [n for n in os.listdir(sub) if os.path.isfile(os.path.join(sub, n))]
        if files and all(find_ci(folder, n) for n in files):
            for n in files:
                mtools("mcopy", "-o", "-i", target, os.path.join(sub, n), "::%s/%s" % (dest, find_ci(
                    folder, n)[len(folder) + 1:]))
            say("Sound: the game's %s folder holds its setup for that card, which is now in place (the VM has "
                "a Sound Blaster 16)." % os.path.basename(sub))
            return None
    if any(os.path.isdir(os.path.join(folder, n)) and n.lower() == "ultrasnd" for n in os.listdir(folder)):
        warn("Sound: the game may be set up for a Gravis Ultrasound (it has an ULTRASND folder); the VM has a "
             "Sound Blaster 16: if there is no sound, run its setup and pick Sound Blaster.")
    setups = sorted((p for p in programs if SETUP_PROGRAM.fullmatch(os.path.splitext(p)[0])),
                    key=lambda p: p.lower().startswith("instal"))
    configured = [n for n in os.listdir(folder) if SOUND_CONFIG.fullmatch(n) or n.lower() in {
        os.path.splitext(p)[0].lower() + ".cfg" for p in programs}]
    if configured:
        say("Sound: it was set up already (%s); copies from GOG, eXoDOS and archive.org are set for DOSBox's "
            "Sound Blaster 16 (220, IRQ 7, DMA 1, high DMA 5), as the VM's.%s" % (
                ", ".join(sorted(configured)[:3]),
                " If there is no sound, type CD %s, then %s (setup programs keep their settings in the folder "
                "they run in)." % (dest.replace("/", "\\"), setups[0]) if setups else ""))
        return None
    return setups[0] if setups else None


# ---- the imports ---------------------------------------------------------------

def report_disc(image):
    """Say what's on a CD to run (its installer, usually) from D:; returns
    the DOS command for it, or None."""
    if os.path.isdir(image):
        root = cd_root(image)
        if root:
            say("On the CD: %s." % ", ".join(root[1][:16]))
        return None
    try:
        fs = CdFs(data_track(image))
    except (TypeError, ValueError, OSError, struct.error, IndexError):
        return None
    try:
        entries = fs.listdir()
        names = [e[0] + ("\\" if e[1] else "") for e in entries]
        say("On the CD%s: %s." % (" (%s)" % fs.label if fs.label else "", ", ".join(names[:16]) +
                                  (", ..." if len(names) > 16 else "") if names else "nothing"))
        REPORT["cd_files"] = names

        def dos_programs(entries):
            return [e for e in entries if not e[1] and PROGRAM.search(e[0]) and
                    not (e[0].upper().endswith(".EXE") and windows_header(fs.head(e, 4096)))]

        def pick(entries):
            progs = dos_programs(entries)
            return next((e for e in progs if INSTALLER.fullmatch(os.path.splitext(e[0])[0])), None) or \
                next((e for e in progs if re.fullmatch(r"setup", os.path.splitext(e[0])[0], re.I)), None)

        found, where = pick(entries), ""
        if found is None:  # a DOS\ or INSTALL\ folder, or a game's *INST\ folder
            for e in entries:
                if e[1] and re.fullmatch(r"dos|install|.*inst", e[0], re.I):
                    found = pick(fs.listdir(e))
                    if found:
                        where = e[0] + "\\"
                        break
        if found:
            prog = os.path.splitext(found[0])[0]
            say("Install the game in the VM: at the C:\\> prompt type D: and then %s%s, and install it to C:. "
                "Pick Sound Blaster 16 (or a Sound Blaster) at port 220, IRQ 7, DMA 1, high DMA 5 if it asks."
                % ("CD " + where.rstrip("\\") + " and " if where else "", prog))
            return where + prog
        progs = dos_programs(entries)
        if progs:
            say("Programs on the CD: %s (run them from D:)." % ", ".join(e[0] for e in progs))
        elif any(PROGRAM.search(e[0]) for e in entries):
            warn("The CD's programs at its top are for Windows; look in its folders for the DOS version.")
        return None
    except (OSError, struct.error, IndexError):
        return None
    finally:
        fs.close()


def import_dosbox(a, root, name, settings, mounts, commands, target, cfg, owners):
    """A game DOSBox runs: copy what DOSBox mounted as C:, write the batch
    file from the config's commands, attach its CDs, report its settings."""
    game_root = mounts["C"][1]
    REPORT.update(kind="dosbox", name=name)

    # Where the game runs: the folder its commands "cd" into while C: is the
    # current drive, which keeps its own relative paths working. Only that
    # folder is copied (GOG's extras next to it stay behind), to the same
    # place on the image. A game run from C:'s root gets a folder of its own
    # (or --dir); so does one whose folder name another game has on C:, and
    # its folder goes inside that one (C:\NEW\TOP), its commands' "cd \"
    # made to start there.
    on_c, drive = [], "Z"  # for each command, whether C: is the current drive then (DOSBox starts on Z:)
    for c in commands:
        if re.fullmatch(r"[A-Za-z]:", c.strip()):
            drive = c.strip()[0].upper()
        on_c.append(drive == "C")
    cds = [c.split(None, 1)[1].strip().replace("/", "\\") for c, here in zip(commands, on_c)
           if here and c.lower().startswith("cd ")]
    top = cds[0].lstrip("\\").split("\\")[0] if cds and cds[0].strip("\\") else ""
    own = find_ci(game_root, top) if top else game_root  # the game's own folder
    if top and own is None:
        sys.exit("The game's commands go into %s, which isn't in %s." % (top, game_root))
    if not top and game_root != root and inside(root, game_root):
        sys.exit("The config mounts %s as C:, a folder above the game's own, and its commands don't go into a "
                 "folder in it: copying it all would copy more than the game. Give --dir and --run, or import the "
                 "game's folder." % game_root)
    name = name or os.path.basename(own)
    REPORT["name"] = name
    short = free_name(target, short_name(name or "GAME"), name)
    # DOSBox's built-ins (INTRO, MIXER...) that aren't the game's own programs
    folders = [game_root, own] + [find_ci(game_root, c.lstrip("\\")) for c in cds]
    dropped = [c for c in commands if dosbox_builtin(c, folders)]
    keep = [k for k, c in enumerate(commands) if c not in dropped]
    commands, on_c = [commands[k] for k in keep], [on_c[k] for k in keep]
    top_dos = os.path.basename(own).upper() if top else ""
    taken = top and not a.dir and free_dir(target, top_dos, name, owners, own) != top_dos
    if a.dir or not top or taken:
        game_dir = (a.dir or free_dir(target, short, name, owners, own if not top else None)).strip("\\/")
        if taken:
            warn("C:\\%s holds another game: this one goes into C:\\%s\\%s." % (top_dos, game_dir, top_dos))
        src, dest = (own, game_dir + "/" + top_dos) if top else (game_root, game_dir)
        base = "\\" + game_dir.replace("/", "\\")

        def rebase(c, here):  # "cd \X" on C: goes into C:\GAME_DIR\X
            if not (here and re.match(r"cd(\s|\\)", c, re.I)):
                return c
            arg = c[2:].strip()
            if re.match(r"[cC]:", arg):
                arg = arg[2:]
            return "cd " + (base + ("" if arg.strip("\\") == "" else "\\" + arg.strip("\\"))
                            if arg.startswith("\\") else arg)
        commands = ["cd " + base] + [rebase(c, here) for c, here in zip(commands, on_c)]
        run_dir = base + ("\\" + top_dos if top else "")
    else:
        src, game_dir = own, top_dos
        dest = game_dir
        run_dir = "\\" + game_dir
        if not cds[0].startswith("\\"):
            commands = ["cd \\"] + commands  # its "cd" was from C:'s root
    if a.run:
        pre, prog, post = resolve_run(own, a.run, short)
        commands = ["cd " + run_dir, " ".join(pre + [prog.replace(os.sep, "\\")] + post)]
    images = [p for m in mounts.values() if m[0] == "image" for p in m[1]]
    images += [f for p in images for f in companions(p)]  # and the tracks their cue sheets list
    images += [m[1] for m in mounts.values() if m[0] == "cdfolder"]
    files, size = copy_tree(src, a.image, a.offset, "/" + dest, images, owners=owners, game=name)
    on_cd = {os.path.realpath(p) for p in images}  # attached as the CD, not left behind
    others = [n for n in os.listdir(game_root) if os.path.join(game_root, n) != src and
              not (n.lower() in SKIP_DIRS or SKIP_FILES.match(n)) and
              os.path.realpath(os.path.join(game_root, n)) not in on_cd]

    bat = (a.batch or short) + ".BAT"
    lines = write_batch(target, bat, name, commands)
    REPORT.update(dir="C:\\" + game_dir.replace("/", "\\"), batch="C:\\" + bat, runs=lines,
                  run_source="given (--run)" if a.run else "the DOSBox configuration", next=["C:\\" + bat])
    say("%s: %d files (%d KB) copied to C:\\%s; run C:\\%s." % (
        name or "Game", files, size // 1024, game_dir.replace("/", "\\"), bat))
    say("%s runs: %s" % (bat, " | ".join(lines)))
    if others and src != game_root:
        say("Not copied (next to the game in GOG's folder): %s" % ", ".join(sorted(others)[:12]))
    called = [c.split()[-1] for c in commands if re.match(r"call\s+\S+$", c, re.I)]
    for c in called + [c for c in commands if re.fullmatch(r"\S+\.bat", c.strip(), re.I)]:
        p = find_ci(src, c if c.lower().endswith(".bat") else c + ".bat")
        if p and DOSBOX_ONLY.search(read_text(p)):
            warn("%s uses DOSBox's own commands (mount, imgmount...), which DOS doesn't have: start the game's "
                 "program yourself, or import again with --run." % os.path.basename(p))
    discs = []  # (drive letter, image or folder, volume label, music tracks)
    for drive, (kind, what, *label) in sorted(mounts.items()):
        if drive == "C":
            continue
        if kind == "image":
            discs += [(drive, p, None, []) for p in what]
        elif kind == "cdfolder":
            discs.append((drive, what, label[0], []))
        elif kind == "disk":
            warn("Drive %s: is the disk image %s in DOSBox: attach it to the VM in 86Box's settings if the "
                 "game needs it." % (drive, ", ".join(what)))
        elif kind == "dir":
            warn("Drive %s: is the folder %s in DOSBox; copy it too if the game needs it." % (drive, what))
        else:
            warn("Drive %s: is the folder %s in DOSBox, which isn't there." % (drive, what))
    attach_cds(discs, cfg, short, name or short)
    # make_game_vm.sh's machine has DOSBox's defaults: a Sound Blaster 16
    # at 220, IRQ 7, DMA 1, high DMA 5, and EMS (JemmEx).
    sb = settings.get("sblaster", {})
    sbtype = sb.get("sbtype", "sb16").lower()
    card = (sb.get("sbbase", "220"), sb.get("irq", "7"), sb.get("dma", "1"), sb.get("hdma", "5"))
    n = 4 if sbtype == "sb16" else 3  # only the SB16 has a high DMA
    if sbtype != "none" and card[:n] != ("220", "7", "1", "5")[:n]:
        warn("Sound in DOSBox: %s at port %s, IRQ %s, DMA %s%s, unlike the game VM's Sound Blaster 16 (220, "
             "IRQ 7, DMA 1, high DMA 5): run the game's setup to pick the VM's settings." % (
                 sbtype, card[0], card[1], card[2], ", high DMA %s" % card[3] if sbtype == "sb16" else ""))
    if settings.get("gus", {}).get("gus", "false").lower() in ("true", "1"):
        warn("DOSBox also emulates a Gravis Ultrasound, which the VM lacks: pick Sound Blaster in the game's "
             "setup if it uses the GUS.")
    if settings.get("dos", {}).get("ems", "true").lower() in ("false", "0"):
        warn("DOSBox runs this game without EMS, which the game VM's JemmEx provides. Most games don't mind; "
             "if this one fails, change the line in the VM's A:\\FDCONFIG.SYS to DEVICE=JEMMEX.EXE NOEMS "
             "(SETUP-LINUX.md, \"Change the boot floppy\").")
    mem = settings.get("dosbox", {}).get("memsize", "")
    have = vm_memory_mb(cfg)
    if mem.isdigit() and have and int(mem) > have:
        warn("DOSBox gives this game %s MB of memory, and the VM has %d MB: if it runs short, raise Memory in "
             "86Box's Settings, Machine." % (mem, have))
    cycles = settings.get("cpu", {}).get("cycles", "").lower().split()
    fixed = cycles[1] if cycles[:1] == ["fixed"] and len(cycles) > 1 else cycles[0] if cycles else ""
    if fixed.isdigit():
        say("In DOSBox it ran at a fixed speed (cycles=%s), likely slower than the VM's CPU: if the game runs too "
            "fast, pick a slower CPU in 86Box's Settings, Machine." % fixed)


def import_cds(discs, name, cfg):
    """A game that is only CDs: put them in the drive and say what to run."""
    REPORT.update(kind="cd", name=name)
    attach_cds([(None, p, None, tracks) for p, tracks in discs], cfg, short_name(name), name)
    step = report_disc(discs[0][0])
    REPORT["next"] = ["D:", step] if step else ["D:", "DIR"]


def leads_to_programs(folder, parts):
    """folder, or one in it, holds a DOS program (not a CD's file)."""
    for dp, dns, fs in os.walk(folder):
        dns[:] = [d for d in dns if d not in JUNK_DIRS]
        if any(PROGRAM.search(f) and regular(os.path.join(dp, f)) and
               os.path.realpath(os.path.join(dp, f)) not in parts for f in fs):
            return True
    return False


def only_extras(folder):
    """A folder of nothing but notes, scans and manuals."""
    files = [f for _, dns, fs in os.walk(folder) for f in fs if f not in JUNK_FILES]
    return bool(files) and all(EXTRAS.search(f) for f in files)


def rank_installers(folder, programs, why):
    """The installers, the one to run first: a batch file that runs one of
    the others (with the arguments it needs), then INSTALL, then other
    installers, self-extracting archives last."""
    inst = [p for p in programs if why.get(p) in ("installer", "a self-extracting archive")]
    stems = {os.path.splitext(p)[0].lower() for p in inst}

    def key(p):
        stem = os.path.splitext(p)[0].lower()
        drives = p.lower().endswith(".bat") and any(
            re.search(r"(^|[\s\\/:@])%s(\.(exe|com|bat))?(\s|$)" % re.escape(x), read_text(os.path.join(folder, p)),
                      re.I | re.M) for x in stems if x != stem)
        return (0 if drives else 1 if stem.startswith("instal") else 2 if why[p] == "installer" else 3, p.lower())
    return sorted(inst, key=key)


def config_program(commands, folder):
    """The program a DOSBox config's commands start that is in folder (DOS's
    order: .COM, .EXE, .BAT), as ([], program, [its arguments]); None."""
    for c in commands:
        words = c.split()
        if words and words[0].lower() == "call":
            words = words[1:]
        if not words or re.fullmatch(r"[A-Za-z]:|cd(\s.*)?", words[0], re.I):
            continue
        p = find_program(folder, words[0])
        if p:
            return [], os.path.basename(p), words[1:]
    return None


def import_plain(a, top, name, target, cfg, owners, hint=()):
    """A DOS game without a DOSBox configuration (a zip, 7z or a folder):
    copy its folder (the one past lone folders and download sites' notes),
    CD images left out (they go into the CD-ROM drive) and floppy images
    unpacked into DISK1, DISK2..., and write a batch file that runs the
    program that starts it."""
    REPORT.update(kind="plain", name=name)
    discs, parts, unread = find_cds(top)
    for p in unread:
        warn("%s is a CD image 86Box doesn't read: convert it to an ISO or a cue sheet (BIN/CUE), and give "
             "the importer that." % os.path.relpath(p, top))
    floppies = sorted((os.path.join(dp, f) for dp, dns, fs in os.walk(top) for f in fs
                       if os.path.realpath(os.path.join(dp, f)) not in parts and floppy_image(os.path.join(dp, f))),
                      key=disc_order)
    parts |= {os.path.realpath(p) for p in floppies}
    disk_dirs = []
    if floppies:  # unpacked next to the VM, not into the user's folder
        TEMP_DIRS.append(tempfile.mkdtemp(prefix=".import-", dir=os.path.dirname(os.path.abspath(a.image))))
        disk_dirs = unpack_floppies(floppies, TEMP_DIRS[-1])
        say("Floppy images %s: their files go into DISK1%s." % (
            ", ".join(os.path.basename(p) for p in floppies),
            "..DISK%d" % len(disk_dirs) if len(disk_dirs) > 1 else ""))
    # The game's folder: past folders holding one folder that leads to
    # programs, beside notes and folders of scans.
    root, extras = top, []
    while True:
        names = [n for n in os.listdir(root) if n not in JUNK_DIRS | JUNK_FILES]
        dirs = [n for n in names if os.path.isdir(os.path.join(root, n))]
        loose = [n for n in names if n not in dirs and os.path.realpath(os.path.join(root, n)) not in parts]
        game_dirs = [n for n in dirs if leads_to_programs(os.path.join(root, n), parts)]
        rest = [n for n in dirs if n not in game_dirs and not all(
            os.path.realpath(os.path.join(dp, f)) in parts for dp, _, fs in os.walk(os.path.join(root, n)) for f in fs)]
        if len(game_dirs) == 1 and all(EXTRAS.search(n) for n in loose) and \
                all(only_extras(os.path.join(root, n)) for n in rest) and \
                not any(PROGRAM.search(n) for n in loose):
            extras += [os.path.relpath(os.path.join(root, n), top) for n in loose + rest]
            root = os.path.join(root, game_dirs[0])
            continue
        break
    folder, programs = find_programs(root, parts)
    disk = None  # the floppy whose program is run
    if disk_dirs and (folder is None or not candidates(folder, programs)[0]):
        for want in ("installer", "game"):  # install floppies: their installer first
            for k, d in enumerate(disk_dirs):
                f, ps = find_programs(d)
                if f and (rank_installers(f, ps, candidates(f, ps)[1]) if want == "installer" else
                          candidates(f, ps)[0]):
                    folder, programs, disk = f, ps, k
                    break
            if disk is not None:
                break
    if folder is None:
        if discs:
            return import_cds(discs, name, cfg)
        sys.exit("No DOS program (EXE, COM or BAT) or CD image in %s: is this a DOS game?" % a.source)
    own = os.path.basename(root.rstrip("/"))
    if a.dir:
        dest = a.dir
    else:
        # The game's own folder name when DOS can have it and it isn't a
        # generic one (GAME, DOS...); else one from the game's name.
        usable = DOS_DIR.match(own) and not GENERIC_DIRS.match(own) and (root != top or os.path.isdir(a.source))
        dest = free_dir(target, own.upper() if usable else short_name(name), name, owners, root)
    short = a.batch or free_name(
        target, short_name(dest.split("/")[-1]) if re.fullmatch(r"[A-Za-z0-9_]{1,8}", dest.split("/")[-1])
        else short_name(name), name)
    games, why = candidates(folder, programs)
    installers = rank_installers(folder, programs, why)
    hints = [name, os.path.basename(folder) if folder != top or os.path.isdir(a.source) else ""]
    ranked = []
    if a.run:  # start: ([words before], program, [arguments])
        start, how = resolve_run(folder, a.run, short), "given (--run)"
    else:
        ranked = rank_programs(folder, games, hints, name)
        configured = config_program(hint, folder)  # a DOSBox config's program
        start = configured or (([], ranked[0][0], []) if ranked else None)
        how = "the DOSBox configuration" if configured else "guessed: %s" % ", ".join(ranked[0][2]) if ranked \
            else None
        REPORT["candidates"] = [{"program": p, "score": s, "why": w} for p, s, w in ranked[:5]]
    files, size = copy_tree(root, a.image, a.offset, "/" + dest, parts, gog=False)
    for n, d in enumerate(disk_dirs):
        f2, s2 = copy_tree(d, a.image, a.offset, "/%s/DISK%d" % (dest, n + 1), (), gog=False)
        files, size = files + f2, size + s2
    where = "C:\\" + dest.replace("/", "\\")
    # Where the program is on C:, as DOS sees it (8.3 short names for long
    # ones: a batch file can't name "Doom II" or "Keen Dreams.exe").
    base = disk_dirs[disk] if disk is not None else root
    host = "/" + dest + ("/DISK%d" % (disk + 1) if disk is not None else "")
    sub = os.path.relpath(folder, base)
    host_dir = host + ("" if sub == "." else "/" + sub.replace(os.sep, "/"))
    dos_dir = dos_path(target, host_dir)
    run_in = "C:" + dos_dir.replace("/", "\\")

    def dos_command(start):  # the program as DOS sees it on C:
        pre, prog, post = start
        found = find_ci(folder, prog.replace("\\", "/"))
        name = dos_path(target, host_dir + "/" + os.path.relpath(found, folder).replace(os.sep, "/"))[
            len(dos_dir) + 1:].replace("/", "\\") if found else prog
        if not all(SHORT_NAME.match(x) for x in name.split("\\")) or \
                os.path.splitext(name.split("\\")[-1])[0].lower() == short.lower() and not found:
            sys.exit("The batch file can't name %s as DOS would (it has no 8.3 name to use): give --run "
                     "with another program." % prog)
        return " ".join(pre + [name] + post)
    REPORT.update(dir=where, run_source=how)
    if extras:
        REPORT["not_copied"] = extras
    bat = short + ".BAT"
    if start is None and installers:  # the batch file runs the installer
        lines = write_batch(target, bat, name, ["cd " + run_in[2:], dos_command(([], installers[0], []))])
        REPORT.update(batch="C:\\" + bat, runs=lines, run_source="the game's installer", next=["C:\\" + bat])
        say("%s: %d files (%d KB) copied to %s." % (name, files, size // 1024, where))
        say("It is the game's installer: C:\\%s runs it (%s\\%s). Install the game to C:, then start it from "
            "the folder it was installed to." % (bat, run_in, lines[-1]))
    elif start is None:  # a batch file all the same: it marks the folder as this game's for the next import
        advice = "None of its programs looks like the game (%s): import it again with --run PROGRAM." % ", ".join(
            programs)
        write_batch(target, bat, name, ["cd " + run_in[2:], "echo " + advice.replace("(", "").replace(")", "")])
        say("%s: %d files (%d KB) copied to %s." % (name, files, size // 1024, where))
        say(advice)
        REPORT["next"] = []
    else:
        lines = write_batch(target, bat, name, ["cd " + run_in[2:], dos_command(start)])
        REPORT.update(batch="C:\\" + bat, runs=lines, next=["C:\\" + bat])
        say("%s: %d files (%d KB) copied to %s; run C:\\%s." % (name, files, size // 1024, where, bat))
        say("%s runs: %s (%s)" % (bat, " | ".join(lines), how))
        if not a.run:
            rest = [p for p in programs if p.lower() != start[1].lower()]
            close = [r for r in ranked[1:3] if r[1] == ranked[0][1]] if how.startswith("guessed") else []
            if close:
                warn("It could also be %s: run the game once to see, and import again with --run \"PROGRAM\" "
                     "if it's another." % "; ".join("%s (%s)" % (p, ", ".join(w)) for p, _, w in close))
            elif rest:
                say("Other programs there: %s; --run \"PROGRAM\" picks another." % ", ".join(
                    "%s (%s)" % (p, why[p]) if p in why else p for p in rest))
        setup = sound_setup(folder, programs, target, dos_dir)
        if setup:
            say("If the game has no sound, type CD %s, then %s (setup programs keep their settings in the folder "
                "they run in), and pick Sound Blaster 16 (or a Sound Blaster) at port 220, IRQ 7, DMA 1, high "
                "DMA 5." % (run_in[2:], setup))
            REPORT["setup"] = "%s\\%s" % (run_in, setup)
    if extras:
        say("Not copied (outside the game's folder): %s" % ", ".join(sorted(extras)[:12]))
    attach_cds([(None, p, None, tracks) for p, tracks in discs], cfg, short, name)


def main():
    global JSON_OUT
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("source", help="a GOG installer (setup_*.exe, or *.sh for Linux), a zip or 7z archive, a "
                    "DOSBox configuration, a CD image (.iso, .cue, .ccd, .mds, .chd...), or a game's folder")
    ap.add_argument("image", help="the 86Box disk or floppy image (FAT)")
    ap.add_argument("--offset", type=int, help="byte offset of the FAT partition (default: from the "
                    "partition table; 0 without one)")
    ap.add_argument("--dir", default="", help="directory on the image, e.g. GAMES\\KEEN4 (default: the game's "
                    "own, under C:\\)")
    ap.add_argument("--batch", default="", help="name of the batch file written at the image's root, without "
                    ".BAT (default: from the game's name)")
    ap.add_argument("--run", default="", help="the command the batch file runs in the game's folder (default: "
                    "the game's own, from its DOSBox configuration, or the program that looks like the game)")
    ap.add_argument("--json", action="store_true", help="the report as JSON on stdout (the text on stderr)")
    a = ap.parse_args()
    JSON_OUT = a.json
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, on_signal)
    try:
        start(a)
        if REPORT["skipped"]:
            say("Left out: %s%s." % ("; ".join(REPORT["skipped"][:8]), "; ..." if len(REPORT["skipped"]) > 8 else ""))
        if REPORT.get("next"):
            say("Next, in the VM: %s." % " then ".join(REPORT["next"]))
    except SystemExit as e:
        if isinstance(e.code, str):
            REPORT["error"] = e.code
        raise
    finally:
        for tmp in TEMP_DIRS:
            shutil.rmtree(tmp, ignore_errors=True)
        if JSON_OUT:
            print(json.dumps(REPORT, indent=1))


def start(a):
    if not os.path.isfile(a.image):
        sys.exit("No disk image at %s." % a.image)
    if not os.path.exists(a.source):
        sys.exit("No installer, archive, CD image or folder at %s." % a.source)
    if a.batch:  # a DOS name, without .BAT
        a.batch = re.sub(r"\.bat$", "", a.batch, flags=re.I).upper()
        if not DOS_DIR.fullmatch(a.batch):
            sys.exit("--batch %s: a batch file's name has 1 to 8 letters, digits or _-!#$%%&'(){}~^@." % a.batch)
    if a.dir:  # GAMES\KEEN4 or GAMES/KEEN4
        a.dir = "/".join(p for p in re.split(r"[\\/]+", a.dir.upper()) if p)
        if not a.dir or not all(DOS_DIR.fullmatch(p) for p in a.dir.split("/")):
            sys.exit("--dir %s: each folder's name has 1 to 8 letters, digits or _-!#$%%&'(){}~^@." % a.dir)
    vmdir = os.path.dirname(os.path.abspath(a.image))
    running = vm_running(vmdir, a.image)
    if running:
        sys.exit("%s (process %d) is running this VM: close it first. It keeps the disk open, and rewrites "
                 "86box.cfg from memory when it closes. (A CD can also go in through its Media menu.)" % running)
    a.offset = partition_offset(a.image) if a.offset is None else a.offset
    target = "%s@@%d" % (a.image, a.offset)
    mtools("mdir", "-i", target, "::/", fail="mdir failed on %s: %%s\nIs that a FAT image, with the FAT "
           "partition at that byte offset (--offset)? 86Box must not be running on it." % target.replace("%", "%%"))
    cfg = os.path.join(vmdir, "86box.cfg")  # the VM's, when the image is a VM's disk
    for stale in glob.glob(os.path.join(glob.escape(vmdir), ".import-*")):  # left by an import that was killed
        shutil.rmtree(stale, ignore_errors=True)
    run(a, target, vmdir, cfg, dir_owners(target))


def run(a, target, vmdir, cfg, owners):
    global BOUND
    kind = cd_kind(a.source) if os.path.isfile(a.source) else None
    if kind == "unread":
        sys.exit("86Box doesn't read %s images: convert it to an ISO or a cue sheet (BIN/CUE), and give the "
                 "importer that." % os.path.splitext(a.source)[1])
    if kind:
        BOUND = folder = os.path.dirname(os.path.abspath(a.source))  # the files a cue sheet names: next to it
        images = [os.path.join(folder, n) for n in os.listdir(folder) if cd_kind(os.path.join(folder, n)) == "read"]
        tracks = loose_tracks(a.source, images) if os.path.splitext(a.source)[1].lower() in (
            ".iso", ".bin", ".img", ".gog") else []
        # Other discs beside it (each with the files its descriptor names)
        # go in only when the folder is given.
        covered = {os.path.realpath(p) for p in [a.source] + companions(a.source)}
        others = []
        for p in sorted(images, key=lambda p: os.path.splitext(p)[1].lower() not in (".cue", ".ccd", ".mds", ".toc")):
            theirs = {os.path.realpath(q) for q in [p] + companions(p)}
            if not theirs & covered:
                others.append(os.path.basename(p))
            covered |= theirs
        if others:
            warn("Other CD images are next to it: %s. For a game on several discs, give the importer their "
                 "folder instead: disc 1 then goes into the drive and the others into 86Box's Media menu." % (
                     ", ".join(sorted(others)[:4] + (["%d more" % (len(others) - 4)] if len(others) > 4 else []))))
        return import_cds([(a.source, tracks)], game_name(a.source), cfg)

    source, conf_file, unpacked, inner_done, tried = a.source, None, None, False, False
    if os.path.isfile(source) and re.search(r"\.(conf|cfg)$", source, re.I) and is_dosbox_conf(source):
        conf_file, source = os.path.abspath(source), os.path.dirname(os.path.abspath(source))
    outer = None  # what was unpacked before an installer found in it
    while True:
        if unpacked != source:
            outer, top, unpacked = (top if unpacked else None), source, source
            if os.path.isfile(source):
                # Unpack next to the VM: the CD images in it are hard-linked
                # into its cd/ folder from there, and /tmp may be small (a
                # RAM disk).
                try:
                    top = tempfile.mkdtemp(prefix=".import-", dir=vmdir)
                except OSError:
                    top = tempfile.mkdtemp(prefix="gog-")
                TEMP_DIRS.append(top)
                try:
                    unpack(source, top)
                except SystemExit as e:
                    if source == a.source:
                        raise
                    warn("%s isn't an installer innoextract can unpack (%s): importing %s as it is." % (
                        os.path.basename(source), str(e.code).splitlines()[0], os.path.basename(a.source)))
                    source = unpacked = top = outer
        # A config the user named is trusted (it may mount anything, as in
        # DOSBox); what's in a download or folder stays inside it.
        BOUND = None if conf_file else top
        root = os.path.dirname(conf_file) if conf_file else find_game_root(top)
        name, confs, workdir = play_task(root)
        gameinfo = find_ci(root, "gameinfo")  # Linux installers: the name on its first line
        if not name and gameinfo and regular(gameinfo):
            name = read_head(gameinfo, 4096).decode("utf-8", "replace").split("\n")[0].strip()
        if conf_file:
            # GOG's configs mount from DOSBox's working folder (the play
            # task's), not from their own; with its other configs (settings).
            if os.path.realpath(conf_file) not in {os.path.realpath(c) for c in confs}:
                confs, workdir = [conf_file], find_ci(root, "DOSBOX") or root
        elif not confs:  # no play task: one player's configs, settings first, one autoexec
            confs, alternatives = pick_confs(root)
            for alt in alternatives:
                warn("%s runs the game another way; to import it so, give the importer that config." %
                     os.path.basename(alt))
            workdir = find_ci(root, "DOSBOX") or root
        inner = None if confs or tried else gog_installer(top)
        if inner is None:
            if not confs and TEMP_DIRS and not inner_done and unpack_inner(top, os.path.basename(source)):
                inner_done = True
                continue  # look again, in what the inner archives held
            break
        say("%s holds GOG's installer %s: importing the game from that." % (
            os.path.basename(source), os.path.basename(inner)))
        source, tried = inner, True
    if not name:  # not from GOG: a config's folder (eXoDOS's !dos\GAME), or the source's name
        name = game_name(os.path.dirname(conf_file) if conf_file else a.source)
        if re.fullmatch(r"dosbox.*|conf|configs?|!dos", name, re.I) or GENERIC_DIRS.match(name):
            name = ""  # import_dosbox names it after the game's folder
    hint = []
    if confs:
        settings, autoexec = read_conf(confs)
        # Mounts are relative to DOSBox's working folder; other packagers'
        # configs (eXoDOS's) to a folder above the config's: only folders
        # inside what was given, unless a config itself was.
        ups = [os.path.dirname(root), os.path.dirname(os.path.dirname(root)),
               os.path.dirname(os.path.dirname(os.path.dirname(root)))]
        ups = [u for u in ups if conf_file or inside(u, top)]
        mounts, commands = parse_autoexec(autoexec, [workdir, root] + ups, top, trusted=bool(conf_file))
        if "C" in mounts and mounts["C"][0] == "dir" and os.path.isdir(mounts["C"][1]):
            return import_dosbox(a, root, name, settings, mounts, commands, target, cfg, owners)
        if files_like(root, r"goggame-.*\.info"):
            sys.exit("The DOSBox configuration mounts no folder as C: (%s)." % (mounts.get("C"),))
        warn("The DOSBox configuration mounts no folder here as C: (%s): importing the game without it." % (
            mounts.get("C", ("none",))[-1],))
        hint = commands
    import_plain(a, top, name or game_name(a.source), target, cfg, owners, hint)


def pick_confs(root):
    """Without a play task: the DOSBox configs for one player (not a
    client's, server's or setup's), settings-only ones first, then the one
    whose [autoexec] starts the game (a "single" one, or the first); and the
    others that start it another way. Other packagers' names (any .conf or
    .cfg that mounts a drive) when no dosbox*.conf mounts anything."""
    one = [p for p in files_like(root, r"dosbox.*\.conf")
           if not re.search(r"client|server|setup", os.path.basename(p), re.I)]
    if not any(is_dosbox_conf(p) for p in one):
        one += [p for p in files_like(root, r".*\.(conf|cfg)") if p not in one and is_dosbox_conf(p) and
                not re.search(r"client|server|setup", os.path.basename(p), re.I)]
    runs = sorted((p for p in one if has_autoexec(p)), key=lambda p: ("single" not in os.path.basename(p).lower(), p))
    return [p for p in one if p not in runs] + runs[:1], runs[1:]


if __name__ == "__main__":
    main()
