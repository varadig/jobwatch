#!/usr/bin/env python3
"""Draws the jobwatch icon (a white briefcase on a rounded violet square) and writes the icon files to static/:
favicon.svg, favicon.ico (16/32/48 px), apple-touch-icon.png (180 px) and icon-192.png.
Standard library only.   python3 tools/make_icons.py
"""
import os
import struct
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "static")
BG = (91, 91, 214)       # violet, close to the app's primary colour
FG = (255, 255, 255)

# The drawing on a 64 x 64 grid, painted in this order (later shapes cover earlier ones).
SHAPES = [
    ("rrect", (0, 0, 64, 64, 14), BG),          # background tile
    ("rrect", (22, 15, 42, 27, 4), FG),         # handle (outer)
    ("rrect", (26, 19, 38, 27, 1.5), BG),       # handle (hole)
    ("rrect", (10, 24, 54, 52, 6), FG),         # case
    ("rect", (10, 35, 54, 38), BG),             # seam
    ("rrect", (28, 32, 36, 41, 2), BG),         # clasp
]

SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="14" fill="#5b5bd6"/>
<path fill="#fff" fill-rule="evenodd" d="M26 15h12a4 4 0 0 1 4 4v8h-4v-6.5a1.5 1.5 0 0 0-1.5-1.5h-9a1.5 1.5 0 0 0-1.5 1.5V27h-4v-8a4 4 0 0 1 4-4z"/>
<rect x="10" y="24" width="44" height="28" rx="6" fill="#fff"/>
<rect x="10" y="35" width="44" height="3" fill="#5b5bd6"/>
<rect x="28" y="32" width="8" height="9" rx="2" fill="#5b5bd6"/>
</svg>
"""


def inside(kind, a, x, y):
    if kind == "rect":
        x0, y0, x1, y1 = a
        return x0 <= x < x1 and y0 <= y < y1
    x0, y0, x1, y1, r = a
    if not (x0 <= x < x1 and y0 <= y < y1):
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def render(size, ss=4):
    """RGBA pixels, antialiased by ss x ss supersampling; the corners outside the tile are transparent."""
    px = bytearray()
    step = 64.0 / (size * ss)
    for py in range(size):
        px.append(0)  # PNG filter byte
        for pxl in range(size):
            r = g = b = a = 0
            for sy in range(ss):
                for sx in range(ss):
                    x = (pxl * ss + sx + 0.5) * step
                    y = (py * ss + sy + 0.5) * step
                    col = None
                    for kind, args, c in SHAPES:
                        if inside(kind, args, x, y):
                            col = c
                    if col:
                        r += col[0]; g += col[1]; b += col[2]; a += 255
            n = ss * ss
            if a:
                cov = a / 255
                px += bytes((round(r / cov), round(g / cov), round(b / cov), round(a / n)))
            else:
                px += b"\0\0\0\0"
    return bytes(px)


def png(size):
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(render(size), 9)) + chunk(b"IEND", b""))


def ico(sizes):
    """An .ico with PNG images inside (supported by every browser and Windows since Vista)."""
    imgs = [png(s) for s in sizes]
    head = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = b""
    for s, data in zip(sizes, imgs):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return head + entries + b"".join(imgs)


def main():
    os.makedirs(OUT, exist_ok=True)
    files = {"favicon.svg": SVG.encode(), "favicon.ico": ico([16, 32, 48]),
             "apple-touch-icon.png": png(180), "icon-192.png": png(192)}
    for name, data in files.items():
        with open(os.path.join(OUT, name), "wb") as f:
            f.write(data)
        print("%-22s %6d bytes" % (name, len(data)))


if __name__ == "__main__":
    main()
