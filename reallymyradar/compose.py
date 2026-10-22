"""Finished, self-contained radar pictures for dumb displays (Raspberry Pi, tablets, widgets).

basemap (Esri, cached on disk) -> radar -> map labels -> overlays (home, rings, legend, timestamp)
"""
from __future__ import annotations

import asyncio
import io
import math
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from pyproj import Geod

from . import render as R
from .colormaps import lut, PALETTES
from .config import DATA_DIR

TILE_DIR = DATA_DIR / "basemap"
ESRI = "https://services.arcgisonline.com/ArcGIS/rest/services"
STYLES = {  # (base, labels) -- keyless Esri tile services, {z}/{y}/{x}, 256 px
    "dark": ("Canvas/World_Dark_Gray_Base", "Canvas/World_Dark_Gray_Reference"),
    "light": ("Canvas/World_Light_Gray_Base", "Canvas/World_Light_Gray_Reference"),
    "satellite": ("World_Imagery", "Reference/World_Boundaries_and_Places"),
}
MAX_Z = 16
TS = 256
PRODUCT_NAMES = {"REF": "Reflectivity", "CREF": "Composite Reflectivity", "VEL": "Velocity",
                 "SW": "Spectrum Width", "ZDR": "Differential Reflectivity", "CC": "Correlation Coefficient",
                 "PHI": "Differential Phase"}
_geod = Geod(ellps="WGS84")
_http: httpx.AsyncClient | None = None
_base_cache: OrderedDict = OrderedDict()


def font(size: int, bold: bool = False):
    for path, idx in (("/System/Library/Fonts/SFNS.ttf", 0), ("/System/Library/Fonts/HelveticaNeue.ttc", 1 if bold else 0)):
        try:
            f = ImageFont.truetype(path, size, index=idx)
            if path.endswith("SFNS.ttf"):
                try:
                    f.set_variation_by_axes([700 if bold else 500])
                except Exception:
                    pass
            return f
        except Exception:
            continue
    return ImageFont.load_default()


async def _tile(layer: str, z: int, x: int, y: int) -> Image.Image | None:
    global _http
    path = TILE_DIR / layer.replace("/", "_") / str(z) / str(x) / f"{y}.img"
    if not path.exists():
        if _http is None:
            _http = httpx.AsyncClient(timeout=20, headers={"User-Agent": "ReallymyRadar/0.1 (personal radar display)"})
        try:
            r = await _http.get(f"{ESRI}/{layer}/MapServer/tile/{z}/{y}/{x}")
            r.raise_for_status()
        except Exception:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(r.content)
    return Image.open(path).convert("RGBA")


