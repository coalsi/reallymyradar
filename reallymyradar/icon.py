"""The app icon, drawn in code so every platform's format can be generated from one place.

python -m reallymyradar.icon   writes assets/icon.png, icon.ico and icon.icns (used by the packagers)
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

ASSETS = Path(__file__).parent / "assets"
INK = (11, 14, 19, 255)
ACCENT = (77, 182, 255)


def draw(size: int = 1024, status: str = "ok", tray: bool = False) -> Image.Image:
    """Radar scope: rings, a sweep wedge and a storm cell. `status` tints the blip: ok / loading / error.
    `tray` uses a round dark disc and fewer rings so it reads at 16-22 px in a menu bar or taskbar."""
    ss = 4  # draw big, downsample for clean edges
    S = size * ss
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = S / 2
    if tray:  # dark disc so it reads on light and dark menu bars / taskbars alike
        d.ellipse((S * 0.02, S * 0.02, S * 0.98, S * 0.98), fill=INK)
    else:
        d.rounded_rectangle((0, 0, S - 1, S - 1), radius=S * 0.22, fill=INK)
    r = S * (0.40 if not tray else 0.38)
    w = max(ss, int(S * (0.018 if not tray else 0.045)))
    ring = (255, 255, 255, 90 if not tray else 150)
    for f in ((1.0, 0.66, 0.33) if not tray else (1.0, 0.5)):
        d.ellipse((c - r * f, c - r * f, c + r * f, c + r * f), outline=ring, width=w)
    # sweep wedge fading behind the beam
    for i in range(60):
        a0 = -90 + i * 1.0
        alpha = int(150 * (i / 60) ** 2)
        d.pieslice((c - r, c - r, c + r, c + r), a0, a0 + 1.2, fill=ACCENT + (alpha,))
    ang = math.radians(-30)
    d.line((c, c, c + r * math.cos(ang), c + r * math.sin(ang)), fill=ACCENT + (255,), width=w * 2)
    # storm blip
    blip = {"ok": (61, 220, 132), "loading": (245, 182, 59), "error": (240, 80, 70)}[status]
    bx, by, br = c + r * 0.42, c - r * 0.38, r * 0.17
    d.ellipse((bx - br, by - br, bx + br, by + br), fill=blip + (255,))
    d.ellipse((c - w * 1.5, c - w * 1.5, c + w * 1.5, c + w * 1.5), fill=(255, 255, 255, 255))
    return img.resize((size, size), Image.LANCZOS)


def write_assets():
    ASSETS.mkdir(exist_ok=True)
    big = draw(1024)
    big.save(ASSETS / "icon.png")
    big.save(ASSETS / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    big.save(ASSETS / "icon.icns")
    draw(64, tray=True).save(ASSETS / "tray.png")
    print("wrote", sorted(p.name for p in ASSETS.iterdir()))


if __name__ == "__main__":
    write_assets()
