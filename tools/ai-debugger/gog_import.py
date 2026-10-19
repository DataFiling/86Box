#!/usr/bin/env python3
"""Put a GOG DOS game onto an 86Box disk image, to run and debug it there.

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

  gog_import.py setup_game.exe IMAGE [--offset BYTES] [--dir NAME]
  gog_import.py gog_game.sh IMAGE ...            (GOG's Linux installer)
  gog_import.py "C:/GOG Games/Game" IMAGE ...    (an installed game)

--offset: the FAT partition's byte offset in the image; by default the first
FAT partition in a hard disk image's partition table, or 0 for a floppy or
an image without one. --dir: the directory on the image (default: the folder
the game runs in, under C:\\). Needs innoextract for Windows installers, and
mtools.
"""

import argparse
import glob
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile

# GOG's own files around the game, never part of it.
SKIP_DIRS = {"dosbox", "__support", "__redist", "commonappdata", "tmp", "app", "cloud_saves"}
SKIP_FILES = re.compile(r"^(goggame-.*|gog\.ico|gfw_high.*|support\.ico|unins\d+\..*|webcache\.zip|"
                        r"dosbox.*\.conf|launch.*\.(bat|lnk)|eula.*|.*\.lnk)$", re.I)


def unpack(installer, dest):
    """Unpack a GOG installer into dest: a Linux one (a shell script with a
    zip appended) with zipfile, a Windows one with innoextract."""
    if zipfile.is_zipfile(installer):
        with zipfile.ZipFile(installer) as z:
            z.extractall(dest)
        return
    if shutil.which("innoextract") is None:
        sys.exit("innoextract is needed to unpack Windows installers (apt install innoextract), "
                 "or give the installed game's folder instead.")
    r = subprocess.run(["innoextract", "--gog", "--silent", "--output-dir", dest, installer],
                       capture_output=True, text=True)
    if r.returncode:
        v = subprocess.run(["innoextract", "--version"], capture_output=True, text=True).stdout.splitlines()
        sys.exit("innoextract could not unpack %s:\n%s\n\n"
                 "Give it GOG's Windows offline installer (setup_*.exe, with its .bin parts next to it) or "
                 "Linux installer (*.sh), or the folder of the installed game. This innoextract (%s) %s; a "
                 "newer installer needs a newer one (github.com/dscharrer/innoextract)." % (
                     installer, (r.stderr or r.stdout).strip(), v[0] if v else "?",
                     v[1][0].lower() + v[1][1:] if len(v) > 1 else "may be too old"))


def find_game_root(top):
    """The folder under top holding goggame-*.info or the DOSBox configs,
    the shallowest first: top itself (an installed game, a current Windows
    installer), app/ (older Windows installers) or data/noarch/ (Linux
    installers)."""
    level = [top]
    for _ in range(4):
        for d in level:
            if any(re.match(r"(goggame-.*\.info|dosbox.*\.conf)$", n, re.I) for n in os.listdir(d)):
                return d
        level = [os.path.join(d, n) for d in level for n in sorted(os.listdir(d))
                 if os.path.isdir(os.path.join(d, n))]
    return top


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


def play_task(root):
    """(game name, [config paths], DOSBox's working directory) from
    goggame-*.info's primary play task. Paths in the task, and the mounts in
    the configs, are relative to that working directory."""
    for info in sorted(glob.glob(os.path.join(root, "goggame-*.info"))):
        with open(info, encoding="utf-8-sig") as f:
            data = json.load(f)
        tasks = data.get("playTasks", [])
        task = next((t for t in tasks if t.get("isPrimary")), tasks[0] if tasks else None)
        if task is None:
            continue
        workdir = find_ci(root, task.get("workingDir", "")) or root
        confs = []
        for rel in re.findall(r'-conf\s+"([^"]+)"|-conf\s+(\S+)', task.get("arguments", "")):
            p = find_ci(workdir, rel[0] or rel[1])
            if p:
                confs.append(p)
        return data.get("name", ""), confs, workdir
    return "", [], None


def read_conf(paths):
    """Settings ({section: {key: value}}) and [autoexec] lines of DOSBox
    configs, later ones overriding earlier ones, as DOSBox reads them."""
    settings, autoexec = {}, []
    for path in paths:
        section = None
        with open(path, encoding="latin-1") as f:
            for raw in f:
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


