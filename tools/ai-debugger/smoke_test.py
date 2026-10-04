"""End-to-end smoke test: drives server.py through a real MCP client.

Start 86Box (built with -DGDBSTUB=ON) on an IBM PC or AT-class machine with
no boot disk first, then run:  python3 smoke_test.py
Set BOX86_GDB_PORT if the stub is not on port 12345. Exits non-zero on failure.

With --protected-mode (386 or newer machines only), it also loads
testdata/pm_probe.bin, which switches to protected mode and loops through
32-bit and 16-bit code segments, checks addressing, disassembly, descriptors
and watchpoints there, and hard-resets the machine at the end.
"""

import asyncio
import os
import re
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = os.path.dirname(os.path.abspath(__file__))


async def _post_done(call):
    # The tick count at 0040:006C only advances once POST has enabled IRQ 0.
    a = await call("read_memory", address="0040:006C", length=2, format="words")
    await call("run_for", seconds=0.5)
    b = await call("read_memory", address="0040:006C", length=2, format="words")
    return a != b


async def protected_mode_checks(call, expect):
    probe = open(os.path.join(HERE, "testdata", "pm_probe.bin"), "rb").read()
    await call("pause")
    await call("write_memory", address="0000:8000", hex_bytes=probe.hex())
    await call("set_register", name="cs", value="0")
    await call("set_register", name="ip", value="8000")
    await call("run_for", seconds=1)

    out = await call("get_segments")
    expect("FS    0018  00012340  0000FFFF  data writable, 16-bit" in out, "PM: segment caches")
    out = await call("read_descriptor_table", table="gdt")
    expect("0020     code readable                base=00008100 limit=0000FFFF 16-bit" in out, "PM: GDT decoding")

    out = await call("run_until", address="0020:0000", timeout_seconds=3)
    expect("CS:IP = 0020:0000  (linear 00008100)" in out and "16-bit code" in out, "PM: 16-bit code segment")
    out = await call("disassemble", address="cs:ip", count=1)
    expect("inc word ptr fs:[0]" in out, "PM: 16-bit disassembly")

    out = await call("run_until", address="0008:802C", timeout_seconds=3)
    expect("CS:EIP = 0008:0000802C" in out and "32-bit code" in out, "PM: 32-bit code segment")
    expect("inc dword ptr [0x806e]" in out, "PM: 32-bit disassembly")
    out = await call("read_stack", entries=2)
    expect(out.startswith("0010:0009F000"), "PM: 32-bit stack")

    a = await call("read_memory", address="fs:0", length=16)
    b = await call("read_memory", address="0018:0000", length=16)
    expect(a.startswith("0018:0000") and a == b, "PM: selector lookup")

    await call("set_watchpoint", address="fs:0", length=2, kind="write")
    out = await call("run_for", seconds=3)
    expect("watchpoint triggered (write access at linear 00012340)" in out, "PM: watchpoint through FS")
    await call("clear_breakpoint", address="all")
    await call("hard_reset")


async def main():
    params = StdioServerParameters(command=sys.executable, args=[os.path.join(HERE, "server.py")],
                                   cwd=HERE, env=dict(os.environ))
    failures = []

    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print("TOOLS:", ", ".join(t.name for t in tools.tools))

            async def call(name, /, **args):
                res = await s.call_tool(name, args)
                parts = []
                for c in res.content:
                    if getattr(c, "type", "") == "image":
                        parts.append("[%s image, %d base64 chars]" % (c.mimeType, len(c.data)))
                    else:
                        parts.append(getattr(c, "text", str(c)))
                text = "\n".join(parts)
                print("\n>>> %s %s%s\n%s" % (name, args, "  [ERROR]" if res.isError else "", text[:1500]))
                if res.isError:
                    failures.append(name)
                return text

            def expect(cond, what):
                if not cond:
                    failures.append(what)

            await call("status")
            # Let the BIOS finish POST (the timer interrupt is only enabled late).
            for _ in range(10):
                await call("run_for", seconds=3)
                if await _post_done(call):
                    break

            # The BIOS timer interrupt (INT 08h) increments the tick count at 0040:006C.
            await call("set_watchpoint", address="0040:006C", length=2, kind="write")
            out = await call("run_for", seconds=3)
            expect("watchpoint triggered" in out, "tick-count write watchpoint")
            await call("clear_breakpoint", address="all")

            vec = await call("read_interrupt_vectors", first="8", count=1)
            handler = re.search(r"-> ([0-9A-F]{4}:[0-9A-F]{4})", vec).group(1)
            out = await call("run_until", address=handler, timeout_seconds=3)
            expect("hit breakpoint" in out and handler in out, "breakpoint at INT 08h handler")

            await call("step", count=3)
            await call("step_over")
            await call("disassemble", address="cs:ip", count=5)
            await call("read_stack", entries=4)
            await call("read_memory", address="0040:006C", length=4, format="words")
            await call("search_memory", pattern="55 AA", start="C0000", end="F0000", max_results=4)
            out = await call("read_text_screen")
            expect('"mode"' in out, "text screen")
            await call("io_read", port="40", count=4)

            out = await call("screenshot")
            expect("image/png image" in out, "screenshot")
            out = await call("screenshot", downscale=2)
            expect("image/png image" in out, "downscaled screenshot")
            await call("press_keys", keys="shift")  # harmless at any prompt
            await call("mouse_move", dx=5, dy=5)
            await call("mouse_click", button="left")
            await call("list_breakpoints")
            if "--protected-mode" in sys.argv:
                await protected_mode_checks(call, expect)
            await call("resume")

    print("\nRESULT:", "FAIL " + ", ".join(failures) if failures else "PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
