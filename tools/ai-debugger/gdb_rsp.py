"""Minimal GDB Remote Serial Protocol client for the 86Box GDB stub.

Notes on 86Box's stub (src/gdbstub.c) that this client relies on:

* The emulated CPU starts paused when the stub is enabled and resumes
  automatically when the last client disconnects.
* All addresses (EIP, memory, breakpoints, watchpoints) are linear
  addresses: in real mode that is segment * 16 + offset.
* Every response must be acknowledged with '+' before the stub sends the
  next one.
* 'c' and 's' get no immediate reply; a stop reply ("T..") arrives when the
  CPU stops (breakpoint, watchpoint, single-step done, or 0x03 break).
* Packets are serviced between CPU time slices, so memory can be read and
  written while the guest is running.
"""

import queue
import socket
import struct
import threading

# Register indices (enum in src/gdbstub.c).
REG_NAMES = ["eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi", "eip", "eflags",
             "cs", "ss", "ds", "es", "fs", "gs"]
REG_CR0, REG_CR2, REG_CR3, REG_CR4 = 16, 17, 18, 19

BP_SOFTWARE, BP_HARDWARE, WP_WRITE, WP_READ, WP_ACCESS = range(5)

SIGNALS = {2: "SIGINT (pause requested)", 5: "SIGTRAP (breakpoint/step/watchpoint)"}


class GdbError(Exception):
    pass


class StopEvent:
    """A parsed stop reply such as T05hwbreak:;0:...;"""

    def describe(self):
        if self.reason == "watchpoint":
            return "watchpoint triggered (%s access at linear %08X)" % (self.watch_kind, self.watch_addr)
        if self.reason == "catch":
            return "caught INT %02Xh %s" % (self.int_vector, "returning" if self.int_return else "call (not executed yet)")
        if self.reason == "range":
            return "execution entered a watched range at linear %08X" % self.range_addr
        return {"breakpoint": "hit breakpoint", "pause": "paused",
                "trap": "stopped (single step)"}.get(self.reason, self.reason)

    def __init__(self, packet):
        self.raw = packet
        self.signal = int(packet[1:3], 16) if packet[:1] in "TS" else None
        self.reason = "pause" if self.signal == 2 else "trap"
        self.watch_kind = None
        self.watch_addr = None
        self.int_vector = None  # catchpoint stops
        self.int_return = False
        self.int_seq = 0  # the call's INT log record, 0 if not logged
        self.range_addr = None
        self.regs = {}
        if packet.startswith("T"):
            for field in packet[3:].split(";"):
                if ":" not in field:
                    continue
                key, val = field.split(":", 1)
                if key in ("watch", "rwatch", "awatch"):
                    self.reason = "watchpoint"
                    self.watch_kind = {"watch": "write", "rwatch": "read", "awatch": "access"}[key]
                    self.watch_addr = int(val, 16)
                elif key in ("swbreak", "hwbreak"):
                    self.reason = "breakpoint"
                elif key in ("intcall", "intret"):
                    self.reason = "catch"
                    self.int_vector = int(val, 16)
                    self.int_return = key == "intret"
                elif key == "seq":
                    self.int_seq = int(val, 16)
                elif key == "xrange":
                    self.reason = "range"
                    self.range_addr = int(val, 16)
                else:
                    try:
                        idx = int(key, 16)
                    except ValueError:
                        continue
                    if idx < len(REG_NAMES) and len(val) >= 8:
                        self.regs[REG_NAMES[idx]] = int.from_bytes(bytes.fromhex(val[:8]), "little")


