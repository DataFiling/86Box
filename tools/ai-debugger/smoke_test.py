"""End-to-end smoke test: drives server.py through a real MCP client.

Start 86Box (built with -DGDBSTUB=ON) on an IBM PC or AT-class machine with
no boot disk first, then run:  python3 smoke_test.py
Set BOX86_GDB_PORT if the stub is not on port 12345. Exits non-zero on failure.
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


async def main():
    params = StdioServerParameters(command=sys.executable, args=[os.path.join(HERE, "server.py")],
                                   cwd=HERE, env=dict(os.environ))
    failures = []

    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print("TOOLS:", ", ".join(t.name for t in tools.tools))

            async def call(name, **args):
                res = await s.call_tool(name, args)
                text = "\n".join(getattr(c, "text", str(c)) for c in res.content)
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
            await call("list_breakpoints")
            await call("resume")

    print("\nRESULT:", "FAIL " + ", ".join(failures) if failures else "PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
