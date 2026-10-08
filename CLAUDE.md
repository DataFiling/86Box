# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

86Box is a low-level x86 PC emulator (IBM PC 5150 through PCI/AGP-era systems) written mostly in C, with a Qt (C++) user interface. Accuracy is the priority: per CONTRIBUTING.md, code must be "technically correct" — getting the right output the wrong way is not acceptable — and must not introduce new compiler warnings.

## Build

CMake (≥3.20) with Ninja, driven by presets in `CMakePresets.json`. Build dirs land in `build/<preset>`.

```sh
cmake --preset regular            # Release; also: optimized, debug, development, dev_debug, ultra_debug
cmake --build build/regular
make development                  # Makefile wrapper around the same presets (qt=6, dynarec=new)
```

- `development`/`dev_debug`/`ultra_debug` set `DEV_BRANCH=ON`, which enables in-progress hardware gated by `cmake_dependent_option(... "DEV_BRANCH" ...)` in the top-level `CMakeLists.txt`. New experimental devices should be gated the same way.
- Other notable options: `QT` (OFF builds the SDL frontend in `src/unix/`), `USE_QT6`, `NEW_DYNAREC`, `DYNAREC`, `GDBSTUB`, `LIBASAN`, `VNC`, `MINITRACE`.
- CI build steps and Linux package lists: `.github/workflows/cmake_linux.yml` (also `_macos`, `_windows_msys2`); `.ci/` holds Jenkins/AppImage scripts.
- At runtime the emulator needs the separate ROM set (github.com/86Box/roms); machines/devices whose ROMs are missing are hidden via their `available()` callback.

## Tests

GoogleTest suites under `tests/` (mirrors `src/` layout; name tests `<source-file>_test.cpp` / `_benchmark.cpp`). Requires `-DBUILD_TESTING=ON` and GTest.

```sh
cmake --preset debug -DBUILD_TESTING=ON
cmake --build build/debug --target fdc_read_id_tests
ctest --test-dir build/debug --output-on-failure                     # all
ctest --test-dir build/debug --output-on-failure -R '^FdcReadId\.'   # one suite
ctest --test-dir build/debug --output-on-failure -R '^Mach64\.Name$' # one test
```

`tests/README.md` lists each suite's target name and ctest regex. Tests are isolated device-level fixtures, not full-machine runs: they typically `#include` the device `.c` file directly into the C++ test (to reach `static` internals) and supply stub/adapter implementations of host services (timers, PIC, DMA, `pclog`, `plat_fopen`, …), sometimes via `-D` symbol renames in the test's `CMakeLists.txt`. Several `*_smoke.py` scripts boot real guest drivers in a full emulator and need external images (see `doc/hardware/*.md`). The Mitsumi suite has known pre-existing failures upstream.

## Code style

- `.clang-format` (WebKit-based: 4-space indent, aligned consecutive assignments/declarations, return type on its own line for top-level definitions, `IndentPPDirectives: AfterHash`, includes not sorted). Format only the lines you change; don't reformat whole files.
- Per-module logging pattern: `#ifdef ENABLE_<MODULE>_LOG` / `<module>_do_log` / a `<module>_log()` wrapper around `pclog_ex`, otherwise defined away as an empty macro. Follow it instead of calling `pclog` directly.
- PRs follow `.github/pull_request_template.md`; new hardware requires its ROM to be in the roms repo and should cite datasheets/documentation.

## Architecture

