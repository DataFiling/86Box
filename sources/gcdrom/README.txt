Source of GCDROM.SYS on the kit's floppy: XCDROM.ASM and its English
messages from https://gitlab.com/FreeDOS/drivers/gcdrom (commit
815078126e1332ec0917536043d973407e1008c1, SOURCE/GCDROM), with
tools/ai-debugger/testdata/gcdrom-legacy-ide.diff applied (see the note
at the top of XCDROM.ASM). GPL 2 or later: licenses/GPL-2.0.txt.

Assemble with NASM:

    nasm -O9 -w-all -f bin -o GCDROM.SYS XCDROM.ASM
