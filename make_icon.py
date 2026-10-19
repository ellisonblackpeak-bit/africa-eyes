# -*- coding: utf-8 -*-
"""Generate the Africa Eyes app icon (an eye) at every size a phone or browser
might ask for. All outputs are **opaque** (no alpha-around-corners transparency)
because iOS and some PWA installers render transparent-corner PNGs as a generic
page-screenshot or fill the alpha with a default background — which is exactly
the "installed but there's no eye" failure mode we just hit.

Outputs:
  icon-192.png             192x192  PWA / Android (any)
  icon-512.png             512x512  PWA / Android (any)
  icon-192-maskable.png    192x192  PWA / Android adaptive icon (maskable)
  icon-512-maskable.png    512x512  PWA / Android adaptive icon (maskable)
  apple-touch-icon.png            180x180  iOS Safari add-to-home-screen
  apple-touch-icon-precomposed.png 180x180  iOS without gloss/rounded re-render
  favicon-32.png            32x32   browser tab

Design: rounded-square background matches the site header gradient
(#0F3D5C -> #12756A). Eye is an almond outline with an iris and pupil,
echoing the 👁️ reference. All shapes drawn at 8x and downsampled with
LANCZOS for clean anti-aliased edges without needing a vector library.
The maskable variants keep the eye inside the inner 66% safe zone that
Android's adaptive-icon mask may crop to.
"""
import io
import os
import sys

from PIL import Image, ImageDraw

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))

NAVY = (15, 61, 92)
TEAL = (18, 117, 106)
S = 8  # supersample factor


def lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def bg(w, h):
    """Diagonal navy->teal gradient, fully opaque."""
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            t = (x / max(1, w - 1) * 0.55) + (y / max(1, h - 1) * 0.45)
            px[x, y] = lerp(NAVY, TEAL, t)
    return img


def almond(d, cx, cy, hw, hh, fill, outline=None, width=1):
    """An eye outline: two arcs meeting at the left and right corners."""
    pts = []
    n = 120
    for i in range(n + 1):                      # upper lid, left -> right
        t = i / n
        x = cx - hw + 2 * hw * t
        pts.append((x, cy - hh * (1 - (2 * t - 1) ** 2) ** 0.5))
    for i in range(n, -1, -1):                  # lower lid, right -> left
        t = i / n
        x = cx - hw + 2 * hw * t
        pts.append((x, cy + hh * (1 - (2 * t - 1) ** 2) ** 0.5))
    d.polygon(pts, fill=fill, outline=outline, width=width)


def make_icon(size, maskable=False):
    """Render one icon at `size` px, fully opaque.

    maskable=True draws the eye scaled into the inner safe zone that Android's
    adaptive-icon crop may keep, and skips the rounded-square mask (Android
    does its own masking).
    """
    W = size * S
    img = bg(W, W).convert("RGBA")
    d = ImageDraw.Draw(img)

    if not maskable:
        # Bake a rounded-square mask into the OPACITY channel — corners become
        # background-coloured, not transparent. iOS then adds its own rounding
        # on top, giving a clean rounded app icon, not a screenshot fallback.
        mask = Image.new("L", (W, W), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, W - 1],
                                               radius=int(W * 0.225), fill=255)
        bg_layer = Image.new("RGBA", (W, W), (0, 0, 0, 255))   # opaque black
        bg_layer.paste(img, (0, 0), mask)
        img = bg_layer
        d = ImageDraw.Draw(img)

    cx = cy = W / 2
    scale = 0.62 if maskable else 0.76
    hw = W * scale / 2
    hh = hw * 0.56

    # subtle glow behind the eye
    glow = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([cx - hw * 1.18, cy - hh * 1.9,
                                  cx + hw * 1.18, cy + hh * 1.9],
                                 fill=(255, 255, 255, 26))
    img = Image.alpha_composite(img, glow)
    d = ImageDraw.Draw(img)

    # eye white (almond), then iris, pupil, highlight.
    almond(d, cx, cy, hw, hh, fill=(255, 255, 255, 255))
    r_iris = hh * 0.62
    d.ellipse([cx - r_iris, cy - r_iris, cx + r_iris, cy + r_iris],
              fill=(18, 117, 106, 255))
    r_pupil = r_iris * 0.50
    d.ellipse([cx - r_pupil, cy - r_pupil, cx + r_pupil, cy + r_pupil],
              fill=(11, 42, 63, 255))
    r_hl = r_iris * 0.19
    hx, hy = cx - r_iris * 0.36, cy - r_iris * 0.42
    d.ellipse([hx - r_hl, hy - r_hl, hx + r_hl, hy + r_hl],
              fill=(255, 255, 255, 240))

    # eyelid accent
    almond(d, cx, cy, hw, hh, fill=None, outline=(232, 240, 246, 255),
           width=max(2, int(W * 0.012)))

    return img.resize((size, size), Image.LANCZOS)


TARGETS = [
    # any-purpose (standard): rounded-square, eye large.
    ("icon-192.png", 192, False),
    ("icon-512.png", 512, False),
    # maskable (Android adaptive): full square, eye in safe zone.
    ("icon-192-maskable.png", 192, True),
    ("icon-512-maskable.png", 512, True),
    # iOS: two variants, both rounded-square opaque.
    ("apple-touch-icon.png", 180, False),
    ("apple-touch-icon-precomposed.png", 180, False),
    # browser tab
    ("favicon-32.png", 32, False),
]

for fn, sz, mk in TARGETS:
    im = make_icon(sz, mk)
    if im.mode != "RGB":
        im = im.convert("RGB")
    p = os.path.join(HERE, fn)
    im.save(p, "PNG", optimize=(sz >= 180))
    print("wrote %-32s %dx%d  %d bytes" % (fn, sz, sz, os.path.getsize(p)))
