"""Generate the PWA icon set.

Written by hand with zlib so the build has no image-library dependency. The
mark is a set of ascending bars (the Stage 2 advance) tightening into a pivot
line (the VCP).
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np

BG = (11, 15, 20)
GREEN = (34, 197, 94)
DIM = (22, 101, 52)
LINE = (226, 232, 240)


def _png(rgb: np.ndarray, path: Path) -> None:
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def _rect(img, x0, y0, x1, y1, color):
    h, w, _ = img.shape
    x0, y0 = max(0, int(x0)), max(0, int(y0))
    x1, y1 = min(w, int(x1)), min(h, int(y1))
    if x1 > x0 and y1 > y0:
        img[y0:y1, x0:x1] = color


def build(size: int) -> np.ndarray:
    img = np.zeros((size, size, 3), dtype=np.uint8)
    img[:, :] = BG
    s = size / 100.0

    # Four bars stepping up, each a little shorter than the gap before it -
    # rising price, contracting range.
    bars = [(14, 62), (30, 48), (46, 36), (62, 26)]
    for i, (x, top) in enumerate(bars):
        color = GREEN if i >= 2 else DIM
        _rect(img, x * s, top * s, (x + 11) * s, 82 * s, color)

    # The pivot line the last bar is pressing against.
    _rect(img, 10 * s, 22 * s, 90 * s, 24.5 * s, LINE)
    # A breakout tick above it.
    _rect(img, 78 * s, 16 * s, 89 * s, 22 * s, GREEN)
    return img


def main() -> None:
    out = Path(__file__).resolve().parent.parent / "docs" / "icons"
    out.mkdir(parents=True, exist_ok=True)
    for size, name in ((192, "icon-192.png"), (512, "icon-512.png"), (180, "apple-touch-icon.png")):
        _png(build(size), out / name)
        print("wrote", out / name)


if __name__ == "__main__":
    main()