**Core loop and lifecycle** — `src/86box.c`: `pc_init` (command line, paths, config) → `pc_init_modules` (ROM availability scan) → `pc_reset_hard_init` (build the machine: reset subsystems, run the machine's init, then instantiate every configured card) → `pc_run` (executes CPU for a slice of time, driving timers). Emulation runs on one thread; the UI runs on another and communicates through `plat_*`/`ui_*` calls and flags.

**Configuration** — `src/config.c` reads/writes the per-VM `86box.cfg` (INI). Globals annotated `(C)` in `86box.c` are persisted config. Device-specific settings come from the device's `device_config_t` array and are read inside its `init` via `device_get_config_int()` / `_string()` etc. — the UI generates its settings dialogs from that same array.

**`device_t`** (`src/include/86box/device.h`) is the universal plug-in abstraction for nearly all hardware: name, `internal_name` (the config key — never rename it, it breaks existing configs), bus `flags` (`DEVICE_ISA`, `DEVICE_PCI`, …), `init/close/reset/available/speed_changed/force_redraw`, and `config`. `device_add()` calls `init` and tracks the instance so it is closed/reset automatically. Each category has a registration table the UI and config use to enumerate choices, e.g. `video/vid_table.c`, `sound/sound.c`, `network/network.c`, `scsi/scsi.c`, `disk/hdc.c`; a new device must be added to its table.

**Machines** — `src/machine/machine_table.c` defines `machines[]` (`machine_t`: bus flags, supported CPU packages/speeds, RAM limits, on-board devices, `init`). Board init functions live in `src/machine/m_*.c`; they load BIOS ROMs, then `device_add` the chipset (`src/chipset/`), Super I/O (`src/sio/`), keyboard controller, and on-board video/sound/etc.

**Hardware access plumbing** — devices hook into the emulated buses rather than being called directly:
- I/O ports: `io_sethandler()` / `io_removehandler()` (`src/io.c`).
- Memory: `mem_mapping_add()` / `mem_mapping_set_addr()` / `mem_mapping_disable()` and `mem_set_mem_state()` for shadow RAM/ROM decoding (`src/mem/`). Chipsets mostly manipulate these to implement PAM/shadow registers.
- PCI: `pci_add_card()` with config-space read/write callbacks (`src/pci.c`); likewise `isapnp`, `mca`, `eisa`.
- Time: `pc_timer_t` + `timer_add()` / `timer_on_auto()` (`src/timer.c`), expressed in emulated time (`TIMER_USEC`); never use host time for emulated behaviour.
- Interrupts/DMA: `picint`/`picintc` (`src/pic.c`), `dma_channel_read/write` (`src/dma.c`).

**CPU** — `src/cpu/`: `cpu_table.c` lists CPU families/models; `808x.c`/`vx0*.c` (8086-class), `386.c` (interpreter for 286+), `386_dynarec.c` with x86 opcode implementations in `x86_ops_*.h` shared between interpreter and recompiler; `codegen_timing_*.c` model per-CPU instruction timing. Two recompilers: `src/codegen/` (old) and `src/codegen_new/` (PCem v15 "new" dynarec, `NEW_DYNAREC`, required on ARM).

**Video** — `src/video/`: ~one `vid_*.c` per card. SVGA cards build on the shared `svga_t` core (`vid_svga.c`, `vid_svga_render.c`) and RAMDAC/clock-chip helpers; 3dfx Voodoo is under `vid_voodoo_*`. Rendered lines are blitted into a shared buffer consumed by the frontend renderer.

**Storage** — `src/disk/` (HDC: IDE/ATAPI, ESDI, MFM/RLL, XTA; HDD image formats), `src/cdrom/` (CD-ROM core + proprietary interfaces like Mitsumi/Sony/MKE), `src/floppy/` (FDC `fdc.c`, drives `fdd.c`, image formats incl. the bit-level `86f`), `src/scsi/` (host adapters + SCSI disk/CD devices).

**Platform/UI layer** — the core never calls OS or UI APIs directly; it uses the `plat_*` and `ui_*` interfaces declared in `src/include/86box/plat.h`/`ui.h`. Implementations: `src/qt/` (main Qt UI, settings dialogs, renderers incl. OpenGL/Vulkan/librashader, plus the built-in VM manager `qt_vmmanager_*`) or `src/unix/` (SDL frontend with optional framebuffer OSD, see `README-UNIX-MODE-WITH-OSD.txt`). Shared host-side code: `src/sound/` backends, `src/network/` (SLiRP, pcap, VDE), `src/thread.cpp`.

**Headers** — public module interfaces are in `src/include/86box/`; generated headers (version, etc.) go to `build/<preset>/src/include`.

**AI debugger bridge** — `tools/ai-debugger/` is a Python MCP server that talks to the GDB stub (`src/gdbstub.c`, enabled with `-DGDBSTUB=ON`, port `gdbstub_port`, CPU starts paused) so an AI client can debug guest software. The stub reports linear addresses only (EIP included); segment bases, descriptor tables and the code/stack size come from its `sg` monitor command. Its watchpoints rely on `GDBSTUB_MEM_ACCESS` hooks in `src/mem/mem.c` plus keeping watched pages out of the MMU lookup caches (`GDBSTUB_PAGE_WATCHED`); any new memory fast path must preserve that. The stub's monitor commands also serve screenshots (`fi`/`fb`, fed by `gdbstub_frame_blit()` from `video_blit_memtoscreen_monitor()`) and inject keyboard/mouse input; mouse devices gate input on `mouse_capture || mouse_injected`, so a new mouse device must check both. Its interrupt log and catchpoints rely on every CPU core's `INT n` calling `gdbstub_int()` (386 interpreters via `x86_ops_int.h`, `808x.c`, `vx0.c`), and its record of what a hardware interrupt interrupted on each core calling `gdbstub_irq()` when it takes one (`386.c`, `386_dynarec.c`, `808x.c`, `vx0.c`), so a new core must too. Verify with `tools/ai-debugger/smoke_test.py` against a running VM.
