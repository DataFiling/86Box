; Protected-mode probe for the AI debugger's smoke test.
;
; Loaded by the debugger at linear 8000h and entered at 0000:8000 in real
; mode. It switches to protected mode and then loops forever between
; 32-bit code in a flat segment and 16-bit code in a segment whose base is
; not zero, so that the debugger has to use descriptor bases to translate
; SEG:OFF and to pick 16- or 32-bit disassembly. Interrupts stay disabled.
;
; Build: nasm -f bin -o pm_probe.bin pm_probe.asm

CODE16_BASE equ 0x8100             ; base of the 16-bit code segment (selector 20h)
DATA_BASE   equ 0x12340            ; base of the 16-bit data segment (selector 18h)

        bits 16
        org 0x8000

start:
        cli
        xor ax, ax
        mov ds, ax
        lgdt [gdt_ptr]
        mov eax, cr0
        or al, 1
        mov cr0, eax
        jmp 0x08:pm32

        bits 32
pm32:
        mov ax, 0x10
        mov ds, ax
        mov es, ax
        mov ss, ax
        mov esp, 0x9f000
        mov ax, 0x18
        mov fs, ax
loop32:
        inc dword [counter32]
        jmp 0x20:(code16 - CODE16_BASE)

        align 8
gdt:
        dq 0                                        ; 00h: null
        dw 0xffff, 0x0000                           ; 08h: 32-bit code, base 0, 4 GiB
        db 0x00, 0x9a, 0xcf, 0x00
        dw 0xffff, 0x0000                           ; 10h: 32-bit data, base 0, 4 GiB
        db 0x00, 0x92, 0xcf, 0x00
        dw 0xffff, DATA_BASE & 0xffff               ; 18h: 16-bit data, base DATA_BASE, 64 KiB
        db (DATA_BASE >> 16) & 0xff, 0x92, 0x00, DATA_BASE >> 24
        dw 0xffff, CODE16_BASE & 0xffff             ; 20h: 16-bit code, base CODE16_BASE, 64 KiB
        db (CODE16_BASE >> 16) & 0xff, 0x9a, 0x00, CODE16_BASE >> 24
gdt_end:

gdt_ptr:
        dw gdt_end - gdt - 1
        dd gdt

counter32:
        dd 0

        times (CODE16_BASE - 0x8000) - ($ - $$) db 0

        bits 16
code16:                                             ; runs at 0020:0000
        inc word [fs:0]                             ; 16-bit counter at linear DATA_BASE
        jmp 0x08:loop32
