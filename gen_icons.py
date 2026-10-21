"""Generate the China Eyes app icon — a globe — with no third-party packages.

Pillow is not installed and the package index is unreachable from this machine,
so the icon is rendered by hand: an orthographic sphere, per-pixel inverse
projection to lat/lon, landmasses and seas as coarse ellipses, 4x supersampled
for anti-aliasing, then written as a PNG using only `zlib` + `struct`.

Per-pixel inverse projection rather than forward-mapping blobs: forward mapping
left the sphere riddled with gaps, because one sample every 0.35° is 5–8 screen
pixels apart at 512px.

Run:  python gen_icons.py
"""
import math
import os
import struct
import zlib

OUT = os.path.dirname(os.path.abspath(__file__))

# ── palette (matches manifest theme_color #0F3D5C) ────────────────────────
BG_TOP = (11, 46, 74)
BG_BOT = (17, 78, 116)
OCEAN = (38, 126, 186)
OCEAN_DEEP = (22, 84, 134)
LAND = (74, 174, 122)
LAND_HI = (108, 199, 145)
GRID = (255, 255, 255)
RIM = (150, 214, 245)
RED = (214, 69, 65)

# Landmasses: (lat, lon, dlat, dlon). Rough continental shapes — an icon needs
# to read as "a globe", not to be a map.
LAND_BLOBS = [
    # Africa (the visual centre of the icon) + Madagascar
    (8, 18, 17, 16), (26, 20, 11, 15), (-16, 24, 13, 9), (7, 42, 8, 7),
    (-19, 47, 4, 1.8),
    # Europe
    (50, 12, 8, 14), (55, 33, 9, 16), (63, 16, 5, 11),
    # Asia
    (45, 82, 16, 32), (27, 98, 13, 20), (58, 124, 10, 26),
    (27, 46, 8, 9), (20, 78, 10, 7), (13, 103, 8, 8),
    # Islands / Australia
    (-3, 118, 4, 9), (-25, 134, 11, 16), (14, 121, 6, 4), (35, 138, 5, 4),
    # Americas
    (-10, -58, 17, 9), (-30, -62, 10, 6), (4, -70, 6, 8),
    (46, -100, 17, 23), (30, -100, 8, 11),
]
# Seas cut back out of the landmasses, so the globe is not one green blob.
SEA_BLOBS = [
    (12, -40, 42, 20), (-20, 80, 32, 26),                   # Atlantic, Indian Ocean
    (36, 16, 4.0, 15), (20, 38, 8, 2.0),                    # Mediterranean, Red Sea
    (2, 2, 5, 10), (-17, 41, 7, 2),                         # Gulf of Guinea, Mozambique Ch.
    (15, 64, 9, 8), (15, 88, 6, 6),                         # Arabian Sea, Bay of Bengal
    (57, 19, 3, 4), (43, 34, 2, 5), (41, 51, 3, 2),         # Baltic, Black, Caspian
    (26, 52, 4, 3), (-4, 112, 5, 8), (25, 125, 4, 6),       # Persian Gulf, Java Sea
    (-48, 0, 10, 60),                                       # Southern Ocean
]

VIEW_LAT, VIEW_LON = 14.0, 22.0     # Africa centred, Europe north, Asia to the east
CHINA = (39.9, 116.4)


def _in_blob(lat, lon, blob):
    blat, blon, dlat, dlon = blob
    dy = (lat - blat) / dlat
    dx = abs((lon - blon + 180.0) % 360.0 - 180.0) / dlon
    return dx * dx + dy * dy <= 1.0


def _is_land(lat, lon):
    if lat < -62:
        return False
    if not any(_in_blob(lat, lon, b) for b in LAND_BLOBS):
        return False
    return not any(_in_blob(lat, lon, b) for b in SEA_BLOBS)


def _proj(lat, lon):
    p, l = math.radians(lat), math.radians(lon)
    p0, l0 = math.radians(VIEW_LAT), math.radians(VIEW_LON)
    dl = l - l0
    return (math.cos(p) * math.sin(dl),
            math.cos(p0) * math.sin(p) - math.sin(p0) * math.cos(p) * math.cos(dl),
            math.sin(p0) * math.sin(p) + math.cos(p0) * math.cos(p) * math.cos(dl))


def _mix(c1, c2, t):
    return tuple(int(round(c1[i] + (c2[i] - c1[i]) * t)) for i in range(3))


