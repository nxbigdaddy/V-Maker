"""Generate the V-Maker app icon source (1024x1024 RGBA PNG).

The Tauri config had `bundle.icon: []` and the repository had no `src-tauri/icons/` directory at
all, so nothing could be bundled. `pnpm tauri icon` derives every platform icon from one squared
source image; this script produces that source with a tiny stdlib PNG writer (no image library).

Run:  python scripts/make_app_icon.py
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

SIZE = 1024
RADIUS = 200.0
BACKGROUND = (23, 32, 51)
ACCENT = (47, 111, 235)
ACCENT_DARK = (29, 78, 216)
INK = (244, 247, 252)


def rounded_alpha(x: float, y: float) -> bool:
    """Inside a rounded square? Corners outside the radius stay transparent."""
    cx = min(max(x, RADIUS), SIZE - RADIUS)
    cy = min(max(y, RADIUS), SIZE - RADIUS)
    dx, dy = x - cx, y - cy
    return dx * dx + dy * dy <= RADIUS * RADIUS


def inside_triangle(x: float, y: float, left: float, top: float, height: float) -> bool:
    """A right-pointing play triangle: width is proportional to the vertical distance from the axis."""
    if not (top <= y <= top + height):
        return False
    axis = top + height / 2
    reach = (1 - abs(y - axis) / (height / 2)) * (height * 0.86)
    return left <= x <= left + reach


def build_rows() -> bytes:
    raw = bytearray()
    icon_left = SIZE * 0.40
    icon_top = SIZE * 0.30
    icon_height = SIZE * 0.40
    bar_top = SIZE * 0.76

    for y in range(SIZE):
        raw.append(0)  # filter type 0
        for x in range(SIZE):
            if not rounded_alpha(x + 0.5, y + 0.5):
                raw += bytes((0, 0, 0, 0))
                continue
            if inside_triangle(x + 0.5, y + 0.5, icon_left, icon_top, icon_height):
                pixel = ACCENT
            elif bar_top <= y < bar_top + SIZE * 0.035 and SIZE * 0.30 <= x < SIZE * 0.70:
                pixel = INK if x < SIZE * 0.50 else ACCENT_DARK
            else:
                pixel = BACKGROUND
            raw += bytes((*pixel, 255))
    return bytes(raw)


def chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def main() -> None:
    target = Path(__file__).resolve().parent / "app-icon.png"
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(build_rows(), 9))
           + chunk(b"IEND", b""))
    target.write_bytes(png)
    print(f"wrote {target} ({len(png)} bytes, {SIZE}x{SIZE} RGBA)")


if __name__ == "__main__":
    main()