def split_args(line):
    return [a.strip('"') for a in re.findall(r'"[^"]*"|\S+', line)]


def parse_autoexec(lines, bases):
    """Mounts {drive: (kind, host path or [images])} and the commands run
    after them (DOS commands for the batch file). Mounted paths are relative
    to DOSBox's working directory, the first of bases; the others are tried
    if it has no such path (an absolute one is taken as it is, as DOSBox
    does). A missing one is kept as written, for the report."""
    mounts, commands = {}, []

    def find(rel):
        for b in bases:
            p = find_ci(b, rel)
            if p:
                return p
        return rel if os.path.isabs(rel) and os.path.exists(rel) else None

    for line in lines:
        words = split_args(line.lstrip("@"))
        if not words:
            continue
        cmd = words[0].lower()
        opts = [w.lower() for w in words[3:]]
        mtype = opts[opts.index("-t") + 1] if "-t" in opts[:-1] else ""
        if cmd == "mount" and len(words) >= 3:
            if mtype == "overlay":
                continue  # GOG's cloud-save overlay on top of C:
            drive = words[1].rstrip(":").upper()
            path = find(words[2])
            kind = "cdfolder" if mtype == "cdrom" else "dir"  # a folder DOSBox presents as a CD
            mounts.setdefault(drive, (kind, path) if path else ("missing", words[2]))
        elif cmd == "imgmount" and len(words) >= 3:
            drive = words[1].rstrip(":").upper()
            images, i = [], 2
            while i < len(words) and not words[i].startswith("-"):
                images.append(find(words[i]) or words[i])
                i += 1
            # CD images (-t iso or cdrom); floppy and hard disk images are attached differently.
            mounts[drive] = ("disk" if mtype in ("floppy", "hdd") else "image", images)
        elif cmd in ("exit", "cls", "echo", "@echo") or cmd.startswith("config"):
            continue
        elif cmd in ("cycles", "core", "mixer", "keyb", "intro", "rescan", "boot"):
            continue  # DOSBox's own commands
        else:
            commands.append(line.lstrip("@"))
    return mounts, commands


def cue_files(image):
    """The files a cue sheet (GOG's .ins, or .cue) refers to: the CD's data
    and audio tracks."""
    try:
        with open(image, encoding="latin-1") as f:
            head = f.read(65536)
    except OSError:
        return []
    if not re.search(r"^\s*FILE\s", head, re.M | re.I):
        return []
    found = []
    for m in re.finditer(r'^\s*FILE\s+(?:"([^"]+)"|(\S+))', head, re.M | re.I):
        p = find_ci(os.path.dirname(image), m.group(1) or m.group(2))
        if p:
            found.append(p)
    return found


# 86Box's CD-ROM drive reads ISO images, cue sheets (with BINARY data tracks
# and WAVE/AIFF/MP3 audio tracks, opened with libsndfile, which also reads
# OGG and FLAC under "WAVE") and folders (as a "virtual ISO"). Audio tracks
# must be 44.1 kHz stereo, or 86Box refuses the whole disc.
CUE_TYPES = ("BINARY", "MOTOROLA", "WAVE", "AIFF", "MP3")


def audio_format(path):
    """(channels, sample rate) of a WAV, AIFF, OGG (Vorbis, Opus) or FLAC file;
    None if it can't be told (MP3, or another format)."""
    try:
        with open(path, "rb") as f:
            head = f.read(65536)
    except OSError:
        return None
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
                return struct.unpack_from(">H", head, i + 8)[0], int(mant * 2.0 ** ((exp & 0x7FFF) - 16383 - 63))
            i += 8 + size + (size & 1)
    return None


