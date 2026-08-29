#!/usr/bin/env python3
"""The Study Hub mark: a mortarboard, drawn once and used everywhere.

Asked for by EH on 2026-08-22: a name for the whole thing, a small logo with
an academic hat, and that hat as the browser tab icon "so it's easier to find the
browser tab". The tab is the point, so the drawing is made for 16 pixels first
and merely happens to hold up larger.

**Three shapes and nothing else.** A board, a cap under it, a tassel. At the size
it will actually be seen, a fourth shape is mud. The tassel is amber against the
teal for the same reason: at 16 pixels a second hue is the fastest thing to
recognise in a row of tabs, faster than any outline.

**Two ways out of here.** `svg()` for anything that renders SVG, which is every
current browser and both places the mark appears on a page. `png()` for the
fallbacks that cannot: `apple-touch-icon`, and the bare `/favicon.ico` that a
browser asks for without being told to.

🔴 **The PNG is rasterised here, in pure Python.** No dependency exists in this
project and none is being added for an icon. It is polygons and a circle, so
point-in-shape with 4x supersampling is honest antialiasing and about sixty
lines. The alternative was shipping a base64 blob nobody could edit.
"""

import struct
import zlib

# The reader's own accent, and its dark-mode counterpart. Kept in step with the
# palette in study_server.py by hand; there are two of them and they change about
# once a year.
TEAL = (0x1C, 0x6D, 0x61)
TEAL_DARK = (0x14, 0x50, 0x47)
TEAL_LIGHT = (0x5F, 0xBF, 0xAE)
TEAL_LIGHT_DARK = (0x3D, 0x8F, 0x82)
AMBER = (0xC9, 0x7B, 0x1E)

# 🔴 Sized to FILL the box, which is not a nicety at 16 pixels: the first
# version left a fifth of the height empty top and bottom, so the drawing was
# effectively 13 pixels tall in a 16 pixel tab and read as a smudge next to
# icons that used their whole square.
BOARD = [(32, 5), (62, 21), (32, 37), (2, 21)]
# The cap's underside is a curve in the SVG, so the polygon the rasteriser uses
# is generated from the same curve rather than eyeballed with four corners. Eight
# straight segments were visible as facets at 180 pixels, which is the size the
# apple-touch-icon is actually seen at.
def _cap():
    import math
    pts = [(17.0, 27.0), (17.0, 42.0)]
    steps = 24
    for i in range(1, steps):
        t = i / float(steps)
        pts.append((17.0 + 30.0 * t, 42.0 + 11.0 * math.sin(math.pi * t)))
    pts += [(47.0, 42.0), (47.0, 27.0), (32.0, 35.0)]
    return pts


CAP = _cap()
CORD_X, CORD_TOP, CORD_BOT, CORD_W = 56.5, 23.0, 47.0, 3.4
KNOT = (56.5, 52.0, 5.4)


def svg(px=None, theme_aware=True):
    """The mark as SVG. `px` sets width and height; omit it to scale to its box.

    Theme-aware by default, which a favicon can be and a person notices: an icon
    drawn for a white tab bar goes muddy on a dark one. A page that already knows
    its theme passes theme_aware=False and inherits nothing it does not want."""
    size = ' width="%d" height="%d"' % (px, px) if px else ""
    style = ""
    if theme_aware:
        style = (
            "<style>"
            ".b{fill:#1C6D61}.c{fill:#145047}.t{stroke:#C97B1E;fill:#C97B1E}"
            "@media (prefers-color-scheme:dark){"
            ".b{fill:#5FBFAE}.c{fill:#3D8F82}.t{stroke:#E0A050;fill:#E0A050}}"
            "</style>")
    else:
        style = ("<style>.b{fill:currentColor}.c{fill:currentColor;opacity:.72}"
                 ".t{stroke:currentColor;fill:currentColor;opacity:.9}</style>")
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"%s '
        'role="img" aria-label="Study Hub">%s'
        '<path class="c" d="M17 27 L17 42 C17 48 23.5 53 32 53 '
        'C40.5 53 47 48 47 42 L47 27 L32 35 Z"/>'
        '<path class="b" d="M32 5 L62 21 L32 37 L2 21 Z"/>'
        '<path class="t" d="M56.5 23 L56.5 47" stroke-width="3.4" '
        'stroke-linecap="round" fill="none"/>'
        '<circle class="t" cx="56.5" cy="52" r="5.4"/>'
        '</svg>') % (size, style)


# --------------------------------------------------------------------------
# the raster fallback
# --------------------------------------------------------------------------

def _inside(poly, x, y):
    """Point in polygon, by ray casting. Fine for eight vertices."""
    hit = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xat = x1 + (y - y1) * (x2 - x1) / float(y2 - y1)
            if x < xat:
                hit = not hit
    return hit


def _shade(x, y, dark):
    """What colour, if any, is at this point of the 64x64 drawing.

    Order matters and is the drawing order: the cap is under the board, the
    tassel is over both. Returns None for the background, which stays
    transparent."""
    cord_hit = (abs(x - CORD_X) <= CORD_W / 2.0 and CORD_TOP <= y <= CORD_BOT)
    kx, ky, kr = KNOT
    knot_hit = ((x - kx) ** 2 + (y - ky) ** 2) <= kr * kr
    if cord_hit or knot_hit:
        return (0xE0, 0xA0, 0x50) if dark else AMBER
    if _inside(BOARD, x, y):
        return TEAL_LIGHT if dark else TEAL
    if _inside(CAP, x, y):
        return TEAL_LIGHT_DARK if dark else TEAL_DARK
    return None


def _pixels(size, dark=False, ss=4):
    """RGBA rows, supersampled `ss` times per axis for the edges."""
    rows = []
    step = 64.0 / size
    inv = 1.0 / (ss * ss)
    for py in range(size):
        row = bytearray()
        for px in range(size):
            r = g = b = 0.0
            hits = 0
            for sy in range(ss):
                for sx in range(ss):
                    x = (px + (sx + 0.5) / ss) * step
                    y = (py + (sy + 0.5) / ss) * step
                    c = _shade(x, y, dark)
                    if c:
                        r += c[0]; g += c[1]; b += c[2]
                        hits += 1
            if not hits:
                row += b"\x00\x00\x00\x00"
            else:
                # Colour is the average of the samples that HIT; alpha is the
                # share that hit. Averaging over all samples instead would darken
                # every edge towards black, which is the classic halo.
                row += bytes((int(r / hits + .5), int(g / hits + .5),
                              int(b / hits + .5), int(hits * inv * 255 + .5)))
        rows.append(bytes(row))
    return rows


def png(size=180, dark=False):
    """A PNG of the mark, `size` square, transparent background."""
    raw = b"".join(b"\x00" + row for row in _pixels(size, dark))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def ico(size=32):
    """A .ico wrapping a PNG, for the bare /favicon.ico a browser asks for.

    An ICO may carry a PNG rather than a BMP, and every browser that still asks
    for favicon.ico understands one."""
    body = png(size)
    # Header: reserved, type 1 (icon), one image.
    head = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII",
                        size if size < 256 else 0, size if size < 256 else 0,
                        0, 0, 1, 32, len(body), 22)
    return head + entry + body


if __name__ == "__main__":
    import pathlib
    import sys
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    (out / "hat.svg").write_text(svg(), encoding="utf-8")
    for n in (16, 32, 64, 180):
        (out / ("hat-%d.png" % n)).write_bytes(png(n))
    (out / "hat-dark-32.png").write_bytes(png(32, dark=True))
    (out / "hat.ico").write_bytes(ico(32))
    print("wrote the mark to %s" % out)
