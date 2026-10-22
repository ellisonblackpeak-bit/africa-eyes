"""Render the launcher icon from the same globe the gate page draws.

The icon and the landing page used to be two different pictures: the icon was a
flat green globe with an eye motif, while the gate draws a dark dot-matrix Earth.
Installed on a phone the two sat next to each other and looked like different
products.

So this script re-implements what index.html's canvas does, with the same numbers:

  · the same LAND / SEA ellipse sets (kept in sync by hand — if one moves, the
    other must move too);
  · the same orthographic projection at the same starting yaw/tilt, so the icon
    shows the same face of the planet the reader sees when the page opens;
  · the same palette: ground #020c1a, ocean #0a3a48, warm land #ffe2ba,
    cyan accent #d6fffe.

One deliberate difference: the land is painted as a CONTINUOUS lit surface with a
dot texture over it, not as isolated dots. An icon is shown at 48–120 px, where a
sparse dot field just reads as noise; a shaded landmass still reads as "Earth".

Run:  python gen_globe_icon.py
Writes icon-192/512 (+maskable), apple-touch-icon(-precomposed), favicon-32.
"""
import math
import os

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))

# Must match index.html. (lat, lon, dlat, dlon)
LAND = [
    (48, -100, 18, 26), (35, -95, 12, 16), (62, -100, 12, 34), (20, -99, 7, 9),
    (64, -150, 7, 14), (72, -42, 8, 13),
    (-8, -60, 16, 13), (-28, -62, 12, 9), (-20, -66, 9, 7), (-12, -72, 13, 7),
    (50, 15, 10, 20), (60, 26, 10, 18), (42, -4, 6, 9), (55, -3, 6, 5), (45, 20, 8, 14),
    (5, 20, 20, 17), (25, 15, 12, 20), (-15, 25, 15, 12), (-26, 25, 8, 10),
    (9, 43, 9, 8), (12, 0, 10, 12),
    (28, 45, 10, 14),
    (46, 86, 18, 34), (30, 105, 14, 22), (56, 112, 14, 33), (24, 79, 12, 11),
    (15, 101, 10, 10), (37, 139, 7, 7), (63, 90, 10, 40), (30, 55, 10, 12),
    (-25, 134, 12, 17), (41, 140, 4, 5),
]
SEA = [
    (5, -30, 32, 11), (-25, 80, 28, 22), (36, 15, 4, 13), (20, 38, 8, 2.2),
    (2, 2, 5, 8), (-55, 0, 10, 60), (15, 88, 7, 7), (15, 64, 9, 7),
    (15, 113, 8, 7), (15, -75, 6, 9), (58, -80, 6, 8), (57, 19, 3, 4),
    (43, 34, 2, 5), (41, 51, 3, 2), (26, 52, 3, 3), (41, 133, 4, 5),
    (24, -92, 6, 7), (55, 150, 6, 10), (30, 125, 5, 6), (-44, 19, 3, 7),
]

YAW, TILT = -1.3614, 0.1745      # same starting angle as the gate:/n# centre at 10°N 12°E, i.e. West Africa. lon0 = 90° + yaw°, lat0 = tilt°.

GROUND = (2, 12, 26)
OCEAN_HI = (18, 64, 86)
OCEAN_LO = (5, 26, 40)
LAND_HI = (255, 226, 186)
LAND_LO = (188, 128, 84)
ACCENT = (127, 227, 255)
STAR = (198, 226, 246)


def _in_blob(lat, lon, b):
    dy = (lat - b[0]) / b[2]
    dx = abs(((lon - b[1] + 180.0) % 360.0) - 180.0) / b[3]
    return dx * dx + dy * dy <= 1.0


def is_land(lat, lon):
    if lat < -62:
        return False
    if not any(_in_blob(lat, lon, b) for b in LAND):
        return False
    return not any(_in_blob(lat, lon, b) for b in SEA)


def _mix(a, b, t):
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    return (int(round(a[0] + (b[0] - a[0]) * t)),
            int(round(a[1] + (b[1] - a[1]) * t)),
            int(round(a[2] + (b[2] - a[2]) * t)))


def _over(dst, src, alpha):
    if alpha <= 0:
        return dst
    a = 1.0 if alpha > 1 else alpha
    return (int(round(dst[0] * (1 - a) + src[0] * a)),
            int(round(dst[1] * (1 - a) + src[1] * a)),
            int(round(dst[2] * (1 - a) + src[2] * a)))


