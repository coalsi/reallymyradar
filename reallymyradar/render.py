"""Polar sweep -> Web Mercator RGBA, rendered straight from gates (no intermediate grid).

Every output pixel is projected back to (geodesic distance, azimuth) from the
radar with an ellipsoidal azimuthal-equidistant transform, converted to slant
range with the 4/3-earth beam model, then sampled from the native 0.5 deg x
250 m gates. So zooming in keeps getting sharper until you reach single gates.
"""
from __future__ import annotations

import io
import math
import threading
from collections import OrderedDict
from functools import lru_cache

import numpy as np
from PIL import Image
from pyproj import Transformer

from .colormaps import LUT_SIZE, lut
from .decode import Sweep
from .geo import ground_to_slant

HALF = 20037508.342789244  # Web Mercator half-world (m)


@lru_cache(maxsize=256)
def _to_aeqd(lat: float, lon: float) -> Transformer:
    return Transformer.from_crs("EPSG:3857", f"+proj=aeqd +lat_0={lat} +lon_0={lon} +ellps=WGS84 +units=m",
                                always_xy=True)


class _GeomCache:
    """(site, extent, size) -> (distance, azimuth). Reused across every frame/product of an animation."""

    def __init__(self, max_bytes: int = 2_000_000_000):
        self.d: OrderedDict = OrderedDict()
        self.max_bytes, self.size = max_bytes, 0
        self.lock = threading.Lock()

    def get(self, key, make):
        with self.lock:
            if key in self.d:
                self.d.move_to_end(key)
                return self.d[key]
        v = make()
        with self.lock:
            if key not in self.d:
                self.d[key] = v
                self.size += sum(a.nbytes for a in v)
            while self.size > self.max_bytes and self.d:
                _, old = self.d.popitem(last=False)
                self.size -= sum(a.nbytes for a in old)
        return v


_geom = _GeomCache()


def mercator_grid(x0: float, y1: float, x1: float, y0: float, w: int, h: int):
    """Pixel-centre mercator coords for a box (x0,y0)-(x1,y1), row 0 at top (y1)."""
    xs = x0 + (np.arange(w) + 0.5) * (x1 - x0) / w
    ys = y1 - (np.arange(h) + 0.5) * (y1 - y0) / h
    return np.meshgrid(xs, ys)


def geometry(lat: float, lon: float, box: tuple[float, float, float, float], w: int, h: int):
    key = (round(lat, 5), round(lon, 5), tuple(round(b, 2) for b in box), w, h)

    def make():
        x0, y0, x1, y1 = box
        mx, my = mercator_grid(x0, y1, x1, y0, w, h)
        ex, ey = _to_aeqd(lat, lon).transform(mx, my)
        s = np.hypot(ex, ey).astype(np.float32)
        az = (np.degrees(np.arctan2(ex, ey)) % 360.0).astype(np.float32)
        return s, az

    return _geom.get(key, make)


