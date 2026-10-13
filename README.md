# 86Box game VM kit (for the AI debugger)

A ready-to-run 86Box virtual machine for debugging DOS games with the AI
debugger in `tools/ai-debugger` (branch `claude/amazing-noether-mnz5kw`).
It is `tools/ai-debugger/testdata/make_game_vm.sh`'s output, prebuilt so you
don't need to build the DOS tools (Open Watcom, FreeDOS) yourself.

```sh
git fetch origin vm-kit
git show origin/vm-kit:game-vm.tar.xz | tar -xJ -C ~/86box-vms   # makes ~/86box-vms/game-vm
```

SHA-256 of game-vm.tar.xz: 57dec3818bfecbc085a4b02afc5176efaa9aa01783b4ee8513a034da566bb471

## What's in game-vm/

| File | What |
|---|---|
| `86box.cfg` | ASUS P/I-P55T2P4, Pentium MMX 200, 32 MB, VGA, PS/2 wheel mouse, Sound Blaster 16 at 220h IRQ 7 DMA 1/5 (DOSBox's settings, which GOG's DOS games are configured for), debugger port 12345 |
| `boot.img` | FreeDOS boot floppy: kernel, FreeCOM, JemmEx (EMS/XMS), CuteMouse, BLASTER set; ends at `C:\>`. Also small test programs (TXTGAME, VGAGAME, DATAGAME, MOUSETST) |
| `hd.img` | Empty 499 MB FAT16 hard disk, C:, for games; partition at byte 32256 (`gog_import.py ... --offset 32256`) |
| `nvr/p55t2p4.nvr` | CMOS settings (floppy drive, IDE auto-detect), so the first boot needs no BIOS setup |

Run it with an 86Box built with `-DGDBSTUB=ON`: `86Box -P ~/86box-vms/game-vm -R ~/86box-roms`.
The machine needs the 86Box ROM set (github.com/86Box/roms).

## Licenses and sources

The floppy's programs are free software, built from these sources:

| Program | License | Source |
|---|---|---|
| FreeDOS kernel (KERNEL.SYS) | GPL 2 (licenses/GPL-2.0.txt) | https://github.com/FDOS/kernel at d6791ad |
| FreeCOM (COMMAND.COM) | GPL 2 | https://github.com/FDOS/freecom at 04fc21a |
| CuteMouse (CTMOUSE.COM) | GPL 2 | https://github.com/FDOS/mouse at dd26852 |
| JemmEx (JEMMEX.EXE) | Artistic License (licenses/Artistic-JemmEx.txt) | https://github.com/Baron-von-Riedesel/jemm at e96bb6b |
| Test programs | GPL 2, as 86Box | `tools/ai-debugger/testdata/games` on the debugger branch, built with Open Watcom v2 (02e9386c) |

The build scripts are in `tools/ai-debugger/testdata` (`build_testbed.sh`,
`build_emm_floppy.sh`, `make_game_vm.sh`).
