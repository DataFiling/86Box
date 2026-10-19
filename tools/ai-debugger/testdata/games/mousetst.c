/* MOUSETST - shows the INT 33h mouse state as text, to check mouse input
 * end to end through a DOS mouse driver. Prints the driver's button count,
 * then continuously the position (in virtual-screen pixels), the buttons
 * held, the presses counted by function 5 and the wheel (CuteMouse wheel
 * API), until a key is pressed.
 *
 * Build (Open Watcom): wcl -ms -bt=dos -0 -os mousetst.c
 */

#include <conio.h>
#include <dos.h>
#include <i86.h>
#include <stdio.h>

int
main(void)
{
    union REGS    r;
    unsigned long left_presses = 0, right_presses = 0;
    int           wheel = 0, wheel_api;

    r.w.ax = 0;
    int86(0x33, &r, &r);
    if (r.w.ax != 0xFFFF) {
        printf("MOUSETST: no mouse driver\n");
        return 1;
    }
    printf("MOUSETST: driver present, %u buttons\n", r.w.bx);

    r.w.ax = 0x0011; /* CuteMouse wheel API: enable, returns 574Dh */
    int86(0x33, &r, &r);
    wheel_api = (r.w.ax == 0x574D);

    while (!kbhit()) {
        r.w.ax = 5; /* button press data: left */
        r.w.bx = 0;
        int86(0x33, &r, &r);
        left_presses += r.w.bx;
        r.w.ax = 5; /* right */
        r.w.bx = 1;
        int86(0x33, &r, &r);
        right_presses += r.w.bx;

        r.w.ax = 3;
        int86(0x33, &r, &r);
        if (wheel_api)
            wheel += (signed char) r.h.bh;
        printf("\rX=%3u Y=%3u buttons=%u%u%u left_presses=%lu right_presses=%lu wheel=%d   ",
               r.w.cx, r.w.dx, (r.w.bx >> 2) & 1, (r.w.bx >> 1) & 1, r.w.bx & 1,
               left_presses, right_presses, wheel);
        delay(50);
    }
    getch();
    printf("\n");
    return 0;
}
