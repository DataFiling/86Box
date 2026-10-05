# Bug report for viti95/FastDoom (draft, to file at https://github.com/viti95/FastDoom/issues)

**Title:** Timer interrupt handler (`TS_ServiceSchedule`) corrupts DOS memory when IRQ0 interrupts real-mode code

**Body:**

`TS_ServiceSchedule` and `TS_ServiceScheduleIntEnabled` in `FASTDOOM/ns_task.c`
switch to their own stack with `SetStack(StackSelector, StackPointer)`, then
keep using their locals `ptr` and `next`. Open Watcom addresses those through
EBP, and EBP is still an offset into the *interrupted* stack. The new stack is
zero-based, so the locals are read and written at linear address EBP-8/EBP-4.

When IRQ0 arrives while the CPU is in protected mode on the game's flat stack,
that is harmless: the old stack is zero-based too. But when it arrives while the
CPU is in real mode (inside DOS or the BIOS, e.g. while loading the WAD), DOS/4GW
runs the handler on its own interrupt stack, whose base isn't 0 (selector 00B0h,
base 143DF0h in our runs). EBP is then something like 4CA4h, and the handler
writes `ptr`/`next` (the address of `HeadTask`) over linear 4C9Ch-4CA3h: low DOS
memory.

**Effect seen:** with the FreeDOS kernel (current git, 8086 FAT32 build) those
addresses are the kernel's disk buffers. Depending on the timing, a buffer header or a cached FAT sector gets
`A0 FD 20 00 A0 FD 20 00` (twice `&HeadTask`). FreeDOS prints "Run chkdsk: Bad
FAT value/index" and loops forever in its buffer search on a later file call, so
FastDoom hangs during startup, after setting mode 13h. Other DOS versions keep
other data there, so the symptom will vary or stay hidden (e.g. with buffers in
the HMA).

**How it was found:** FastDoom at commit 9a7b435 (`fdoom.exe -debug`, DOS/4GW
1.97, Freedoom 0.13 `freedm1.wad`) on FreeDOS in the 86Box emulator (Pentium MMX
machine), with a write watchpoint over the DOS buffers limited to
protected-mode accesses. It stopped at `TS_ServiceSchedule_+4Eh`, the
`mov [ebp-8],eax` that stores `ptr = TaskList->next` right after the stack
switch, with EBP=00004CA4. The saved old stack was 00B0:00004C98, and the
descriptor for 00B0 has base 00143DF0.

**Fix:** keep the locals off the EBP frame, e.g. make them `static`. That is
safe because neither handler re-enters that code: `TS_ServiceSchedule` runs
with interrupts disabled, and `TS_ServiceScheduleIntEnabled` returns before
touching them while `TS_InInterrupt` is set.

```diff
 static void __interrupt __far TS_ServiceSchedule(void)
 {
-    task *ptr;
-    task *next;
+    static task *ptr; // static: the stack switch below changes SS but not EBP
+    static task *next;
```

(and the same in `TS_ServiceScheduleIntEnabled`). With this change the hang
is gone and the game loads and runs its demos.
