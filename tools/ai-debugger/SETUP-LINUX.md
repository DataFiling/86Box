# Quick start on Linux with Claude Code

From nothing to "Claude, find out why this DOS game crashes" in about 30
minutes, most of it waiting for 86Box to build. Commands are for Ubuntu and
Debian; other distributions have the same packages under similar names.

You end up with:

- 86Box built with the debugger,
- a ready DOS machine (Pentium MMX 200, FreeDOS with EMS, Sound Blaster 16,
  mouse, a CD-ROM drive, an empty 500 MB C: for games),
- the debugger bridge registered in Claude Code.

Before you start, you need:

- Ubuntu 22.04 or later, or Debian 12 or later, with a desktop: 86Box
  opens a window.
- Claude Code, installed and logged in: `claude` starts it in a terminal.
- About 1 GB of disk, plus your games.

Directories used below: the source in `~/86Box`, the ROMs in
`~/86box-roms`, the machine in `~/86box-vms/game-vm`. Any others work too.

## 1. Packages

```sh
sudo apt install build-essential cmake ninja-build pkg-config git \
    libfreetype-dev libgles2-mesa-dev libpng-dev librtmidi-dev libopenal-dev \
    libslirp-dev libfluidsynth-dev libvdeplug-dev libserialport-dev libzstd-dev \
    qtbase5-dev qtbase5-private-dev qttools5-dev qttranslations5-l10n \
    libsdl2-dev python3-venv mtools innoextract
```

The last line covers SDL (for the build), Python for the bridge, and the GOG
importer's tools (mtools, innoextract).

## 2. Build 86Box with the debugger

```sh
git clone -b claude/amazing-noether-mnz5kw https://github.com/DataFiling/86Box ~/86Box
cd ~/86Box
cmake --preset regular -DGDBSTUB=ON -DSDL2=ON
cmake --build build/regular            # 10-20 minutes; the program is build/regular/src/86Box
```

- **`-DSDL2=ON` builds with SDL 2.** 86Box otherwise wants SDL 3, which
  Ubuntu 24.04 and older don't package. With `libsdl3-dev` installed,
  leave the option out.
- **Qt 6 instead of Qt 5:** add `-DUSE_QT6=ON` and install the Qt 6
  packages (`qt6-base-dev qt6-base-private-dev qt6-tools-dev
  qt6-tools-dev-tools qt6-l10n-tools qt6-translations-l10n`).
- **The debugger forces the CPU interpreter.** The recompiler doesn't
  support breakpoints. Emulation keeps real time up to about a Pentium
  MMX 200.

## 3. The ROMs

```sh
git clone --depth 1 https://github.com/86Box/roms ~/86box-roms
```

## 4. The game machine

```sh
mkdir -p ~/86box-vms
git -C ~/86Box fetch origin vm-kit
git -C ~/86Box show origin/vm-kit:game-vm.tar.xz | tar -xJ --skip-old-files -C ~/86box-vms
```

This unpacks `~/86box-vms/game-vm`: the machine's settings, a FreeDOS boot
floppy, an empty 500 MB hard disk (C:) and its BIOS settings.
`--skip-old-files` keeps the files of a machine you already have, so
running this again doesn't wipe the games on its disk. The kit's
README on the `vm-kit` branch lists what's inside, with the licenses and
sources of the DOS programs. To build the machine yourself instead, see
`testdata/make_game_vm.sh` (it needs the DOS tools from `testdata/README.md`).

## 5. The bridge, registered in Claude Code

```sh
python3 -m venv ~/86box-venv
~/86box-venv/bin/pip install -r ~/86Box/tools/ai-debugger/requirements.txt
claude mcp add --scope user 86box -- ~/86box-venv/bin/python ~/86Box/tools/ai-debugger/server.py
```

- **Why the venv:** recent Ubuntu and Debian refuse `pip install` outside
  one ("externally-managed-environment").