def render(size, ball_ratio=0.80, ss=3):
    """One icon. `ball_ratio` is sphere diameter / canvas (smaller for maskable,
    whose outer edge the launcher may crop). `ss` supersamples the sphere edge
    and the orbit ring."""
    S = size * ss
    cx = cy = S / 2.0
    R = S * ball_ratio / 2.0

    img = Image.new("RGB", (S, S), GROUND)
    px = img.load()

    cyaw, syaw = math.cos(YAW), math.sin(YAW)
    ct, st = math.cos(TILT), math.sin(TILT)
    lx, ly, lz = -0.55, 0.62, 0.56          # light: upper-left, in front

    for y in range(S):
        py = y + 0.5 - cy
        for x in range(S):
            dx = (x + 0.5 - cx) / R
            dy = -py / R
            d2 = dx * dx + dy * dy

            if d2 > 1.0:
                r = math.sqrt(d2)
                if r < 1.45:                     # atmosphere halo
                    t = 1.0 - (r - 1.0) / 0.45
                    px[x, y] = _over(GROUND, (26, 92, 134), 0.26 * t * t)
                else:                            # faint star field
                    h = (x * 73856093) ^ (y * 19349663)
                    px[x, y] = (_over(GROUND, STAR, 0.55)
                                if (h & 1023) < 5 else GROUND)
                continue

            dz = math.sqrt(max(0.0, 1.0 - d2))
            # screen -> unit sphere
            x1, y1 = dx, dy
            y0 = y1 * ct + dz * st               # undo tilt (about x)
            z1 = -y1 * st + dz * ct
            x0 = x1 * cyaw - z1 * syaw           # undo yaw (about y)
            z0 = x1 * syaw + z1 * cyaw

            lat = math.degrees(math.asin(max(-1.0, min(1.0, y0))))
            lon = math.degrees(math.atan2(z0, x0))

            lam = x0 * lx + y0 * ly + z0 * lz
            lam = 0.0 if lam < 0 else lam
            lam = lam ** 0.85

            # Soften the very edge so the disc does not end on a hard pixel step.
            limb = math.sqrt(d2)
            edge = 1.0 if limb < 0.90 else max(0.0, (1.0 - limb) / 0.10)

            if is_land(lat, lon):
                base = _mix(LAND_LO, LAND_HI, 0.30 + 0.70 * lam)
                # dot lattice over the land, so the matrix texture survives
                gx = int((lon + 180) / 5.0)
                gy = int((lat + 90) / 5.0)
                if ((gx * 31 + gy * 17) % 7) < 3:
                    base = _mix(base, (255, 250, 240), 0.18)
                else:
                    base = _mix(base, (146, 100, 66), 0.14)
                col = _mix(base, OCEAN_LO, (1.0 - edge) * 0.80)
            else:
                base = _mix(OCEAN_LO, OCEAN_HI, 0.22 + 0.78 * lam)
                h = ((int((lon + 180) / 6.0) * 2654435761)
                     ^ (int((lat + 90) / 6.0) * 40503)) & 0xFFFFFFFF
                if (h & 255) < 24:
                    base = _mix(base, (130, 200, 232), 0.55)
                col = _mix(base, GROUND, (1.0 - edge) * 0.60)

            px[x, y] = col

    # ── limb: a crisp cyan ring plus a soft outer bloom
    for y in range(S):
        for x in range(S):
            dx = (x + 0.5 - cx) / R
            dy = (y + 0.5 - cy) / R
            d = math.sqrt(dx * dx + dy * dy)
            if 0.972 <= d <= 1.0:
                px[x, y] = _over(px[x, y], ACCENT, 0.78)
            elif 1.0 < d <= 1.06:
                px[x, y] = _over(px[x, y], ACCENT, 0.34 * (1.06 - d) / 0.06)

    # ── orbit ring, same tilt as the page; the far half is dimmer
    ar, br = R * 1.22, R * 0.30
    ca, sa = math.cos(-0.42), math.sin(-0.42)
    steps = int(S * 2.4)
    for i in range(steps):
        th = 2 * math.pi * i / steps
        ox, oy = math.cos(th) * ar, math.sin(th) * br
        xf = cx + ox * ca - oy * sa
        yf = cy + ox * sa + oy * ca
        xi, yi = int(xf), int(yf)
        if 0 <= xi < S and 0 <= yi < S:
            near = math.sin(th) < 0
            px[xi, yi] = _over(px[xi, yi], ACCENT, 0.36 if near else 0.14)

    return img.resize((size, size), Image.LANCZOS)


def main():
    jobs = [
        ("icon-512.png", 512, 0.80, 3),
        ("icon-192.png", 192, 0.80, 3),
        ("icon-512-maskable.png", 512, 0.62, 3),
        ("icon-192-maskable.png", 192, 0.62, 3),
        ("apple-touch-icon.png", 180, 0.78, 3),
        ("apple-touch-icon-precomposed.png", 180, 0.78, 3),
        ("favicon-32.png", 32, 0.94, 4),
    ]
    for name, size, ratio, ss in jobs:
        im = render(size, ratio, ss=ss)
        p = os.path.join(HERE, name)
        im.save(p, "PNG", optimize=True)
        print("%-34s %4dpx  %6d bytes" % (name, size, os.path.getsize(p)))


if __name__ == "__main__":
    main()
