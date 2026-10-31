"""Generate the tvOS asset catalog: layered app icons (parallax back/front) and top-shelf banners.

    uv run python tvos/make_assets.py
"""
import json
import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).parent / "Resources" / "Assets.xcassets"
BRAND = OUT / "App Icon & Top Shelf Image.brandassets"
INFO = {"author": "xcode", "version": 1}
INK = (11, 14, 19)
ACCENT = (77, 182, 255)
# NWS-style reflectivity ramp for the storm art
RAMP = [(0.0, (4, 233, 231)), (0.2, (1, 159, 244)), (0.35, (2, 253, 2)), (0.5, (0, 142, 0)), (0.62, (253, 248, 2)),
        (0.74, (253, 149, 0)), (0.86, (253, 0, 0)), (1.0, (248, 0, 253))]


def ramp(t):
    t = max(0.0, min(1.0, t))
    for (a, ca), (b, cb) in zip(RAMP, RAMP[1:]):
        if t <= b:
            f = (t - a) / (b - a)
            return tuple(int(ca[i] + (cb[i] - ca[i]) * f) for i in range(3))
    return RAMP[-1][1]


def storm_field(w, h, seed=7):
    """Soft storm-cell intensity field rendered with the radar palette."""
    rnd = random.Random(seed)
    small = (w // 8, h // 8)
    field = Image.new("L", small, 0)
    d = ImageDraw.Draw(field)
    for _ in range(14):  # a squall line plus scattered cells
        x = rnd.uniform(0.15, 0.95) * small[0]
        y = rnd.uniform(0.1, 0.9) * small[1]
        r = rnd.uniform(0.05, 0.16) * small[0]
        for k in range(12, 0, -1):
            v = int(255 * (1 - k / 12) ** 0.8 * rnd.uniform(0.6, 1.0))
            rr = r * k / 12
            d.ellipse((x - rr * 1.6, y - rr, x + rr * 1.6, y + rr), fill=max(v, 0))
    field = field.filter(ImageFilter.GaussianBlur(small[0] / 60)).resize((w, h), Image.BICUBIC)
    px = field.load()
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    op = out.load()
    for y in range(h):
        for x in range(w):
            v = px[x, y] / 255
            if v > 0.12:
                c = ramp((v - 0.12) / 0.88)
                op[x, y] = c + (int(255 * min(1.0, (v - 0.12) * 4) * 0.9),)
    return out


def back_layer(w, h):
    img = Image.new("RGBA", (w, h), INK + (255,))
    grid = Image.new("RGBA", (w, h), (0, 0, 0, 0))  # faint map grid, blended (drawing directly would overwrite)
    d = ImageDraw.Draw(grid)
    step = max(12, w // 24)
    for i in range(0, w, step):
        d.line((i, 0, i, h), fill=(255, 255, 255, 14))
    for j in range(0, h, step):
        d.line((0, j, w, j), fill=(255, 255, 255, 14))
    img.alpha_composite(grid)
    img.alpha_composite(storm_field(w, h))
    shade = Image.new("RGBA", (w, h), (0, 0, 0, 0))  # darken the center so the scope reads
    ImageDraw.Draw(shade).ellipse((w * 0.2, -h * 0.1, w * 0.8, h * 1.1), fill=(11, 14, 19, 150))
    img.alpha_composite(shade.filter(ImageFilter.GaussianBlur(w / 14)))
    return img


def front_layer(w, h):
    ss = 3
    W, H = w * ss, h * ss
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c, r = (W / 2, H / 2), H * 0.36
    lw = max(ss, int(H * 0.012))
    for f in (1.0, 0.66, 0.33):
        d.ellipse((c[0] - r * f, c[1] - r * f, c[0] + r * f, c[1] + r * f), outline=(255, 255, 255, 170), width=lw)
    for i in range(70):
        a0 = -90 + i
        d.pieslice((c[0] - r, c[1] - r, c[0] + r, c[1] + r), a0, a0 + 1.3, fill=ACCENT + (int(170 * (i / 70) ** 2),))
    ang = math.radians(-20)
    d.line((c[0], c[1], c[0] + r * math.cos(ang), c[1] + r * math.sin(ang)), fill=ACCENT + (255,), width=lw * 2)
    d.ellipse((c[0] - lw * 2, c[1] - lw * 2, c[0] + lw * 2, c[1] + lw * 2), fill=(255, 255, 255, 255))
    return img.resize((w, h), Image.LANCZOS)


def font(px, bold=True):
    for p, idx in (("/System/Library/Fonts/SFNS.ttf", 0), ("/System/Library/Fonts/HelveticaNeue.ttc", 1 if bold else 0)):
        try:
            f = ImageFont.truetype(p, px, index=idx)
            if p.endswith("SFNS.ttf"):
                f.set_variation_by_axes([700 if bold else 400])
            return f
        except Exception:
            continue
    return ImageFont.load_default()


def top_shelf(w, h):
    img = back_layer(w, h)
    scope = front_layer(h, h)
    img.alpha_composite(scope, (int(w * 0.08), 0))
    d = ImageDraw.Draw(img)
    d.text((int(w * 0.08) + h, h * 0.36), "ReallymyRadar", font=font(int(h * 0.16)), fill=(255, 255, 255, 255))
    d.text((int(w * 0.08) + h, h * 0.56), "Live high-resolution NEXRAD radar", font=font(int(h * 0.065), False),
           fill=(255, 255, 255, 190))
    return img


def write(path: Path, contents: dict):
    path.mkdir(parents=True, exist_ok=True)
    (path / "Contents.json").write_text(json.dumps({**contents, "info": INFO}, indent=2))


def imagestack(name, sizes):
    """sizes: [(scale, (w, h))]"""
    stack = BRAND / f"{name}.imagestack"
    write(stack, {"layers": [{"filename": "Front.imagestacklayer"}, {"filename": "Back.imagestacklayer"}]})
    for layer, maker in (("Front", front_layer), ("Back", back_layer)):
        lp = stack / f"{layer}.imagestacklayer"
        write(lp, {})
        imgs = []
        for scale, (w, h) in sizes:
            fn = f"{layer.lower()}{'' if scale == 1 else f'@{scale}x'}.png"
            im = maker(w, h)
            if layer == "Back":
                im = im.convert("RGB")  # the back layer must be opaque
            (lp / "Content.imageset").mkdir(parents=True, exist_ok=True)
            im.save(lp / "Content.imageset" / fn)
            imgs.append({"filename": fn, "idiom": "tv", "scale": f"{scale}x"})
        write(lp / "Content.imageset", {"images": imgs})


def imageset(name, fname, sizes):
    s = BRAND / f"{name}.imageset"
    s.mkdir(parents=True, exist_ok=True)
    imgs = []
    for scale, (w, h) in sizes:
        fn = f"{fname}{'' if scale == 1 else f'@{scale}x'}.png"
        top_shelf(w, h).convert("RGB").save(s / fn)
        imgs.append({"filename": fn, "idiom": "tv", "scale": f"{scale}x"})
    write(s, {"images": imgs})


if __name__ == "__main__":
    write(OUT, {})
    write(BRAND, {"assets": [
        {"filename": "App Icon - App Store.imagestack", "idiom": "tv", "role": "primary-app-icon", "size": "1280x768"},
        {"filename": "App Icon.imagestack", "idiom": "tv", "role": "primary-app-icon", "size": "400x240"},
        {"filename": "Top Shelf Image Wide.imageset", "idiom": "tv", "role": "top-shelf-image-wide", "size": "2320x720"},
        {"filename": "Top Shelf Image.imageset", "idiom": "tv", "role": "top-shelf-image", "size": "1920x720"},
    ]})
    imagestack("App Icon - App Store", [(1, (1280, 768))])
    imagestack("App Icon", [(1, (400, 240)), (2, (800, 480))])
    imageset("Top Shelf Image Wide", "TopShelfImageWide", [(1, (2320, 720)), (2, (4640, 1440))])
    imageset("Top Shelf Image", "TopShelfImage", [(1, (1920, 720)), (2, (3840, 1440))])
    write(OUT / "AccentColor.colorset", {"colors": [{"idiom": "universal", "color": {"color-space": "srgb", "components": {
        "red": f"{ACCENT[0] / 255:.3f}", "green": f"{ACCENT[1] / 255:.3f}", "blue": f"{ACCENT[2] / 255:.3f}", "alpha": "1.000"}}}]})
    print("wrote", OUT)