def link_or_copy(src, dst):
    """A hard link where it can (no second copy of a big track), else a copy."""
    if os.path.exists(dst):
        os.unlink(dst)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def prepare_cd(source, dest):
    """Put a CD (an image, a cue sheet with its tracks, or a folder) into the
    folder dest for 86Box: a cue sheet is renamed .cue (86Box goes by the
    extension), names the tracks as they are on disk (case), uses file types
    86Box knows, and has audio tracks 86Box can play (converted with ffmpeg
    when needed and possible). Returns (path of what to put in the drive,
    notes)."""
    notes = []
    os.makedirs(dest, exist_ok=True)
    if os.path.isdir(source):
        to = os.path.join(dest, os.path.basename(source.rstrip("/")))
        if os.path.exists(to):
            shutil.rmtree(to)
        shutil.copytree(source, to, copy_function=lambda a, b: link_or_copy(a, b))
        return to, notes
    stem = os.path.splitext(os.path.basename(source))[0]
    if not cue_files(source) and not source.lower().endswith(".cue"):
        to = os.path.join(dest, os.path.basename(source))  # an ISO
        link_or_copy(source, to)
        return to, notes
    with open(source, encoding="latin-1") as f:
        lines = f.read().splitlines()
    out = []
    for line in lines:
        m = re.match(r'(\s*)FILE\s+(?:"([^"]+)"|(\S+))\s*(\S*)', line, re.I)
        if not m:
            out.append(line)
            continue
        indent, name, ftype = m.group(1), m.group(2) or m.group(3), m.group(4).upper()
        track = find_ci(os.path.dirname(source), name)
        if track is None:
            notes.append("the cue sheet names %s, which isn't there: 86Box will refuse the disc" % name)
            out.append(line)
            continue
        new = os.path.basename(track)
        if ftype not in ("BINARY", "MOTOROLA"):
            fmt = audio_format(track)
            ftype = ftype if ftype in ("WAVE", "AIFF", "MP3") else "WAVE"
            if fmt is not None and fmt != (2, 44100):
                wav = os.path.splitext(new)[0] + ".wav"
                if shutil.which("ffmpeg") and subprocess.run(
                        ["ffmpeg", "-v", "error", "-y", "-i", track, "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le",
                         os.path.join(dest, wav)], capture_output=True).returncode == 0:
                    notes.append("converted %s (%d channels, %d Hz) to 44.1 kHz stereo" % (new, fmt[0], fmt[1]))
                    new, ftype, track = wav, "WAVE", None
                else:
                    notes.append("%s is %d channels at %d Hz; 86Box plays only 44.1 kHz stereo and refuses the "
                                 "disc: convert it, e.g. ffmpeg -i %s -ac 2 -ar 44100 %s.wav" % (
                                     new, fmt[0], fmt[1], new, os.path.splitext(new)[0]))
        if track is not None:
            link_or_copy(track, os.path.join(dest, new))
        out.append('%sFILE "%s" %s' % (indent, new, ftype))
    to = os.path.join(dest, stem + ".cue")
    with open(to, "w", encoding="latin-1", newline="\r\n") as f:
        f.write("\n".join(out) + "\n")
    return to, notes


