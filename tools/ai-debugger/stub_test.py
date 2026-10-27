"""Checks of the stub's execution hooks and connection handling, on a 386
or later VM at a DOS prompt (real mode): logpoints and breakpoints on a REP
instruction, logpoint EFLAGS, a false conditional watchpoint beside an
unconditional one, wrapping watch ranges, breaks during a streamed
screenshot, a break right after a continue, cleanup when a client leaves,
and run_tool.py leaving a running CPU running.

    python3 stub_test.py [PORT]      (default: BOX86_GDB_PORT or 12345)

It runs scratch code at 8000:0000 with interrupts off and leaves the guest
spinning there: restart the VM afterwards.
"""
import os
import struct
import subprocess
import sys
import threading
import time

TOOLS = os.path.dirname(os.path.abspath(__file__))
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("BOX86_GDB_PORT", "12345"))
sys.path.insert(0, TOOLS)
from gdb_rsp import BP_HARDWARE, WP_ACCESS, WP_WRITE, GdbClient, GdbError  # noqa: E402

BASE = 0x80000
CODE = bytes.fromhex(
    "B90020"   # 00 mov cx,2000h
    "BF0010"   # 03 mov di,1000h
    "B0AA"     # 06 mov al,0AAh
    "F3AA"     # 08 rep stosb        (ES=8000: writes 81000..82FFF)
    "3CAA"     # 0A cmp al,0AAh      (ZF=1)
    "90"       # 0C nop
    "EBFE")    # 0D jmp $
REP, NOP, JMP = BASE + 8, BASE + 0xC, BASE + 0xD
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print("%s %s %s" % ("PASS" if ok else "FAIL", name, detail))


def connect():
    c = GdbClient("127.0.0.1", PORT, timeout=15)
    c.connect()
    return c


def setup(c):
    if c.running:
        c.pause(5)
    c.write_memory(BASE, CODE)
    c.write_memory(BASE + 0x1000, bytes(0x2000))
    for r, v in (("cs", 0x8000), ("es", 0x8000), ("ds", 0x8000)):
        c.write_register(r, v)
    c.write_register("eflags", 0x0002)  # IF off: no interrupts in the way
    c.write_register("eip", BASE)


def run_to_jmp(c, timeout=10):
    c.set_point(BP_HARDWARE, JMP)
    c.resume()
    stop = c.wait_stop(timeout)
    c.clear_point(BP_HARDWARE, JMP)
    return stop


c = connect()

# 1. Logpoints on a REP instruction and the one after it: one hit each per run,
#    and the record's EFLAGS: ZF from the cmp (lazy flags rebuilt), bit 1 set.
setup(c)
c.logpoints("off")
c.logpoints("%x" % REP)
c.logpoints("%x" % NOP)
st0 = c.logpoints()
stop = run_to_jmp(c)
st = c.logpoints()
hits = {p[0]: p[3] for p in st["points"]}
check("REP logpoint counts one execution", hits.get(REP) == 1, "hits=%s" % hits.get(REP))
check("logpoint after REP counts one", hits.get(NOP) == 1, "hits=%s" % hits.get(NOP))
raw = c.hit_log(st0.get("next", 0))
size = st["record"]
recs = [raw[i:i + size] for i in range(0, len(raw) - size + 1, size)]
nop_recs = [r for r in recs if struct.unpack_from("<I", r, 4)[0] == NOP]
if nop_recs:
    efl = struct.unpack_from("<I", nop_recs[-1], 48)[0]
    check("logpoint EFLAGS has ZF from cmp and bit 1", (efl & 0x40) and (efl & 0x2) and not (efl & 0x200),
          "eflags=%08X" % efl)
else:
    check("logpoint EFLAGS record present", False)
c.logpoints("off")