class GdbClient:
    def __init__(self, host="127.0.0.1", port=12345, timeout=10.0):
        self.host, self.port, self.timeout = host, port, timeout
        self.sock = None
        self.replies = queue.Queue()
        self.stops = queue.Queue()
        self.lock = threading.Lock()  # one request/response at a time
        self.running = False  # whether we told the CPU to run
        self.last_stop = None
        self._reader = None
        self._closed = threading.Event()

    # ---- connection -----------------------------------------------------
    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(None)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._closed.clear()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self.request("qSupported:swbreak+;hwbreak+")
        # The stub pauses the CPU when a client connects, unless an earlier
        # client asked it to hold the run state across connections.
        self.running = False
        if self.query_running():
            self.resume()  # re-arms stop reporting for this connection
        else:
            self.last_stop = self.query_stop()

    def stub_state(self):
        """The stub's "state": {"running": bool, "stops": count or None}
        ({"running": False} on builds without it)."""
        try:
            text = self.monitor("state")
        except GdbError as e:
            if "unknown" in str(e):
                return {"running": False, "stops": None}
            raise
        st = {"running": False, "stops": None}
        for line in text.splitlines():
            words = line.split()
            if len(words) == 2 and words[0] == "running":
                st["running"] = words[1] == "1"
            elif len(words) == 2 and words[0] == "stops":
                st["stops"] = int(words[1], 16)
        return st

    def query_running(self):
        """Whether the CPU is running (False on builds without "state")."""
        return self.stub_state()["running"]

    def query_stop(self):
        """Why the CPU is stopped, as a StopEvent, or None if the stub doesn't say."""
        with self.lock:
            self._expect_stop_reply = True
            try:
                self._send_packet("?")
                reply = self._get_reply()
            finally:
                self._expect_stop_reply = False
        return StopEvent(reply) if reply[:1] in ("T", "S") else None

    def close(self):
        if self.sock:
            # shutdown() first: close() alone doesn't send the FIN while the
            # reader thread is blocked in recv() on the socket, so the stub
            # wouldn't notice that this client has gone.
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self._closed.set()

    @property
    def connected(self):
        return self.sock is not None and not self._closed.is_set()

    def _read_loop(self):
        buf = b""
        try:
            while True:
                data = self.sock.recv(65536)
                if not data:
                    break
                buf += data
                while buf:
                    if buf[:1] in (b"+", b"-"):
                        buf = buf[1:]
                        continue
                    start = buf.find(b"$")
                    if start < 0:
                        buf = b""
                        break
                    end = buf.find(b"#", start)
                    if end < 0 or len(buf) < end + 3:
                        buf = buf[start:]
                        break
                    payload = buf[start + 1:end].decode("latin-1")
                    buf = buf[end + 3:]
                    self.sock.sendall(b"+")
                    self._dispatch(payload)
        except OSError:
            pass
        finally:
            self._closed.set()
            self.replies.put(None)
            self.stops.put(None)

    def _dispatch(self, payload):
        # Stop replies arrive asynchronously after 'c'/'s'/0x03. Ordinary
        # replies are hex data, "OK", "Exx", "O<hex>" or empty, never 'T'/'S'.
        if payload[:1] in ("T", "S") and not self._expect_stop_reply:
            stop = StopEvent(payload)
            self.last_stop = stop
            self.running = False
            self.stops.put(stop)
        else:
            self.replies.put(payload)

    _expect_stop_reply = False

    # ---- packet I/O -----------------------------------------------------
    def _send_packet(self, payload):
        if not self.connected:
            raise GdbError("not connected to the 86Box GDB stub")
        csum = sum(payload.encode("latin-1")) & 0xFF
        self.sock.sendall(b"$" + payload.encode("latin-1") + b"#%02x" % csum)

    def _get_reply(self, timeout=None):
        try:
            reply = self.replies.get(timeout=timeout or self.timeout)
        except queue.Empty:
            # A late reply would otherwise be taken as the answer to the next
            # request, so drop the connection; the next call reconnects.
            self.close()
            raise GdbError("timed out waiting for the emulator (is 86Box paused from its own UI?); "
                           "disconnected, the next call reconnects")
        if reply is None:
            raise GdbError("connection to 86Box closed")
        return reply

    def request(self, payload, timeout=None):
        with self.lock:
            self._send_packet(payload)
            return self._get_reply(timeout)

    # ---- execution control ---------------------------------------------
    def _drain_stops(self):
        while True:
            try:
                self.stops.get_nowait()
            except queue.Empty:
                return

    def resume(self):
        with self.lock:
            self._drain_stops()
            # Set before sending: the reader clears it when the stop reply comes.
            self.running = True
            self._send_packet("c")

    def step(self, timeout=None):
        with self.lock:
            self._drain_stops()
            self.running = True
            self._send_packet("s")
        return self.wait_stop(timeout)

    def pause(self, timeout=None):
        if not self.running:
            return self.last_stop
        self.sock.sendall(b"\x03")
        return self.wait_stop(timeout)

    def wait_stop(self, timeout=None):
        """Wait for the CPU to stop. Returns a StopEvent or None on timeout."""
        try:
            stop = self.stops.get(timeout=timeout if timeout is not None else self.timeout)
        except queue.Empty:
            return None
        if stop is None:
            raise GdbError("connection to 86Box closed")
        return stop

    # ---- registers ------------------------------------------------------
    def read_registers(self):
        reply = self.request("g")
        if len(reply) < 16 * 8:
            raise GdbError("bad register reply: %r" % reply[:40])
        regs = {}
        for i, name in enumerate(REG_NAMES):
            regs[name] = int.from_bytes(bytes.fromhex(reply[i * 8:i * 8 + 8]), "little")
        for name in ("cs", "ss", "ds", "es", "fs", "gs"):
            regs[name] &= 0xFFFF  # the stub leaves stale bytes in the upper half
        for idx, name in ((REG_CR0, "cr0"), (REG_CR2, "cr2"), (REG_CR3, "cr3"), (REG_CR4, "cr4")):
            regs[name] = self.read_register(idx)
        regs["seg"] = self.read_segments()
        return regs

    _has_sg = True

    def read_segments(self):
        """Segment descriptor caches and descriptor table registers from the
        stub's "sg" monitor command, or None on builds without it.

        Returns {"cs": {"sel", "base", "limit", "access", "flags"}, ..., "gdt",
        "idt", "ldt", "tr", "cpu": {"use32", "stack32", "cpl"}}."""
        if not self._has_sg:
            return None
        try:
            text = self.monitor("sg")
        except GdbError as e:
            if "unknown" not in str(e):
                raise
            self._has_sg = False
            return None
        out = {}
        for line in text.splitlines():
            parts = line.split()
            if not parts:
                continue
            fields = {}
            for kv in parts[1:]:
                key, val = kv.split("=", 1)
                fields[key] = int(val, 10 if parts[0] == "cpu" else 16)
            out[parts[0]] = fields
        return out

    def read_register(self, index):
        reply = self.request("p%x" % index)
        if reply.startswith("E") or not reply:
            raise GdbError("cannot read register %d: %s" % (index, reply))
        return int.from_bytes(bytes.fromhex(reply), "little")

    def write_register(self, name, value):
        index = REG_NAMES.index(name)
        width = 2 if name in ("cs", "ss", "ds", "es", "fs", "gs") else 4
        reply = self.request("P%x=%s" % (index, value.to_bytes(width, "little").hex()))
        if reply != "OK":
            raise GdbError("cannot write %s: %s" % (name, reply))

    # ---- memory -----------------------------------------------------------
    CHUNK = 4096

    def read_memory(self, addr, length):
        """Read guest memory. Returns fewer bytes than asked if the range runs
        into a page the CPU can't read (the stub returns what it read before
        the fault); raises only if nothing at all could be read."""
        out = bytearray()
        addr &= 0xFFFFFFFF
        while length > 0:
            n = min(length, self.CHUNK, 0x100000000 - addr)
            reply = self.request("m%x,%x" % (addr, n))
            if reply.startswith("E") and len(reply) == 3:
                if out:
                    break
                raise GdbError("memory read failed at %08X (%s; page fault?)" % (addr, reply))
            try:
                data = bytes.fromhex(reply)
            except ValueError:
                raise GdbError("unexpected reply to a memory read at %08X: %r" % (addr, reply[:40])) from None
            out += data
            if len(data) < n:  # partial read, e.g. page fault mid-way
                break
            addr += n
            length -= n
        return bytes(out)

    def write_memory(self, addr, data):
        for off in range(0, len(data), self.CHUNK):
            chunk = data[off:off + self.CHUNK]
            reply = self.request("M%x,%x:%s" % (addr + off, len(chunk), chunk.hex()))
            if reply != "OK":
                raise GdbError("memory write failed at %08X: %s" % (addr + off, reply))

    # ---- breakpoints ------------------------------------------------------
    def set_point(self, kind, addr, length=1):
        reply = self.request("Z%d,%x,%x" % (kind, addr, length))
        if reply != "OK":
            raise GdbError("could not set point at %08X: %s" % (addr, reply or "unsupported"))

    def list_points(self):
        """Breakpoints/watchpoints the stub holds, as [(kind, addr, length)], or
        None on builds without the "bl" monitor command."""
        try:
            text = self.monitor("bl")
        except GdbError as e:
            if "unknown" in str(e):
                return None
            raise
        points = []
        self.point_hit_counts = {}
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 3:
                points.append((int(parts[0]), int(parts[1], 16), int(parts[2], 16)))
                if len(parts) >= 4:  # newer builds: times it stopped the CPU
                    self.point_hit_counts[(int(parts[0]), int(parts[1], 16))] = int(parts[3], 16)
        return points

    def clear_point(self, kind, addr, length=1):
        reply = self.request("z%d,%x,%x" % (kind, addr, length))
        if reply != "OK":
            raise GdbError("could not clear point at %08X: %s" % (addr, reply or "not set"))

    # ---- monitor commands (I/O ports, reset) ----------------------------
    def set_hold(self, on):
        """Ask the stub to keep a paused CPU paused when this client
        disconnects (instead of resuming it). Returns False on 86Box builds
        without the "hold" monitor command."""
        try:
            self.monitor("hold %d" % (1 if on else 0))
            return True
        except GdbError as e:
            if "unknown" in str(e):
                return False
            raise

    def monitor(self, command, raw=False):
        """Run a stub monitor command; returns its text output (bytes if raw)."""
        with self.lock:
            self._send_packet("qRcmd," + command.encode().hex())
            parts = []
            while True:
                reply = self._get_reply()
                if reply.startswith("O") and reply != "OK":
                    parts.append(bytes.fromhex(reply[1:]))
                    continue
                if reply.startswith("E") and len(reply) == 3:
                    raise GdbError("monitor command %r failed (%s); is this 86Box build current?" % (command, reply))
                if reply and reply != "OK":
                    parts.append(bytes.fromhex(reply))
                out = b"".join(parts)  # one join: replies can be megabytes in many packets
                if out.rstrip(b"\x00") == b"Unknown command\n":
                    raise GdbError("monitor command %r unknown to this 86Box build" % command)
                if raw:
                    return out
                return out.rstrip(b"\x00").decode("latin-1")  # some text replies include the C string's NUL

    # ---- INT log, catchpoints, range catch, raw memory (86Box stub extensions)
    def peek(self, addr, length):
        """Read RAM/ROM without side effects (device memory and unmapped pages
        are skipped). Returns [(address, bytes or None)] runs covering the range."""
        raw = self.monitor("mr 0x%x 0x%x" % (addr & 0xFFFFFFFF, length), raw=True)
        runs, pos = [], 0
        while pos + 9 <= len(raw):
            readable = raw[pos]
            run_addr, n = struct.unpack_from("<II", raw, pos + 1)
            pos += 9
            if readable:
                runs.append((run_addr, raw[pos:pos + n]))
                pos += n
            else:
                runs.append((run_addr, None))
        return runs

    def int_status(self):
        """Parse "ts": {"vectors": [...], "first", "next", "record", "pending",
        "catches": [(vector, ah, al, when)], "ranges": [(lo, hi)], "tsc", "hz"}."""
        st = {"vectors": [], "catches": [], "ranges": []}
        for line in self.monitor("ts").splitlines():
            words = line.split()
            if not words:
                continue
            if words[0] == "vectors":
                st["vectors"] = [int(w, 16) for w in words[1:]]
            elif words[0] == "log":
                for w in words[1:]:
                    k, v = w.split("=")
                    st[k] = int(v, 16)
            elif words[0] == "pending":
                st["pending"] = int(words[1])
            elif words[0] == "catch":
                v, ah, al = (int(w, 16) for w in words[1:4])
                st["catches"].append((v, None if ah > 0xFF else ah, None if al > 0xFF else al, int(words[4])))
            elif words[0] == "xrange":
                st["ranges"].append((int(words[1], 16), int(words[2], 16)))
            elif words[0] == "tsc":
                st["tsc"] = int(words[1], 16)
                st["hz"] = int(words[3])
        return st

    def int_trace(self, vectors):
        """Log INT calls to these vectors (replacing the set); [] stops logging."""
        self.monitor("tv " + (" ".join("%x" % v for v in vectors) if vectors else "off"))

    def int_log(self, first=0, count=None):
        """Raw log records (bytes each) from sequence number `first` on."""
        cmd = "tl 0x%x" % first + ("" if count is None else " 0x%x" % count)
        return self.monitor(cmd, raw=True)

    def logpoints(self, cmd=""):
        """"lp" (with an optional command: "ADDR [REG DISP]", "- ADDR", "off"):
        {"first", "next", "record", "points": [(addr, reg, disp, hits)], "tsc", "hz"}."""
        st = {"points": []}
        for line in self.monitor(("lp " + cmd).strip()).splitlines():
            words = line.split()
            if not words:
                continue
            if words[0] == "log":
                for w in words[1:]:
                    k, v = w.split("=")
                    st[k] = int(v, 16)
            elif words[0] == "lp":
                st["points"].append((int(words[1], 16), int(words[2]), int(words[3], 16), int(words[4], 16)))
            elif words[0] == "tsc":
                st["tsc"] = int(words[1], 16)
                st["hz"] = int(words[3])
        return st

    def hit_log(self, first=0, count=None):
        """Raw logpoint records from sequence number `first` on."""
        return self.monitor("ll 0x%x" % first + ("" if count is None else " 0x%x" % count), raw=True)

    def int_log_clear(self):
        self.monitor("tc")

    def catch_int(self, vector, ah=None, al=None, when="call"):
        self.monitor("ca %x %s %s %s" % (vector, "*" if ah is None else "%x" % ah, "*" if al is None else "%x" % al, when))

    def clear_int_catches(self):
        self.monitor("cx")

    def set_exec_ranges(self, ranges, pm32=False, skip=()):
        """Stop when execution enters any [lo, hi) linear range (up to 8), and with
        pm32, at the first 32-bit protected-mode instruction outside the code
        segments in `skip` (up to 16: selectors, (selector, base) pairs, or
        ("range", lo, hi, cr3) linear ranges of one address space, up to 4); [] clears."""
        self.monitor("xr" + "".join(" %x %x" % (lo, hi) for lo, hi in ranges) + (" pm32" if pm32 else "") +
                     "".join((" skipr %x %x %x" % sk[1:] if sk[0] == "range" else " skipb %x %x" % sk)
                             if isinstance(sk, tuple) else " skip %x" % sk for sk in skip))