def set_cd_image(cfg, image, others=()):
    """Put image into the VM's first CD-ROM drive in 86box.cfg (as a path
    relative to the VM's folder), with others (more discs) in the drive's
    recent images (86Box's Media menu). False if the VM has no CD-ROM drive."""
    with open(cfg, encoding="utf-8") as f:
        lines = f.read().splitlines()
    sec = next((i for i, l in enumerate(lines) if l.strip().lower() == "[floppy and cd-rom drives]"), None)
    if sec is None:
        return False
    end = next((i for i in range(sec + 1, len(lines)) if lines[i].startswith("[")), len(lines))
    keys = [l.split("=", 1)[0].strip() for l in lines[sec + 1:end]]
    if "cdrom_01_parameters" not in keys or "none" in lines[sec + 1 + keys.index("cdrom_01_parameters")]:
        return False
    body = [l for k, l in zip(keys, lines[sec + 1:end])
            if not re.match(r"cdrom_01_image_(path|history_\d+)$", k)]
    vmdir = os.path.dirname(os.path.abspath(cfg))
    rel = [os.path.relpath(p, vmdir) for p in [image] + list(others)[:10]]
    at = next((i + 1 for i, l in enumerate(body) if l.split("=", 1)[0].strip() == "cdrom_01_ide_channel"),
              next((i + 1 for i, l in enumerate(body) if l.split("=", 1)[0].strip() == "cdrom_01_parameters")))
    body[at:at] = ["cdrom_01_image_path = %s" % rel[0]] + [
        "cdrom_01_image_history_%02d = %s" % (n + 1, r) for n, r in enumerate(rel[1:])]
    lines[sec + 1:end] = body
    with open(cfg, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return True


def mtools(*args, check=True):
    r = subprocess.run(list(args), capture_output=True, text=True)
    if check and r.returncode:
        target = args[args.index("-i") + 1]
        sys.exit("%s failed on %s: %s\nIs that a FAT image, with the FAT partition at that byte offset "
                 "(--offset)? 86Box must not be running on it." % (
                     args[0], target, (r.stderr or r.stdout).strip() or "exit code %d" % r.returncode))
    return r


def copy_tree(src, image, offset, dest, exclude=()):
    """Copy src's game files to ::dest on the image, leaving out GOG's own
    files at its top, manuals (PDF) and the files and folders in exclude (CD
    images, which the VM mounts as a drive); returns (files, bytes)."""
    exclude = {os.path.realpath(p) for p in exclude}
    target = "%s@@%d" % (image, offset)
    mtools("mmd", "-D", "s", "-i", target, "::" + dest, check=False)  # -D s: an existing one is fine
    files = size = 0
    for dirpath, dirnames, filenames in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        if rel == ".":
            dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS]
        dirnames[:] = [d for d in dirnames if os.path.realpath(os.path.join(dirpath, d)) not in exclude]
        for d in dirnames:
            mtools("mmd", "-D", "s", "-i", target, "::" + "/".join(p for p in (dest, rel, d) if p and p != "."),
                   check=False)
        for name in filenames:
            if (rel == "." and SKIP_FILES.match(name)) or name.lower().endswith(".pdf") or \
                    os.path.realpath(os.path.join(dirpath, name)) in exclude:
                continue
            to = "/".join(p for p in (dest, rel, name) if p and p != ".")
            mtools("mcopy", "-o", "-i", target, os.path.join(dirpath, name), "::" + to)
            files += 1
            size += os.path.getsize(os.path.join(dirpath, name))
    return files, size


def attach_cds(discs, cfg, short):
    """Put the game's CD (the first of discs, [(drive letter, image or
    folder)]) into the VM's CD-ROM drive when the image is a VM's disk
    (86box.cfg next to it), copied to its folder cd/SHORT; further discs go
    into the drive's recent images. Else say what to attach."""
    if not discs:
        return
    if not os.path.isfile(cfg):
        for drive, what in discs:
            print("Drive %s: is a CD in DOSBox: %s. Put it in the VM's CD-ROM drive (86Box's Media menu)." % (
                drive, what))
        return
    dest = os.path.join(os.path.dirname(cfg), "cd", short)
    ready = []
    for drive, what in discs:
        if not os.path.exists(what):
            print("Drive %s: is the CD %s in DOSBox, which isn't there." % (drive, what))
            continue
        to, notes = prepare_cd(what, dest)
        ready.append((drive, to))
        for note in notes:
            print("CD %s: %s." % (os.path.basename(to), note))
    if not ready:
        return
    if not set_cd_image(cfg, ready[0][1], [to for _, to in ready[1:]]):
        print("The VM has no CD-ROM drive: add one (86Box's Settings, Storage) or make the VM again with "
              "make_game_vm.sh, then put %s in it." % ready[0][1])
        return
    print("CD: %s is in the VM's CD-ROM drive, D: in DOS (copied to %s)." % (os.path.basename(ready[0][1]), dest))
    if ready[0][0] != "D":
        print("DOSBox gave the game its CD as %s:. If the game needs that letter, change the VM's "
              "A:\\AUTOEXEC.BAT line to SHSUCDX /D:?CDROM1,%s." % (ready[0][0], ready[0][0]))
    if len(ready) > 1:
        print("More discs: %s, in 86Box's Media menu (CD-ROM 1, recent images) to change discs." % ", ".join(
            os.path.basename(to) for _, to in ready[1:]))


BAT_MARK = "rem gog_import.py: "  # the batch file's second line names the game it starts