def render(size, ball=0.40):
    SS = 4
    S = size * SS
    px = bytearray(S * S * 4)

    # background: vertical gradient
    for y in range(S):
        r, g, b = _mix(BG_TOP, BG_BOT, y / max(1, S - 1))
        px[y * S * 4:(y + 1) * S * 4] = bytes((r, g, b, 255)) * S

    cx = cy = S / 2.0
    R = S * ball
    p0 = math.radians(VIEW_LAT)
    l0 = math.radians(VIEW_LON)
    sp0, cp0 = math.sin(p0), math.cos(p0)

    y0, y1 = int(cy - R) - 1, int(cy + R) + 2
    x0, x1 = int(cx - R) - 1, int(cx + R) + 2

    for yy in range(max(0, y0), min(S, y1)):
        Y = -(yy - cy) / R
        for xx in range(max(0, x0), min(S, x1)):
            X = (xx - cx) / R
            r2 = X * X + Y * Y
            if r2 > 1.0:
                continue
            z = math.sqrt(max(0.0, 1.0 - r2))

            # inverse orthographic → lat / lon on the sphere
            sphi = sp0 * z + cp0 * Y
            sphi = max(-1.0, min(1.0, sphi))
            phi = math.asin(sphi)
            cosphi = math.cos(phi)
            if abs(cosphi) < 1e-6:
                lon = VIEW_LON
            else:
                cosd = (z - sp0 * sphi) / (cp0 * cosphi)
                cosd = max(-1.0, min(1.0, cosd))
                d = math.acos(cosd)
                if X < 0:
                    d = -d
                lon = math.degrees(l0 + d)
                if lon > 180:
                    lon -= 360
                if lon < -180:
                    lon += 360
            lat = math.degrees(phi)

            # base colour
            if _is_land(lat, lon):
                shade = 0.80 + 0.34 * z
                col = tuple(min(255, int(c * shade)) for c in LAND)
                # a touch of relief: brighter high ground
                if math.cos(lat) * math.cos(math.radians(lon - VIEW_LON)) > 0.55:
                    col = _mix(col, LAND_HI, 0.22 * z)
            else:
                shade = 0.70 + 0.46 * z
                deep = 1.0 - z
                base = _mix(OCEAN, OCEAN_DEEP, deep * 0.55)
                col = tuple(min(255, int(c * shade)) for c in base)

            # graticule, 30° spacing, faded toward the limb
            dla = abs(((lon - l0) + 15.0) % 30.0 - 15.0)
            dph = abs(((lat - p0) + 15.0) % 30.0 - 15.0)
            ga = 0.0
            if dla < 0.34:
                ga = max(ga, 0.30 * (1.0 - dla / 0.34))
            if dph < 0.34:
                ga = max(ga, 0.30 * (1.0 - dph / 0.34))
            if ga > 0.0:
                ga *= 0.30 + 0.70 * z
                col = _mix(col, GRID, ga)

            # rim light
            if z < 0.055:
                col = _mix(col, RIM, (0.055 - z) / 0.055 * 0.85)

            i = (yy * S + xx) * 4
            # Write by index. Slice assignment silently RESIZED the buffer when a
            # right-hand side was ever shorter than the slice, which left the
            # array 11652 bytes short and blew up the downsampler later.
            px[i] = col[0]
            px[i + 1] = col[1]
            px[i + 2] = col[2]
            px[i + 3] = 255

    # the China marker
    mx, my, mz = _proj(*CHINA)
    if mz > 0:
        pcx, pcy = cx + mx * R, cy - my * R
        rad = R * 0.082
        for yy in range(int(pcy - rad) - 3, int(pcy + rad) + 4):
            if not (0 <= yy < S):
                continue
            for xx in range(int(pcx - rad) - 3, int(pcx + rad) + 4):
                if not (0 <= xx < S):
                    continue
                d = math.hypot(xx - pcx, yy - pcy)
                i = (yy * S + xx) * 4
                if d <= rad:
                    px[i] = RED[0]
                    px[i + 1] = RED[1]
                    px[i + 2] = RED[2]
                    px[i + 3] = 255
                elif d <= rad + 2.6 * SS / 4:
                    a = 0.55 * (1.0 - (d - rad) / (2.6 * SS / 4)) + 0.18
                    for k in range(3):
                        px[i + k] = int(px[i + k] * (1 - a) + RED[k] * a)

    # downsample SS×SS → size
    out = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            acc = [0, 0, 0, 0]
            for oy in range(SS):
                base = ((y * SS + oy) * S + x * SS) * 4
                for ox in range(SS):
                    i = base + ox * 4
                    acc[0] += px[i]
                    acc[1] += px[i + 1]
                    acc[2] += px[i + 2]
                    acc[3] += px[i + 3]
            i = (y * size + x) * 4
            for k in range(4):
                out[i + k] = acc[k] // (SS * SS)
    return out


def write_png(path, size, rgba):
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        raw.extend(rgba[y * size * 4:(y + 1) * size * 4])

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
           + chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(png)
    return len(png)


def main():
    jobs = [
        ("icon-512.png", 512, 0.40),
        ("icon-192.png", 192, 0.40),
        ("apple-touch-icon.png", 180, 0.40),
        ("apple-touch-icon-precomposed.png", 180, 0.40),
        ("favicon-32.png", 32, 0.45),
        # maskable: content must sit inside the central 80% circle
        ("icon-512-maskable.png", 512, 0.31),
        ("icon-192-maskable.png", 192, 0.31),
    ]
    for name, size, ball in jobs:
        data = render(size, ball)
        n = write_png(os.path.join(OUT, name), size, data)
        print("%-34s %3dpx  %6d bytes" % (name, size, n))


if __name__ == "__main__":
    main()
