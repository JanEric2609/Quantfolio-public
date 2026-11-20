#!/usr/bin/env python3
"""Generate placeholder PWA icons: dark background with a centered accent circle.

Uses only the Python standard library (struct + zlib) to hand-encode PNGs —
no image library dependency needed for a one-time asset generation step.
Re-run this script and commit the output if the icon design changes later.
"""
import struct
import zlib
from pathlib import Path

BG = (10, 10, 10, 255)       # #0a0a0a — Quantfolio's dark background
ACCENT = (250, 128, 1, 255)  # #FA8001 — Quantfolio's accent amber


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def render(size: int, radius_ratio: float = 0.34) -> bytes:
    cx = cy = size / 2
    radius = size * radius_ratio
    rows = []
    for y in range(size):
        row = bytearray([0])  # PNG filter type 0 (none) for this scanline
        for x in range(size):
            dx, dy = x - cx + 0.5, y - cy + 0.5
            color = ACCENT if (dx * dx + dy * dy) <= radius * radius else BG
            row.extend(color)
        rows.append(bytes(row))
    raw = b"".join(rows)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA, no interlace
    png = b"\x89PNG\r\n\x1a\n"
    png += _chunk(b"IHDR", ihdr)
    png += _chunk(b"IDAT", zlib.compress(raw, 9))
    png += _chunk(b"IEND", b"")
    return png


def main() -> None:
    out_dir = Path(__file__).resolve().parent.parent / "public" / "icons"
    out_dir.mkdir(parents=True, exist_ok=True)
    targets = {
        "icon-192.png": 192,
        "icon-512.png": 512,
        "icon-512-maskable.png": 512,
        "apple-touch-icon.png": 180,
    }
    for name, size in targets.items():
        path = out_dir / name
        path.write_bytes(render(size))
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