def free_name(target, base, game):
    """base, or base with a digit, whose batch file on the image is unused or
    starts this game: games whose names begin alike (Wing Commander, Wing
    Commander II) get names of their own."""
    for n in range(10):
        short = base if n == 0 else base[:7] + str(n)
        r = mtools("mtype", "-i", target, "::/%s.BAT" % short, check=False)
        if r.returncode or (BAT_MARK + game).lower() in r.stdout.lower():
            return short
    sys.exit("C:\\%s.BAT and nine more names like it are taken: give --batch and --dir." % base)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("source", help="a GOG installer (setup_*.exe, or *.sh for Linux) or an installed game's folder")
    ap.add_argument("image", help="the 86Box disk or floppy image (FAT)")
    ap.add_argument("--offset", type=int, help="byte offset of the FAT partition (default: from the "
                    "partition table; 0 without one)")
    ap.add_argument("--dir", default="", help="directory on the image (default: the game's own, under C:\\)")
    ap.add_argument("--batch", default="", help="name of the batch file written at the image's root")
    a = ap.parse_args()
    if not os.path.isfile(a.image):
        sys.exit("No disk image at %s." % a.image)
    offset = partition_offset(a.image) if a.offset is None else a.offset
    if not os.path.exists(a.source):
        sys.exit("No installer or folder at %s." % a.source)

    tmp = tempfile.mkdtemp(prefix="gog-") if os.path.isfile(a.source) else None
    try:
        if tmp:
            unpack(a.source, tmp)
            root = find_game_root(tmp)
        else:
            root = find_game_root(a.source)
        name, confs, workdir = play_task(root)
        gameinfo = find_ci(root, "gameinfo")  # Linux installers: the name on its first line
        if not name and gameinfo and os.path.isfile(gameinfo):
            with open(gameinfo, encoding="utf-8", errors="replace") as f:
                name = f.readline().strip()
        if not confs:  # no play task: the DOSBox configs for one player, the base one first
            confs = sorted((p for p in glob.glob(os.path.join(root, "*.conf"))
                            if re.match(r"dosbox", os.path.basename(p), re.I) and
                            not re.search(r"client|server|setup", os.path.basename(p), re.I)),
                           key=lambda p: ("single" in p.lower(), p))
            workdir = find_ci(root, "DOSBOX") or root
        if not confs:
            sys.exit("No DOSBox configuration found in %s: is this a GOG DOS game?" % root)
        settings, autoexec = read_conf(confs)
        mounts, commands = parse_autoexec(autoexec, [workdir, root])
        if "C" not in mounts or mounts["C"][0] != "dir" or not os.path.isdir(mounts["C"][1]):
            sys.exit("The DOSBox configuration mounts no folder as C: (%s)." % (mounts.get("C"),))
        game_root = mounts["C"][1]

        # Where the game runs: the folder its commands "cd" into, which keeps
        # its own relative paths working. Only that folder is copied (GOG's
        # extras next to it stay behind), to the same place on the image; a
        # game run from C:'s root gets a folder of its own (or --dir).
        cds = [c.split(None, 1)[1].strip().replace("/", "\\") for c in commands if c.lower().startswith("cd ")]
        top = cds[0].lstrip("\\").split("\\")[0] if cds else ""
        target = "%s@@%d" % (a.image, offset)
        short = free_name(target, re.sub(r"[^A-Z0-9_]", "", (name or "GAME").upper())[:8] or "GAME", name)
        if a.dir or not top:
            game_dir = (a.dir or short).strip("\\/")
            src, dest = game_root, game_dir
            # Its commands ran with C:\ as the game's folder: start there, and
            # make absolute "cd \X" relative to it.
            commands = ["cd \\" + game_dir.replace("/", "\\")] + [
                "cd " + c.split(None, 1)[1].strip().lstrip("\\") if c.lower().startswith("cd ") else c
                for c in commands]
        else:
            src = find_ci(game_root, top)
            if src is None:
                sys.exit("The game's commands go into %s, which isn't in %s." % (top, game_root))
            game_dir = dest = os.path.basename(src).upper()
            if not cds[0].startswith("\\"):
                commands = ["cd \\"] + commands  # its "cd" was from C:'s root
        images = [p for kind, what in mounts.values() if kind == "image" for p in what]
        images += [f for p in images for f in cue_files(p)]  # and the tracks their cue sheets list
        images += [what for kind, what in mounts.values() if kind == "cdfolder"]
        files, size = copy_tree(src, a.image, offset, "/" + dest, images)
        on_cd = {os.path.realpath(p) for p in images}  # attached as the CD, not left behind
        others = [n for n in os.listdir(game_root) if os.path.join(game_root, n) != src and
                  not (n.lower() in SKIP_DIRS or SKIP_FILES.match(n)) and
                  os.path.realpath(os.path.join(game_root, n)) not in on_cd]

        # The batch file: the game's commands, on C: (the image's drive).
        bat = (a.batch or short) + ".BAT"
        lines = ["@echo off", "c:"] + [c for c in commands if c.lower() not in ("c:",)]
        with tempfile.NamedTemporaryFile("w", suffix=".bat", delete=False, newline="\r\n") as f:
            f.write("\n".join(lines[:1] + [BAT_MARK + (name or "")] + lines[1:]) + "\n")
        mtools("mcopy", "-o", "-i", "%s@@%d" % (a.image, offset), f.name, "::/" + bat)
        os.unlink(f.name)

        print("%s: %d files (%d KB) copied to C:\\%s; run C:\\%s." % (
            name or "Game", files, size // 1024, game_dir.replace("/", "\\"), bat))
        print("%s runs: %s" % (bat, " | ".join(lines[1:])))
        if others and src != game_root:
            print("Not copied (next to the game in GOG's folder): %s" % ", ".join(sorted(others)[:12]))
        discs = []  # (drive letter, image or folder)
        for drive, (kind, what) in sorted(mounts.items()):
            if drive == "C":
                continue
            if kind == "image":
                discs += [(drive, p) for p in what]
            elif kind == "cdfolder":
                discs.append((drive, what))
            elif kind == "disk":
                print("Drive %s: is the disk image %s in DOSBox: attach it to the VM in 86Box's settings if the "
                      "game needs it." % (drive, ", ".join(what)))
            elif kind == "dir":
                print("Drive %s: is the folder %s in DOSBox; copy it too if the game needs it." % (drive, what))
            else:
                print("Drive %s: is the folder %s in DOSBox, which isn't there." % (drive, what))
        attach_cds(discs, os.path.join(os.path.dirname(os.path.abspath(a.image)), "86box.cfg"), short)
        # make_game_vm.sh's machine has DOSBox's defaults: a Sound Blaster 16
        # at 220, IRQ 7, DMA 1, high DMA 5, and EMS (JemmEx).
        sb = settings.get("sblaster", {})
        sbtype = sb.get("sbtype", "sb16").lower()
        card = (sb.get("sbbase", "220"), sb.get("irq", "7"), sb.get("dma", "1"), sb.get("hdma", "5"))
        n = 4 if sbtype == "sb16" else 3  # only the SB16 has a high DMA
        if sbtype != "none" and card[:n] != ("220", "7", "1", "5")[:n]:
            print("Sound in DOSBox: %s at port %s, IRQ %s, DMA %s%s, unlike the game VM's Sound Blaster 16 (220, "
                  "IRQ 7, DMA 1, high DMA 5): run the game's setup to pick the VM's settings." % (
                      sbtype, card[0], card[1], card[2], ", high DMA %s" % card[3] if sbtype == "sb16" else ""))
        if settings.get("gus", {}).get("gus", "false").lower() in ("true", "1"):
            print("DOSBox also emulates a Gravis Ultrasound, which the VM lacks: pick Sound Blaster in the game's "
                  "setup if it uses the GUS.")
        if settings.get("dos", {}).get("ems", "true").lower() in ("false", "0"):
            print("DOSBox runs this game without EMS, which the game VM's JemmEx provides. Most games don't mind; "
                  "if this one fails, change the line in the VM's A:\\FDCONFIG.SYS to DEVICE=JEMMEX.EXE NOEMS.")
        mem = settings.get("dosbox", {}).get("memsize")
        if mem:
            print("Memory in DOSBox: %s MB." % mem)
        cpu = settings.get("cpu", {})
        if cpu.get("cycles"):
            print("CPU speed in DOSBox: cycles=%s (pick the VM's CPU to suit)." % cpu["cycles"])
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
