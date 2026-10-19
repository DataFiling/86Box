; Hard disk MBR that boots drive A: instead (the data disk has no OS).
        org     0x600
        cli
        xor     ax, ax
        mov     ds, ax
        mov     es, ax
        mov     ss, ax
        mov     sp, 0x7c00
        sti
        mov     si, 0x7c00
        mov     di, 0x600
        mov     cx, 256
        rep     movsw
        jmp     0:go
go:     mov     di, 3                   ; retries
.read:  mov     ax, 0x0201              ; read 1 sector
        mov     bx, 0x7c00
        mov     cx, 1                   ; cylinder 0, sector 1
        xor     dx, dx                  ; head 0, drive 0 (A:)
        int     0x13
        jnc     .boot
        xor     ax, ax                  ; reset and retry
        int     0x13
        dec     di
        jnz     .read
        int     0x18                    ; no floppy: let the BIOS give up
.boot:  xor     dx, dx                  ; boot drive A:
        jmp     0:0x7c00
        times   0x1be-($-$$) db 0
