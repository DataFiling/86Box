#!/usr/bin/env python3
"""Put a GOG DOS game onto an 86Box disk image, to run and debug it there.

GOG sells DOS games wrapped in DOSBox: the installer (setup_*.exe, an Inno
Setup installer) unpacks the game's files next to a DOSBox copy, DOSBox
configuration files whose [autoexec] section mounts the game's folder as C:
(and sometimes a CD image as D:) and starts it, and goggame-*.info, which
says which configurations the "Play" button uses.

This reads those, copies what DOSBox would have mounted as C: onto a FAT
disk image (with mtools; the emulator must not be running on it), writes a
batch file that runs the game's own commands, and reports what the virtual
machine needs (a CD image, EMS, the sound card settings DOSBox emulated).

  gog_import.py setup_game.exe IMAGE [--offset BYTES] [--dir NAME]
  gog_import.py "C:/GOG Games/Game" IMAGE ...    (an installed game)

--offset: the FAT partition's byte offset in a hard disk image (e.g.
32256 = 63 sectors); 0 for a floppy image. --dir: the directory on the image
(default: the folder the game runs in, under C:\\). Needs innoextract for
installers, and mtools.
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

# GOG's own files around the game, never part of it.
SKIP_DIRS = {"dosbox", "__support", "__redist", "commonappdata", "tmp", "app", "cloud_saves"}
SKIP_FILES = re.compile(r"^(goggame-.*|gog\.ico|gfw_high.*|support\.ico|unins\d+\..*|webcache\.zip|"
                        r"dosbox.*\.conf|launch.*\.(bat|lnk)|eula.*|.*\.lnk)$", re.I)


def unpack(installer, dest):
    """Unpack a GOG installer with innoextract into dest; returns the game root."""
    if shutil.which("innoextract") is None:
        sys.exit("innoextract is needed to unpack installers (apt install innoextract), "
                 "or give the installed game's folder instead.")
    r = subprocess.run(["innoextract", "--gog", "--silent", "--output-dir", dest, installer],
                       capture_output=True, text=True)
    if r.returncode:
        sys.exit("innoextract could not unpack %s (is it a GOG installer? Multi-part installers need their "
                 ".bin files next to the .exe):\n%s" % (installer, (r.stderr or r.stdout).strip()))
    # Older GOG installers put the game under app/.
    app = os.path.join(dest, "app")
    return app if os.path.isdir(app) and glob.glob(os.path.join(app, "goggame-*.info")) else dest


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
                if section == "autoexec":
                    if line:
                        autoexec.append(line)
                elif section and "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    settings.setdefault(section, {})[k.strip().lower()] = v.strip()
    return settings, autoexec


def split_args(line):
    return [a.strip('"') for a in re.findall(r'"[^"]*"|\S+', line)]


def parse_autoexec(lines, workdir):
    """Mounts {drive: (kind, host path or [images])} and the commands run
    after them (DOS commands for the batch file)."""
    mounts, commands = {}, []
    for line in lines:
        words = split_args(line.lstrip("@"))
        if not words:
            continue
        cmd = words[0].lower()
        if cmd == "mount" and len(words) >= 3:
            if "-t" in [w.lower() for w in words[3:]] and "overlay" in [w.lower() for w in words[3:]]:
                continue  # GOG's cloud-save overlay on top of C:
            drive = words[1].rstrip(":").upper()
            mounts.setdefault(drive, ("dir", find_ci(workdir, words[2]) or words[2]))
        elif cmd == "imgmount" and len(words) >= 3:
            drive = words[1].rstrip(":").upper()
            images, i = [], 2
            while i < len(words) and not words[i].startswith("-"):
                images.append(find_ci(workdir, words[i]) or words[i])
                i += 1
            mounts[drive] = ("image", images)
        elif cmd in ("exit", "cls", "echo", "@echo") or cmd.startswith("config"):
            continue
        elif cmd in ("cycles", "core", "mixer", "keyb", "intro", "rescan", "boot"):
            continue  # DOSBox's own commands
        else:
            commands.append(line.lstrip("@"))
    return mounts, commands


def mtools(*args, check=True):
    return subprocess.run(list(args), check=check, capture_output=True, text=True)


def copy_tree(src, image, offset, dest, exclude=()):
    """Copy src's game files to ::dest on the image, leaving out GOG's own
    files at its top, manuals (PDF) and the files in exclude (CD images,
    which the VM mounts as a drive); returns (files, bytes)."""
    exclude = {os.path.realpath(p) for p in exclude}
    target = "%s@@%d" % (image, offset)
    mtools("mmd", "-i", target, "::" + dest, check=False)
    files = size = 0
    for dirpath, dirnames, filenames in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        if rel == ".":
            dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS]
        for d in dirnames:
            mtools("mmd", "-i", target, "::" + "/".join(p for p in (dest, rel, d) if p and p != "."), check=False)
        for name in filenames:
            if (rel == "." and SKIP_FILES.match(name)) or name.lower().endswith(".pdf") or \
                    os.path.realpath(os.path.join(dirpath, name)) in exclude:
                continue
            to = "/".join(p for p in (dest, rel, name) if p and p != ".")
            mtools("mcopy", "-o", "-i", target, os.path.join(dirpath, name), "::" + to)
            files += 1
            size += os.path.getsize(os.path.join(dirpath, name))
    return files, size


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("source", help="a GOG installer (setup_*.exe) or an installed game's folder")
    ap.add_argument("image", help="the 86Box disk or floppy image (FAT)")
    ap.add_argument("--offset", type=int, default=0, help="byte offset of the FAT partition (hard disk: 32256)")
    ap.add_argument("--dir", default="", help="directory on the image (default: the game's own, under C:\\)")
    ap.add_argument("--batch", default="", help="name of the batch file written at the image's root")
    a = ap.parse_args()

    tmp = None
    if os.path.isfile(a.source):
        tmp = tempfile.mkdtemp(prefix="gog-")
        root = unpack(a.source, tmp)
    else:
        root = a.source
    try:
        name, confs, workdir = play_task(root)
        if not confs:  # no play task: every DOSBox config, the base one first
            confs = sorted(glob.glob(os.path.join(root, "dosbox*.conf")), key=lambda p: ("single" in p, p))
            workdir = find_ci(root, "DOSBOX") or root
        if not confs:
            sys.exit("No DOSBox configuration found in %s: is this a GOG DOS game?" % root)
        settings, autoexec = read_conf(confs)
        mounts, commands = parse_autoexec(autoexec, workdir)
        if "C" not in mounts or mounts["C"][0] != "dir" or not os.path.isdir(mounts["C"][1]):
            sys.exit("The DOSBox configuration mounts no folder as C: (%s)." % mounts.get("C"))
        game_root = mounts["C"][1]

        # Where the game runs: the folder its commands "cd" into, which keeps
        # its own relative paths working. Only that folder is copied (GOG's
        # extras next to it stay behind), to the same place on the image; a
        # game run from C:'s root gets a folder of its own (or --dir).
        cds = [c.split(None, 1)[1].strip().replace("/", "\\") for c in commands if c.lower().startswith("cd ")]
        top = cds[0].lstrip("\\").split("\\")[0] if cds else ""
        if a.dir or not top:
            game_dir = (a.dir or re.sub(r"[^A-Z0-9_]", "", (name or "GAME").upper())[:8] or "GAME").strip("\\/")
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
        files, size = copy_tree(src, a.image, a.offset, "/" + dest, images)
        others = [n for n in os.listdir(game_root) if os.path.join(game_root, n) != src and
                  not (n.lower() in SKIP_DIRS or SKIP_FILES.match(n))]

        # The batch file: the game's commands, on C: (the image's drive).
        bat = (a.batch or re.sub(r"[^A-Z0-9_]", "", (name or game_dir).upper())[:8] or "GAME") + ".BAT"
        lines = ["@echo off", "c:"] + [c for c in commands if c.lower() not in ("c:",)]
        with tempfile.NamedTemporaryFile("w", suffix=".bat", delete=False, newline="\r\n") as f:
            f.write("\n".join(lines) + "\n")
        mtools("mcopy", "-o", "-i", "%s@@%d" % (a.image, a.offset), f.name, "::/" + bat)
        os.unlink(f.name)

        print("%s: %d files (%d KB) copied to C:\\%s; run C:\\%s." % (
            name or "Game", files, size // 1024, game_dir.replace("/", "\\"), bat))
        print("%s runs: %s" % (bat, " | ".join(lines[1:])))
        if others and src != game_root:
            print("Not copied (next to the game in GOG's folder): %s" % ", ".join(sorted(others)[:12]))
        for drive, (kind, what) in sorted(mounts.items()):
            if drive != "C" and kind == "image":
                print("Drive %s: is a CD image in DOSBox: %s. Give the VM a CD-ROM drive with it (and a CD "
                      "driver plus SHSUCDX/MSCDEX in DOS) for the game to find it." % (drive, ", ".join(what)))
            elif drive != "C":
                print("Drive %s: is the folder %s in DOSBox; copy it too if the game needs it." % (drive, what))
        sb = settings.get("sblaster", {})
        if sb.get("sbtype", "sb16") != "none":
            print("Sound in DOSBox: %s at port %s, IRQ %s, DMA %s%s; set the VM's sound card to match, or the "
                  "game's own setup to the VM's." % (sb.get("sbtype", "sb16"), sb.get("sbbase", "220"),
                                                     sb.get("irq", "7"), sb.get("dma", "1"),
                                                     ", high DMA %s" % sb["hdma"] if "hdma" in sb else ""))
        dosset = settings.get("dos", {})
        if dosset.get("ems", "true").lower() not in ("false", "0"):
            print("DOSBox provides EMS: if the game needs it, load an EMS manager in the VM (e.g. JemmEx).")
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