# 2. A hardware breakpoint on a REP instruction: after continuing from it, the
#    CPU doesn't stop again between passes of the same instruction.
setup(c)
c.set_point(BP_HARDWARE, REP)
c.resume()
s1 = c.wait_stop(10)
c.clear_point(BP_HARDWARE, REP)  # cleared after the first stop...
c.set_point(BP_HARDWARE, REP)    # ...and set again: it must not fire mid-instruction
c.set_point(BP_HARDWARE, JMP)
c.resume()
s2 = c.wait_stop(10)
regs = c.read_registers()
c.clear_point(BP_HARDWARE, REP)
c.clear_point(BP_HARDWARE, JMP)
check("hw breakpoint at REP stops once", s1 and s1.regs.get("eip") == REP and s2 and s2.regs.get("eip") == JMP,
      "first=%s second=%s cx=%X" % (s1 and hex(s1.regs.get("eip", 0)), s2 and hex(s2.regs.get("eip", 0)),
                                   regs["ecx"] & 0xFFFF))

# 3. A conditional write watchpoint whose condition is false doesn't hide an
#    unconditional access watchpoint on the same byte.
setup(c)
c.set_point(WP_WRITE, BASE + 0x1000, 1)
c.set_condition(WP_WRITE, BASE + 0x1000, (1, 0, BASE + 0x1000, 1, 0, 0x55))  # byte == 55h: never
c.set_point(WP_ACCESS, BASE + 0x1000, 1)
c.set_point(BP_HARDWARE, JMP)
c.resume()
s = c.wait_stop(10)
for k in (WP_WRITE, WP_ACCESS):
    c.clear_point(k, BASE + 0x1000, 1)
c.clear_point(BP_HARDWARE, JMP)
check("unconditional watchpoint next to a false conditional one stops", s and s.reason == "watchpoint",
      s and s.describe())

# 4. A watchpoint range that wraps past 4 GB is refused, and the stub lives on.
r = c.request("Z2,fffffffe,4")
r0 = c.request("Z2,0,0")
alive = c.read_memory(BASE, 4) == CODE[:4]
check("wrapping and empty watch ranges refused", r.startswith("E") and r0.startswith("E") and alive,
      "replies %s %s" % (r, r0))

# 5. A break (0x03) during a streamed screenshot doesn't deadlock the stub.
setup(c)
c.write_register("eip", JMP)  # spin on jmp $
ok = True
for i in range(12):
    c.resume()
    t = threading.Timer(0.002 * i, lambda: c.sock.sendall(b"\x03"))
    t.start()
    try:
        c.monitor("fb", raw=True)
    except GdbError as e:
        ok = False
        print("   fb failed:", e)
        break
    t.join()
    if not c.wait_stop(5):
        ok = False
        print("   no stop after break", i)
        break
check("break during a streamed screenshot", ok)

# 6. 'c' followed at once by a break gets a fresh stop reply (pause), not the
#    previous stop's reason again.
setup(c)
s = run_to_jmp(c)  # stopped on a hw breakpoint
before = c.stub_state()["stops"]
with c.lock:
    c._drain_stops()
    c.running = True
    c.sock.sendall(b"$c#63\x03")
s = c.wait_stop(5)
after = c.stub_state()["stops"]
check("break right after continue reports a pause", s and s.reason == "pause" and after == before + 1,
      "%s, stops %s -> %s" % (s and s.describe(), before, after))

# Leave the guest spinning with interrupts on, so that the disconnect checks
# see a live CPU (the BIOS timer still ticks).
c.write_register("eflags", 0x0202)
c.close()
time.sleep(0.5)

# 7. Disconnecting with a watchpoint armed that fires right after: the stub
#    still resumes the CPU and drops the point.
c = connect()
was = c.attached_running
c.set_point(WP_WRITE, 0x46C, 2)  # BIOS timer tick (IRQ 0 runs: IF is on, though the CPU spins)
c.resume()
c.close()
time.sleep(1.0)
c = connect()
pts = c.list_points()
check("after disconnect the CPU runs and points are gone", c.attached_running and pts == [],
      "attached_running=%s (before: %s) points=%s" % (c.attached_running, was, pts))
c.close()

# 8. run_tool leaves a running CPU running.
out = subprocess.run([sys.executable, TOOLS + "/run_tool.py", "--port", str(PORT), "status"],
                     capture_output=True, text=True).stdout
c = connect()
check("run_tool status leaves the CPU running", "RUNNING" in out and c.attached_running, out.splitlines()[0])
c.close()

print("RESULT: %s (%d/%d)" % ("PASS" if all(results) else "FAIL", sum(map(bool, results)), len(results)))
sys.exit(0 if all(results) else 1)
