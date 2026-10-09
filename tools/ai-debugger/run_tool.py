"""Run one bridge tool from the command line, without an MCP client.

    python3 run_tool.py [--port 12345] TOOL [key=value ...]

Calls the same functions the MCP server exposes (see server.py), in this
process, then exits. Values are parsed as JSON when possible ("5", "true",
"\"0040:006C\"") and used as strings otherwise. Screenshots are written to
a PNG file whose path is printed. Exits 1 if the tool raised an error.

Each run opens and closes its own debugger connection. The stub pauses the
CPU when a client connects; this script asks it to "hold", so a CPU that is
paused when the script ends stays paused, and one left running (resume,
input tools) keeps running. Scripts, tests and agents without MCP support
can therefore drive the bridge one command at a time.
"""

import argparse
import asyncio
import inspect
import json
import os
import re
import signal
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def parse_value(text):
    """JSON when it parses ("5", "true", "\"x\""), otherwise the string with
    \\n, \\t and \\\\ escapes turned into newline, tab and backslash (so
    text="DIR\\n" types Enter, as it would through MCP)."""
    # Only JSON that can't be a hex number or address: 4E00 or 1e3 stay strings.
    if not re.fullmatch(r"-?\d+|-?\d+\.\d+|true|false|null|[\[{\"].*", text, re.S):
        return re.sub(r"\\([nt\\])", lambda m: {"n": "\n", "t": "\t", "\\": "\\"}[m.group(1)], text)
    try:
        return json.loads(text)
    except ValueError:
        return re.sub(r"\\([nt\\])", lambda m: {"n": "\n", "t": "\t", "\\": "\\"}[m.group(1)], text)


RAW_TEXT = {}  # parameter -> the text as typed (escapes processed), for str parameters


def _unescape(text):
    return re.sub(r"\\([nt\\])", lambda m: {"n": "\n", "t": "\t", "\\": "\\"}[m.group(1)], text)


def coerce(fn, kwargs):
    """Match values to the tool's annotated parameter types, as MCP would."""
    raw = RAW_TEXT
    sig = inspect.signature(inspect.unwrap(fn))
    out = {}
    aliases = {"count": ("entries", "length", "limit"), "length": ("count", "entries"), "entries": ("count",),
               "name": ("query", "address", "map_path"), "path": ("save_path", "map_path"),
               "data": ("hex_bytes",), "bytes": ("hex_bytes",), "hex": ("hex_bytes", "pattern"),
               "addr": ("address",), "timeout": ("timeout_seconds",), "address": ("target", "map_path"),
               "target": ("address",), "register": ("name",), "reg": ("name",), "text": ("pattern",),
               "map": ("map_path",), "file": ("map_path", "path"), "seconds": ("timeout_seconds",)}
    for k, v in list(kwargs.items()):
        if k not in sig.parameters:
            alt = next((a for a in aliases.get(k, ()) if a in sig.parameters and a not in kwargs), None)
            if alt:
                del kwargs[k]
                kwargs[alt] = v
                if k in raw:
                    raw[alt] = raw.pop(k)
    for k, v in kwargs.items():
        if k not in sig.parameters:
            raise SystemExit("%s has no parameter %r; parameters: %s" % (fn.__name__, k, ", ".join(sig.parameters)))
        ann = sig.parameters[k].annotation
        if ann is str and not isinstance(v, str):
            # Keep exactly what was typed ("1.50" stays "1.50", "null" stays "null").
            v = raw.get(k, json.dumps(v) if isinstance(v, bool) else str(v))
        elif ann is int and isinstance(v, str):
            try:
                v = int(v, 0)  # "0xB0" or "176"
            except ValueError:
                raise SystemExit("%s=%r: expected an integer (decimal, or hex with 0x)" % (k, v))
        elif ann is float and isinstance(v, int) and not isinstance(v, bool):
            v = float(v)
        out[k] = v
    missing = [n for n, prm in sig.parameters.items() if prm.default is inspect.Parameter.empty and n not in out]
    if missing:
        raise SystemExit("%s needs %s; parameters: %s" % (fn.__name__, ", ".join("%s=..." % m for m in missing),
                                                         ", ".join(sig.parameters)))
    return out


async def call(tool_name, kwargs, image_dir):
    import server  # noqa: E402  (reads BOX86_GDB_PORT on first connection)

    tools = {t.name: t for t in server.mcp._tool_manager.list_tools()}
    names = sorted(tools)
    if tool_name == "help":
        # help: the server instructions and a line per tool; help TOOL: its full description.
        wanted = list(kwargs)
        if wanted:
            for name in wanted:
                if name not in tools:
                    raise SystemExit("unknown tool %r" % name)
                fn = getattr(server, name)
                print("%s(%s)\n\n%s\n" % (name, ", ".join(str(p) for p in inspect.signature(fn).parameters.values()),
                                          inspect.cleandoc(tools[name].description or "")))
        else:
            print(server.INSTRUCTIONS)
            for name in names:
                params = ", ".join(inspect.signature(getattr(server, name)).parameters)
                print("%s(%s)\n    %s" % (name, params, (tools[name].description or "").strip().split("\n")[0]))
            print("\nrun_tool.py help TOOL shows a tool's parameters and full description.")
        return
    if tool_name not in names:
        raise SystemExit("unknown tool %r; tools: %s" % (tool_name, ", ".join(names)))
    fn = getattr(server, tool_name)
    kwargs = coerce(fn, kwargs)
    server.client().set_hold(True)
    result = await fn(**kwargs)
    items = result if isinstance(result, list) else [result]
    for item in items:
        data = getattr(item, "data", None)
        if isinstance(data, (bytes, bytearray)):
            path = os.path.join(image_dir, "screen_%d.png" % int(time.time() * 1000))
            with open(path, "wb") as f:
                f.write(data)
            print("[image saved to %s]" % path)
        elif isinstance(item, (dict, list)):
            print(json.dumps(item, indent=2, ensure_ascii=False))
        else:
            print(item)


def main():
    if hasattr(signal, "SIGPIPE"):  # exit quietly when piped into head and the like
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=int(os.environ.get("BOX86_GDB_PORT", "12345")))
    ap.add_argument("--host", default=os.environ.get("BOX86_GDB_HOST", "127.0.0.1"))
    ap.add_argument("--image-dir", default=os.path.join(tempfile.gettempdir(), "86box-screens"))
    ap.add_argument("tool")
    ap.add_argument("args", nargs="*", help="key=value")
    a = ap.parse_args()

    os.environ["BOX86_GDB_PORT"] = str(a.port)
    os.environ["BOX86_GDB_HOST"] = a.host
    os.makedirs(a.image_dir, exist_ok=True)
    kwargs = {}
    if a.tool == "help":  # "help TOOL..." takes bare tool names
        a.args = [n + "=" for n in a.args]
    for kv in a.args:
        if "=" not in kv:
            raise SystemExit("arguments are key=value, got %r" % kv)
        k, v = kv.split("=", 1)
        kwargs[k] = parse_value(v)
        RAW_TEXT[k] = _unescape(v)
    try:
        asyncio.run(call(a.tool, kwargs, a.image_dir))
    except SystemExit:
        raise
    except Exception as e:  # report tool errors the way an MCP client would see them
        print("ERROR: %s" % e)
        sys.exit(1)


if __name__ == "__main__":
    main()