- **Why `--scope user`:** it makes the tools available in every directory.
  Without it, Claude Code only offers them in the directory you ran the
  command in.
- **`claude mcp list`** shows whether the server is registered.
  "Connected" there means the bridge starts; whether it reaches 86Box
  shows in step 6.
- **To register it again** (with other options, below): first
  `claude mcp remove 86box -s user`. `claude mcp add` refuses a name that
  is already registered.
- **Fewer questions:** Claude Code asks before it uses each tool. To let it
  use all the bridge's tools without asking, add `mcp__86box` to the
  allowed list in `~/.claude/settings.json` (merge it into the file if it
  has other settings):

  ```json
  { "permissions": { "allow": ["mcp__86box"] } }
  ```

**Optional:** `identify_functions` names the C library functions of games
built with Open Watcom, from Open Watcom's libraries. To use it, install
Open Watcom v2 (github.com/open-watcom/open-watcom-v2/releases). Register
the bridge with `-e WATCOM=/path/to/watcom` after `86box`, or put `wcl386`
on the PATH.

## 6. Start the machine and check the connection

```sh
pgrep -a 86Box      # prints nothing if no 86Box is running
~/86Box/build/regular/src/86Box -P ~/86box-vms/game-vm -R ~/86box-roms &
~/86box-venv/bin/python ~/86Box/tools/ai-debugger/smoke_test.py    # RESULT: PASS
```

Only one 86Box at a time can have the debugger port. A second one prints
`GDB Stub: Failed to bind on port 12345` and runs without the debugger.

- **The machine starts paused.** 86Box waits for the debugger, so the
  window stays black until something resumes it. The smoke test does,
  and so does Claude when you ask it to do anything. To resume it from a
  shell:
  `~/86box-venv/bin/python ~/86Box/tools/ai-debugger/run_tool.py resume`.
- **It boots from the floppy to `C:\>`.** The first boot prints `READY`
  after loading JemmEx and the mouse driver.
- **Use the debugger to pause, not the 86Box window.** Pausing from 86Box's
  own menu stops the debugger from answering, and its tools then time out.
- **Closing 86Box:** close its window, or use Action > Exit, and answer
  Yes ("Don't show this message again" stops the question). Ctrl+C in the
  terminal and `kill` don't stop it.
- **Speed depends on your computer.** The debugger runs the CPU without
  the recompiler, so the emulated Pentium MMX 200 may run slower than a
  real one: on a 2.1 GHz server core it ran at about half speed. The
  window's title bar shows the speed in percent. Games still work, only
  slower.
- **The debugger port has no password.** Like the rest of 86Box's
  debugger, it accepts connections from the network, not only from your
  computer. On a network you share, keep port 12345 closed in your
  firewall.

## 7. Put a game on C:

Close 86Box first: the disk image must not change while the machine runs.

```sh
~/86box-venv/bin/python ~/86Box/tools/ai-debugger/gog_import.py \
    ~/Downloads/setup_some_game.exe ~/86box-vms/game-vm/hd.img
```

- **What it accepts:** GOG's Windows offline installer (`setup_*.exe`,
  with its `.bin` parts in the same folder), GOG's Linux installer
  (`*.sh`), or a folder where GOG installed the game. On gog.com these
  are under the game's "offline backup game installers".
- **Not tried with a real GOG download yet.** The tests use folders laid
  out like GOG's. If an installer fails, install the game with Wine (or
  copy its folder from a Windows PC) and give the importer that folder.
  Ubuntu's innoextract may also be too old for a recent Windows installer;
  the error then says so.
- **What it does:** it copies the game to C: and writes `C:\GAMENAME.BAT`,
  which starts it the way GOG's DOSBox did. It then says what else the
  game expects.
- **CD games:** the game's CD goes into the machine's CD-ROM drive, D: in
  DOS. The importer copies it into `~/86box-vms/game-vm/cd/`. A game with
  several discs gets the others in 86Box's Media menu (CD-ROM 1, recent
  images), where you change discs. CD music tracks must be 44.1 kHz
  stereo; with `ffmpeg` installed (`sudo apt install ffmpeg`) the
  importer converts others, and otherwise it says which.