def sample(sweep: Sweep, s: np.ndarray, az: np.ndarray, interpolation: str) -> np.ndarray:
    """Sweep values at ground distance s / azimuth az (float32, NaN = none)."""
    data = sweep.data
    naz, ng = data.shape
    r = s if sweep.ground else ground_to_slant(s.astype(np.float64), sweep.elev).astype(np.float32)
    fa = (az - sweep.az0) / sweep.daz
    fg = (r - sweep.r0) / sweep.dr

    ai = np.round(fa).astype(np.int64) % naz
    gi = np.round(fg).astype(np.int64)
    inside = (gi >= 0) & (gi < ng)
    near = np.full(s.shape, np.nan, dtype=np.float32)
    near[inside] = data[ai[inside], gi[inside]]
    if interpolation == "nearest":
        return near

    # Bilinear in (azimuth, range). Echo edges follow the 50% contour of the interpolated
    # "has data" weight, so outlines come out as smooth curves instead of gate-shaped blocks.
    m = (fg > -1) & (fg < ng)
    fa_m, fg_m = fa[m], fg[m]
    a0 = np.floor(fa_m).astype(np.int64)
    g0 = np.floor(fg_m).astype(np.int64)
    wa = (fa_m - a0).astype(np.float32)
    wg = (fg_m - g0).astype(np.float32)
    acc = np.zeros(a0.shape, np.float32)
    wsum = np.zeros(a0.shape, np.float32)
    for da, dg, wt in ((0, 0, (1 - wa) * (1 - wg)), (1, 0, wa * (1 - wg)), (0, 1, (1 - wa) * wg), (1, 1, wa * wg)):
        aa = (a0 + da) % naz
        gg = g0 + dg
        in_g = (gg >= 0) & (gg < ng)
        v = data[aa, np.clip(gg, 0, ng - 1)].astype(np.float32)
        ok = in_g & ~np.isnan(v)
        acc += np.where(ok, v * wt, 0)
        wsum += np.where(ok, wt, 0)
    vals = np.where(wsum >= 0.5, acc / np.maximum(wsum, 1e-6), np.nan)
    out = np.full(s.shape, np.nan, np.float32)
    out[m] = vals
    return out


def colorize(values: np.ndarray, style) -> np.ndarray:
    table = lut(style.palette, float(style.vmin), float(style.vmax))
    idx = np.clip((values - style.vmin) / (style.vmax - style.vmin) * (LUT_SIZE - 1), 0, LUT_SIZE - 1)
    idx = np.nan_to_num(idx, nan=0).astype(np.int32)
    rgba = table[idx]
    hide = np.isnan(values)
    if style.threshold is not None:
        hide |= values < style.threshold
    rgba[..., 3] = np.where(hide, 0, (rgba[..., 3].astype(np.float32) * style.opacity)).astype(np.uint8)
    return rgba


def render_box(sweep: Sweep, lat: float, lon: float, box, w: int, h: int, style, interpolation: str,
               supersample: int = 1) -> Image.Image:
    ss = max(1, supersample)
    s, az = geometry(lat, lon, box, w * ss, h * ss)
    return to_image(sample(sweep, s, az, interpolation), style, ss)


def to_image(vals: np.ndarray, style, ss: int) -> Image.Image:
    """Values rendered at ss x the output size -> colored, downsampled RGBA."""
    img = Image.fromarray(colorize(vals, style), "RGBA")
    if ss > 1:
        img = img.convert("RGBa").reduce(ss).convert("RGBA")  # premultiplied so edges don't go dark
    return img


def tile_box(z: int, x: int, y: int):
    span = 2 * HALF / (2 ** z)
    x0 = -HALF + x * span
    y1 = HALF - y * span
    return (x0, y1 - span, x0 + span, y1)


def tile_in_range(lat: float, lon: float, z: int, x: int, y: int, max_range_m: float) -> bool:
    """Cheap reject for tiles nowhere near the radar."""
    x0, y0, x1, y1 = tile_box(z, x, y)
    rx, ry = lonlat_to_merc(lon, lat)
    k = 1.0 / math.cos(math.radians(lat))  # mercator stretch
    dx = max(x0 - rx, 0, rx - x1)
    dy = max(y0 - ry, 0, ry - y1)
    return math.hypot(dx, dy) / k <= max_range_m * 1.02


def lonlat_to_merc(lon: float, lat: float):
    x = math.radians(lon) * 6378137.0
    y = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * 6378137.0
    return x, y


def merc_to_lonlat(x: float, y: float):
    lon = math.degrees(x / 6378137.0)
    lat = math.degrees(2 * math.atan(math.exp(y / 6378137.0)) - math.pi / 2)
    return lon, lat


def max_range(sweep: Sweep) -> float:
    return sweep.r0 + sweep.dr * sweep.data.shape[1]


def png_bytes(img: Image.Image, level: int = 3) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=level)
    return buf.getvalue()


@lru_cache(maxsize=4)
def empty_png(size: int) -> bytes:
    return png_bytes(Image.new("RGBA", (size, size), (0, 0, 0, 0)), 9)
