"""Draw the Magic Pro icon (amber disc on a dark rounded square, like static icon.svg) as a
Windows .ico with 16-256 px images. Standard library only.

    python desktop/build/make_icon.py out.ico
"""
import math
import struct
import sys
import zlib


def _png(size):
    ss = 4                                         # 4x4 supersampling for smooth edges
    rows = []
    r_box, r_disc = size * 14 / 64, size * 18 / 64
    c = size / 2
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            acc = [0.0, 0.0, 0.0, 0.0]
            for sy in range(ss):
                for sx in range(ss):
                    px, py = x + (sx + .5) / ss, y + (sy + .5) / ss
                    # rounded square
                    dx, dy = max(r_box - px, 0, px - (size - r_box)), max(r_box - py, 0, py - (size - r_box))
                    if dx * dx + dy * dy > r_box * r_box:
                        continue
                    d = math.hypot(px - c, py - c)
                    if d <= r_disc:
                        g = math.hypot(px - size * .35, py - size * .35) / (size * .7)   # radial gradient
                        stops = [(0, (0xf6, 0xb9, 0x55)), (.55, (0xf2, 0xa9, 0x3b)), (1, (0xb8, 0x6f, 0x2a))]
                        g = min(1.0, g)
                        for (a, ca), (b, cb) in zip(stops, stops[1:]):
                            if a <= g <= b:
                                t = (g - a) / (b - a)
                                col = [ca[i] + (cb[i] - ca[i]) * t for i in range(3)]
                                break
                    else:
                        col = (0x0e, 0x0f, 0x11)
                    acc[0] += col[0]
                    acc[1] += col[1]
                    acc[2] += col[2]
                    acc[3] += 255
            n = ss * ss
            alpha = acc[3] / n
            k = acc[3] / 255 or 1
            row += bytes([int(acc[0] / k), int(acc[1] / k), int(acc[2] / k), int(alpha)])
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def make_ico(path, sizes=(16, 24, 32, 48, 64, 128, 256)):
    images = [_png(s) for s in sizes]
    head = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    for s, img in zip(sizes, images):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(img), offset)
        offset += len(img)
    with open(path, "wb") as f:
        f.write(head + entries + b"".join(images))


if __name__ == "__main__":
    make_ico(sys.argv[1] if len(sys.argv) > 1 else "magicpro.ico")