async def _mosaic(layer: str, box, w: int, h: int) -> Image.Image:
    """Stitch tiles covering mercator `box` and resample to w x h."""
    x0, y0, x1, y1 = box
    res_out = (x1 - x0) / w
    zt = max(0, min(MAX_Z, math.ceil(math.log2(2 * R.HALF / (TS * res_out)))))
    span = 2 * R.HALF / 2 ** zt
    tx0, tx1 = int((x0 + R.HALF) // span), int((x1 + R.HALF) // span)
    ty0, ty1 = int((R.HALF - y1) // span), int((R.HALF - y0) // span)
    n = 2 ** zt
    canvas = Image.new("RGBA", ((tx1 - tx0 + 1) * TS, (ty1 - ty0 + 1) * TS), (0, 0, 0, 0))
    coords = [(tx, ty) for tx in range(tx0, tx1 + 1) for ty in range(ty0, ty1 + 1) if 0 <= ty < n]
    tiles = await asyncio.gather(*[_tile(layer, zt, tx % n, ty) for tx, ty in coords])
    for (tx, ty), t in zip(coords, tiles):
        if t is not None:
            canvas.paste(t, ((tx - tx0) * TS, (ty - ty0) * TS))
    px = TS / span
    left = (x0 - (-R.HALF + tx0 * span)) * px
    top = ((R.HALF - ty0 * span) - y1) * px
    crop = canvas.crop((round(left), round(top), round(left + (x1 - x0) * px), round(top + (y1 - y0) * px)))
    return crop.resize((w, h), Image.LANCZOS)


async def base_layers(style: str, box, w: int, h: int):
    key = (style, tuple(round(b, 1) for b in box), w, h)
    if key in _base_cache:
        _base_cache.move_to_end(key)
        return _base_cache[key]
    if style == "none":
        layers = (Image.new("RGBA", (w, h), (11, 14, 19, 255)), None)
    else:
        base_l, label_l = STYLES[style]
        layers = await asyncio.gather(_mosaic(base_l, box, w, h), _mosaic(label_l, box, w, h))
    _base_cache[key] = layers
    if len(_base_cache) > 24:
        _base_cache.popitem(last=False)
    return layers


def view_box(lat: float, lon: float, zoom: float, w: int, h: int):
    """Mercator box for a w x h logical-pixel view at slippy-map zoom `zoom`."""
    res = 2 * R.HALF / (256 * 2 ** zoom)
    cx, cy = R.lonlat_to_merc(lon, lat)
    return (cx - w / 2 * res, cy - h / 2 * res, cx + w / 2 * res, cy + h / 2 * res)


def _to_px(lon, lat, box, w, h):
    x, y = R.lonlat_to_merc(lon, lat)
    return (x - box[0]) / (box[2] - box[0]) * w, (box[3] - y) / (box[3] - box[1]) * h


def compose_frame(base, labels, radar: Image.Image, *, box, w, h, scale, site, site_lat, site_lon,
                  product, tilt, sweep_time: datetime, style, img_settings, home, vel_units: str) -> Image.Image:
    out = base.copy()
    out.alpha_composite(radar)
    if labels is not None and img_settings.labels:
        out.alpha_composite(labels)
    d = ImageDraw.Draw(out, "RGBA")
    s = scale

    if img_settings.range_rings and site_lat is not None:
        for km in (50, 100, 150, 200, 250):
            lons, lats, _ = _geod.fwd([site_lon] * 181, [site_lat] * 181, np.linspace(0, 360, 181), [km * 1000] * 181)
            pts = [_to_px(lo, la, box, w, h) for lo, la in zip(lons, lats)]
            d.line(pts, fill=(255, 255, 255, 46), width=max(1, s))
    if site_lat is not None:
        sx, sy = _to_px(site_lon, site_lat, box, w, h)
        d.rectangle((sx - 3 * s, sy - 3 * s, sx + 3 * s, sy + 3 * s), fill=(255, 255, 255, 200))

    if img_settings.show_home and home:
        hx, hy = _to_px(home[1], home[0], box, w, h)
        d.ellipse((hx - 9 * s, hy - 9 * s, hx + 9 * s, hy + 9 * s), outline=(255, 255, 255, 230), width=2 * s)
        d.ellipse((hx - 4 * s, hy - 4 * s, hx + 4 * s, hy + 4 * s), fill=(77, 182, 255, 255))

    # header
    local = sweep_time.astimezone()
    age = max(0, int((datetime.now(timezone.utc) - sweep_time).total_seconds() // 60))
    title = f"{site}  ·  {PRODUCT_NAMES.get(product, product)}" + (f"  {tilt:.1f}°" if product != "CREF" else "")
    sub = f"{local:%-I:%M %p %Z}  ·  {age} min ago"
    f1, f2 = font(22 * s, True), font(16 * s)
    pad = 18 * s
    tw = max(d.textlength(title, font=f1), d.textlength(sub, font=f2)) + 2 * pad
    d.rounded_rectangle((pad, pad, pad + tw, pad + 74 * s), radius=12 * s, fill=(10, 13, 18, 190))
    d.text((2 * pad, pad + 12 * s), title, font=f1, fill=(255, 255, 255, 255))
    d.text((2 * pad, pad + 44 * s), sub, font=f2, fill=(255, 255, 255, 170))

    if img_settings.show_legend:
        _legend(d, out, style, product, vel_units, s, pad, h)
    return out


def _legend(d, out, style, product, vel_units, s, pad, h):
    bw, bh = 320 * s, 12 * s
    x0, y0 = pad + 14 * s, h - pad - 46 * s
    d.rounded_rectangle((pad, y0 - 14 * s, pad + bw + 28 * s, h - pad), radius=12 * s, fill=(10, 13, 18, 190))
    table = lut(style.palette, float(style.vmin), float(style.vmax))
    idx = np.linspace(0, len(table) - 1, bw).astype(int)
    bar = np.repeat(table[idx][None, :, :], bh, axis=0).copy()
    bar[..., 3] = 255
    out.paste(Image.fromarray(bar, "RGBA"), (x0, y0))
    f = font(12 * s)
    conv, unit = 1.0, PALETTES[style.palette]["units"]
    if product == "VEL" or product == "SW":
        conv, unit = {"kt": (1.94384, "kt"), "mph": (2.23694, "mph"), "m/s": (1.0, "m/s")}[vel_units]
    for i in range(6):
        v = style.vmin + (style.vmax - style.vmin) * i / 5
        x = x0 + bw * i / 5
        label = f"{v * conv:.0f}" if abs(style.vmax - style.vmin) > 5 else f"{v * conv:.2f}"
        d.text((x, y0 + bh + 4 * s), label, font=f, fill=(255, 255, 255, 190), anchor="ma")
    d.text((x0 + bw, y0 - 2 * s), unit, font=f, fill=(255, 255, 255, 140), anchor="rd")


def encode(img: Image.Image, fmt: str) -> tuple[bytes, str]:
    buf = io.BytesIO()
    if fmt == "jpg":
        img.convert("RGB").save(buf, "JPEG", quality=92)
        return buf.getvalue(), "image/jpeg"
    if fmt == "webp":
        img.save(buf, "WEBP", quality=92)
        return buf.getvalue(), "image/webp"
    img.save(buf, "PNG", compress_level=3)
    return buf.getvalue(), "image/png"


def encode_loop(frames: list[Image.Image], ms: int, hold_last_ms: int) -> bytes:
    buf = io.BytesIO()
    durations = [ms] * (len(frames) - 1) + [hold_last_ms]
    frames[0].save(buf, "WEBP", save_all=True, append_images=frames[1:], duration=durations, loop=0, quality=88)
    return buf.getvalue()
