#!/usr/bin/env python3
"""Writes the app icons (PNG, ICO and ICNS) using only the standard library.

The artwork is a placeholder: a blue rounded square with a white shield. To use
real artwork, replace it with a 1024x1024 PNG and run `cargo tauri icon <file>`
from src-tauri/, which regenerates every size.
"""

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent / "src-tauri" / "icons"
TOP, BOTTOM = (90, 200, 250), (10, 132, 255)  # light to deep macOS-style blue


def inside_shield(x, y):
    # Shield outline in unit coordinates: a flat top, straight sides, then a point at the bottom.
    if y < 0.24 or y > 0.80:
        return False
    if y <= 0.46:
        half = 0.25
    else:
        half = 0.25 * (0.80 - y) / (0.80 - 0.46)
    return abs(x - 0.5) <= half


def inside_rounded_square(x, y, radius=0.22):
    cx = min(max(x, radius), 1 - radius)
    cy = min(max(y, radius), 1 - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2


def render(size):
    rows = []
    for py in range(size):
        row = bytearray()
        y = (py + 0.5) / size
        for px in range(size):
            x = (px + 0.5) / size
            if not inside_rounded_square(x, y):
                row += b"\x00\x00\x00\x00"
                continue
            t = y
            r, g, b = (round(TOP[i] + (BOTTOM[i] - TOP[i]) * t) for i in range(3))
            if inside_shield(x, y):
                r, g, b = 255, 255, 255
            row += bytes((r, g, b, 255))
        rows.append(bytes(row))
    return rows


def png(size, rows):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + row for row in rows)
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def ico(entries):
    """entries: [(size, png_bytes)]. Modern ICO files may embed PNGs directly."""
    header = struct.pack("<HHH", 0, 1, len(entries))
    directory, data = b"", b""
    offset = 6 + 16 * len(entries)
    for size, blob in entries:
        dim = 0 if size >= 256 else size
        directory += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        data += blob
    return header + directory + data


def icns(entries):
    """entries: [(four_char_type, png_bytes)]. ic07..ic10 are PNG-encoded 128, 256, 512 and 1024 px."""
    body = b"".join(kind + struct.pack(">I", len(blob) + 8) + blob for kind, blob in entries)
    return b"icns" + struct.pack(">I", len(body) + 8) + body


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    blobs = {}
    for size in (32, 128, 256, 512, 1024):
        blobs[size] = png(size, render(size))
        print(f"rendered {size}px")
    (OUT / "32x32.png").write_bytes(blobs[32])
    (OUT / "128x128.png").write_bytes(blobs[128])
    (OUT / "128x128@2x.png").write_bytes(blobs[256])
    (OUT / "icon.png").write_bytes(blobs[512])
    (OUT / "icon.ico").write_bytes(ico([(s, blobs[s]) for s in (32, 256)]))
    (OUT / "icon.icns").write_bytes(icns([(b"ic07", blobs[128]), (b"ic08", blobs[256]),
                                          (b"ic09", blobs[512]), (b"ic10", blobs[1024])]))
    print(f"wrote icons to {OUT}")


if __name__ == "__main__":
    main()
