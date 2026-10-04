# Debugger test setup

Three emulated machines, one per DOS game era, boot FreeDOS from a floppy
that also holds test programs with known, planted bugs. AI agents (or you)
debug those programs through the bridge, and the results can be checked
against [GROUND_TRUTH.md](GROUND_TRUTH.md).

| Machine | Config | Era it stands for | Mouse |
|---|---|---|---|
| ASUS ISA-386C, 386DX-33 + 387, VGA, SB16 | `vms/386.cfg` | late-80s/early-90s real-mode games | serial (COM1) |
| AMI 486 (OPTi 495), 486DX-33, VGA, SB16 | `vms/486.cfg` | 486 DOS-extender games | serial (COM1) |
| ASUS P/I-P55T2P4 (i430HX), Pentium MMX 200, VGA, SB16 | `vms/pentium.cfg` | Pentium-era games | PS/2 |

Programs on the floppy (sources in `games/`):

| Program | Kind | Planted bug |
|---|---|---|
| TXTGAME | 16-bit, text mode, BIOS keyboard | hangs after 5 gems |
| VGAGAME | 16-bit, mode 13h, own INT 9 handler | goes wrong at game over |
| PMGAME | 32-bit DOS/4GW, mode 13h, INT 33h mouse | lives jump after rapid fire |
| MOUSETST | 16-bit, shows INT 33h state | none (mouse check) |
| BENCH | 32-bit DOS/4GW, CPU/FPU/memory/VGA workload | none (speed) |

Keep `games/` and `GROUND_TRUTH.md` away from an AI being tested.

## Building

Everything is built from source; only `git` access to GitHub is needed.

1. **Open Watcom v2** (DOS C compiler, linker, DOS/4GW):
   `git clone --depth 1 https://github.com/open-watcom/open-watcom-v2`, then
   in it `OWGUINOBUILD=1 . ./setvars.sh && ./build.sh rel` (about 35 minutes
   on 4 cores). The result is in `rel/`.
2. **FreeDOS kernel**: `git clone --depth 1 https://github.com/FDOS/kernel`,
   `git submodule update --init --depth 1 country`, `printf 'XUPX=\n' > config.mak`
   (no UPX), then with `WATCOM=…/rel PATH=$WATCOM/binl64:$PATH INCLUDE=$WATCOM/h`:
   `make all COMPILER=owlinux`. The kernel is `bin/kernel.sys`.
3. **FreeCOM**: `git clone --depth 1 https://github.com/FDOS/freecom`, then
   `./build.sh wc` with the same environment. The shell is `command.com`.
4. **JWasm** (to assemble CuteMouse): `git clone --depth 1
   https://github.com/Baron-von-Riedesel/JWasm`, `make -f GccUnix.mak`.
5. **CuteMouse** source: `git clone --depth 1 https://github.com/FDOS/mouse`.
6. `build_testbed.sh` (set `WATCOM`, `FDOS_KERNEL_DIR`, `FREECOM`,
   `CTMOUSE_SRC`, `JWASM` if your paths differ) compiles the programs,
   assembles CuteMouse and writes `out/boot.img` with `build_floppy.sh`.
   Needs `nasm` and `mtools`.

## Running a machine

```sh
mkdir -p vm/486 && cp vms/486.cfg vm/486/86box.cfg && cp out/boot.img vm/486/
86Box -P vm/486 -R /path/to/roms          # built with -DGDBSTUB=ON
python3 cmos_floppy.py --port 12346 --fpu  # first boot only
```

The configs use debugger ports 12347 (386), 12346 (486) and 12348
(Pentium). On a fresh machine the CMOS has no floppy drive; `cmos_floppy.py`
sets one through the debugger. AMI BIOSes then report a CMOS checksum error
(their checksum covers more than the standard range): press F1, choose
"Write to CMOS and Exit" and confirm with Y. The Award BIOS loads its
defaults, which already include the drive; press F1. 86Box keeps the CMOS in
the VM's `.nvr` file, so this is needed once.

Each machine then boots to `A:\>` with CuteMouse loaded (`CTMOUSE /O`, so it
detects the Pentium machine's wheel mouse) and prints `READY`.
Run a program by typing its name (`type_text text="PMGAME\n"`); DOS/4GW
programs take several seconds to load from the floppy.

## Measured emulation speed

With the debugger enabled 86Box runs the CPU interpreter only. Running BENCH
on the Pentium machine, the emulator kept real time (emulated BIOS ticks per
host second) under a CPU-bound load:

| Emulated CPU | Emulated s / host s | 86Box host CPU |
|---|---|---|
| Pentium 100 | 1.00 | 59% of one core |
| Pentium MMX 200 | 0.99 | 73% of one core |

(Host: 4-core cloud VM. BENCH scores, which count work per emulated second,
doubled from 100 to 200 MHz: `int=2633 fpu=2376 mem=1161` vs
`int=5564 fpu=4777 mem=2441`.)