- **Sound and EMS need no setup.** The machine's Sound Blaster and its
  EMS are set the way GOG's DOSBox sets them. The importer says when a
  game was set up differently.
- **Other DOS software:** use mtools directly, for example `mcopy -s -i
  ~/86box-vms/game-vm/hd.img@@32256 MYGAME ::/MYGAME`.

## 8. Ask Claude

Start 86Box again, run `claude` in any directory, and ask, for example:

- "The game in C:\SOMEGAME crashes when I open the map screen. Run it and
  find out why."
- "Start C:\SOMEGAME.BAT, stop at the program's first instruction, and
  tell me which DOS calls it makes while loading."
- "Find where the game keeps the number of lives and what changes it."

The 86Box window shows what happens. Claude also takes screenshots, types,
and uses the mouse in the machine.

## Next time

Start the machine (step 6) and run `claude`. To update everything later:

```sh
git -C ~/86Box pull
cmake --build ~/86Box/build/regular
~/86box-venv/bin/pip install -r ~/86Box/tools/ai-debugger/requirements.txt
```

## If something goes wrong

| Problem | Fix |
|---|---|
| `Could not find a package configuration file provided by "SDL3"` | Add `-DSDL2=ON` to the cmake command, or install `libsdl3-dev` |
| `externally-managed-environment` from pip | Use the venv from step 5 |
| `Cannot reach the 86Box GDB stub at 127.0.0.1:12345` | 86Box isn't running, was built without `-DGDBSTUB=ON`, or the machine uses another port (`gdbstub_port` in `86box.cfg`; register the bridge again, step 5, with `-e BOX86_GDB_PORT=N` after `86box`) |
| `GDB Stub: Failed to bind on port 12345` | Another 86Box has the port: `pgrep -a 86Box`, and close it |
| Claude doesn't list the 86box tools | `claude mcp list`; register again with `--scope user` (step 5) |
| `claude mcp list` says "Failed to connect" | The bridge doesn't start: run `~/86box-venv/bin/python ~/86Box/tools/ai-debugger/server.py` to see its error (it waits for input when it works; Ctrl+C) |
| `MCP server 86box already exists` from `claude mcp add` | `claude mcp remove 86box -s user`, then add again |
| The 86Box window stays black | The machine is paused: see step 6 |
| Tools time out | 86Box was paused from its own menu: resume it there |
| `Not a supported Inno Setup installer` | The installer is newer than your innoextract: use the game's installed folder (step 7), or a newer innoextract (github.com/dscharrer/innoextract) |
| A game has no sound | Run its setup program and pick Sound Blaster 16 at 220, IRQ 7, DMA 1 (high DMA 5) |
| Sound effects play but the music is silent | The game plays General MIDI music, which needs a synthesizer: `sudo apt install fluid-soundfont-gm`, then in 86Box's Settings > Sound > MIDI, set MIDI Out Device to FluidSynth. Or pick Sound Blaster or AdLib music in the game's setup |
| A game can't find its CD | In the machine, `DIR D:` should list the CD. If the importer said the game used another letter, change `SHSUCDX /D:?CDROM1,D` in `A:\AUTOEXEC.BAT` to that letter. An empty drive means 86Box refused the image: see the importer's notes about its tracks |
| The machine has no CD-ROM drive | It was unpacked from a kit older than CD support: unpack the kit into another folder and copy its `boot.img` and `86box.cfg` into your machine's folder (your games on `hd.img` stay) |

## Other AI clients

The bridge is a standard MCP server: it runs as a command (the venv's
python with `server.py`) and talks over stdin and stdout. Any MCP client can
start it the same way: Claude Desktop, other editors and agents.
`run_tool.py` runs one tool per command, for scripts and agents without MCP
(`run_tool.py help` lists them).
