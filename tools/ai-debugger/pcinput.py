"""Screen capture and keyboard/mouse input through the 86Box GDB stub's
monitor commands (fi/fb, kd/ku, mm/mb; see "monitor help")."""

import re
import struct
import time
import zlib

# ---- screen ----------------------------------------------------------------

def grab_frame(client):
    """Return (width, height, frame_number, rgb_bytes) of the last frame shown."""
    info = client.monitor("fi").split()
    w, h, seq = int(info[0]), int(info[1]), int(info[2])
    if not w or not h:
        return 0, 0, 0, b""
    data = client.monitor("fb", raw=True)
    if len(data) != w * h * 3:
        raise RuntimeError("frame transfer returned %d bytes, expected %d" % (len(data), w * h * 3))
    return w, h, seq, data


def downscale(w, h, rgb, factor):
    """Integer box downscale (factor 2 averages 2x2 pixels)."""
    if factor <= 1:
        return w, h, rgb
    nw, nh = w // factor, h // factor
    out = bytearray(nw * nh * 3)
    area = factor * factor
    for y in range(nh):
        for x in range(nw):
            r = g = b = 0
            for dy in range(factor):
                base = ((y * factor + dy) * w + x * factor) * 3
                for dx in range(factor):
                    o = base + dx * 3
                    r += rgb[o]
                    g += rgb[o + 1]
                    b += rgb[o + 2]
            o = (y * nw + x) * 3
            out[o], out[o + 1], out[o + 2] = r // area, g // area, b // area
    return nw, nh, bytes(out)


def encode_png(w, h, rgb):
    def chunk(tag, payload):
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    stride = w * 3
    raw = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(h))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


# ---- keyboard --------------------------------------------------------------

# Scan code set 1 (what 86Box's keyboard_input() takes); E0xx = extended key.
KEYS = {
    "esc": 0x01, "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06, "6": 0x07, "7": 0x08,
    "8": 0x09, "9": 0x0A, "0": 0x0B, "-": 0x0C, "=": 0x0D, "backspace": 0x0E, "tab": 0x0F,
    "q": 0x10, "w": 0x11, "e": 0x12, "r": 0x13, "t": 0x14, "y": 0x15, "u": 0x16, "i": 0x17,
    "o": 0x18, "p": 0x19, "[": 0x1A, "]": 0x1B, "enter": 0x1C, "ctrl": 0x1D, "a": 0x1E,
    "s": 0x1F, "d": 0x20, "f": 0x21, "g": 0x22, "h": 0x23, "j": 0x24, "k": 0x25, "l": 0x26,
    ";": 0x27, "'": 0x28, "`": 0x29, "shift": 0x2A, "\\": 0x2B, "z": 0x2C, "x": 0x2D,
    "c": 0x2E, "v": 0x2F, "b": 0x30, "n": 0x31, "m": 0x32, ",": 0x33, ".": 0x34, "/": 0x35,
    "rshift": 0x36, "kp*": 0x37, "alt": 0x38, "space": 0x39, "capslock": 0x3A,
    "f1": 0x3B, "f2": 0x3C, "f3": 0x3D, "f4": 0x3E, "f5": 0x3F, "f6": 0x40, "f7": 0x41,
    "f8": 0x42, "f9": 0x43, "f10": 0x44, "numlock": 0x45, "scrolllock": 0x46,
    "kp7": 0x47, "kp8": 0x48, "kp9": 0x49, "kp-": 0x4A, "kp4": 0x4B, "kp5": 0x4C,
    "kp6": 0x4D, "kp+": 0x4E, "kp1": 0x4F, "kp2": 0x50, "kp3": 0x51, "kp0": 0x52,
    "kp.": 0x53, "f11": 0x57, "f12": 0x58,
    "kpenter": 0xE01C, "rctrl": 0xE01D, "kp/": 0xE035, "ralt": 0xE038,
    "home": 0xE047, "up": 0xE048, "pageup": 0xE049, "left": 0xE04B, "right": 0xE04D,
    "end": 0xE04F, "down": 0xE050, "pagedown": 0xE051, "insert": 0xE052, "delete": 0xE053,
}
ALIASES = {"escape": "esc", "return": "enter", "control": "ctrl", "lctrl": "ctrl", "lshift": "shift",
           "lalt": "alt", "spacebar": "space", "bksp": "backspace", "del": "delete", "ins": "insert",
           "pgup": "pageup", "pgdn": "pagedown", "arrowup": "up", "arrowdown": "down",
           "arrowleft": "left", "arrowright": "right"}
SHIFTED = {"!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8",
           "(": "9", ")": "0", "_": "-", "+": "=", "{": "[", "}": "]", ":": ";", '"': "'",
           "~": "`", "|": "\\", "<": ",", ">": ".", "?": "/"}


def scancode(name):
    key = name.strip().lower()
    key = ALIASES.get(key, key)
    if key not in KEYS:
        raise ValueError("unknown key %r (known: %s)" % (name, ", ".join(sorted(KEYS))))
    return KEYS[key]


def parse_combo(combo):
    """'ctrl+alt+delete' -> [scan, ...]. "kp+" (keypad plus) works inside
    chords too ("ctrl+kp+"); a lone "+" is also the keypad plus."""
    combo = combo.strip().lower()
    if combo == "+":
        return [scancode("kp+")]
    return [scancode(m.group()) for m in re.finditer(r"kp\+|[^+]+", combo)]


def char_keys(ch):
    """Keys to press for one character, as (needs_shift, scan)."""
    if ch == "\n":
        return False, KEYS["enter"]
    if ch == "\t":
        return False, KEYS["tab"]
    if ch == " ":
        return False, KEYS["space"]
    if ch.isalpha() and ch.isascii():
        return ch.isupper(), KEYS[ch.lower()]
    if ch in SHIFTED:
        return True, KEYS[SHIFTED[ch]]
    if ch in KEYS:
        return False, KEYS[ch]
    raise ValueError("cannot type %r" % ch)


def key_event(client, scan, down):
    client.monitor("%s 0x%x" % ("kd" if down else "ku", scan))


def press_combo(client, scans, hold):
    for s in scans:
        key_event(client, s, True)
    time.sleep(hold)
    for s in reversed(scans):
        key_event(client, s, False)


# ---- mouse -------------------------------------------------------------------

BUTTONS = {"left": 1, "right": 2, "middle": 4}


def mouse_move(client, dx, dy, dz=0):
    cmd = "mm %d %d" % (dx, dy)
    if dz:
        cmd += " %d" % dz
    client.monitor(cmd)


def mouse_buttons(client, mask):
    client.monitor("mb 0x%x" % mask)
