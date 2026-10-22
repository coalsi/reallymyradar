"""Multi-radar mosaic: every spot is drawn from the radar that sees it best.

Beam width grows with range (0.5 deg is ~0.9 km at 100 km, ~2.6 km at 300 km), so for
each output pixel the *closest* radar covering it owns the value. Around the
equidistant seam between two radars their values are blended over `blend_km` so there
is no visible edge. Where the owner reports nothing but a farther radar sees real
precipitation (>= fill_min dBZ), the farther radar fills in -- that is what repairs
terrain-blocked beams without smearing blurry far-radar halos around every storm edge.

Radars scan on their own clocks, so animation frames sit on a fixed time grid
(every `step_minutes`); each radar contributes its newest sweep at or before the slot
time. A final "live" frame uses every radar's newest sweep.
"""
from __future__ import annotations

import hashlib
import math
from datetime import datetime, timedelta, timezone

import numpy as np

from . import render as R

MOSAIC_PRODUCTS = ("CREF", "REF", "ZDR", "CC")  # VEL/SW/PHI are relative to each radar
FILL_PRODUCTS = ("CREF", "REF")
MAX_AGE = timedelta(minutes=12)    # a sweep older than this does not count for a slot
LIVE_MAX_AGE = timedelta(minutes=20)


# ---------------- which radars ----------------

def _km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = math.sin((lat2 - lat1) * p / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(min(1.0, a)))


def candidates(sites: dict, south: float, west: float, north: float, east: float, cfg) -> list[str]:
    """Radars whose trusted radius reaches the box, nearest to the box centre first."""
    clat, clon = (south + north) / 2, (west + east) / 2
    out = []
    for sid, v in sites.items():
        if v["kind"] == "TDWR" and not cfg.include_tdwr:
            continue
        near = _km(v["lat"], v["lon"], min(max(v["lat"], south), north), min(max(v["lon"], west), east))
        if near <= cfg.radius_km:
            out.append((_km(v["lat"], v["lon"], clat, clon), sid))
    return [sid for _, sid in sorted(out)[: cfg.max_radars]]


def tile_bounds(z: int, x: int, y: int):
    x0, y0, x1, y1 = R.tile_box(z, x, y)
    west, south = R.merc_to_lonlat(x0, y0)
    east, north = R.merc_to_lonlat(x1, y1)
    return south, west, north, east


def box_bounds(box):
    west, south = R.merc_to_lonlat(box[0], box[1])
    east, north = R.merc_to_lonlat(box[2], box[3])
    return south, west, north, east


# ---------------- time slots ----------------

def _slot_id(t: datetime) -> str:
    return "S" + t.strftime("%Y%m%dT%H%M")


def _parse_slot(fid: str) -> datetime:
    return datetime.strptime(fid[1:], "%Y%m%dT%H%M").replace(tzinfo=timezone.utc)


def pick(sweeps: list[tuple], fid: str):
    """The (volume, sweep, gen) a radar contributes to frame `fid` ("L"/"latest" = live)."""
    if not sweeps:
        return None
    if fid in ("L", "latest"):
        v = sweeps[-1]
        return v if datetime.now(timezone.utc) - v[1].time <= LIVE_MAX_AGE else None
    t = _parse_slot(fid)
    best = None
    for entry in sweeps:
        if entry[1].time <= t + timedelta(seconds=30):
            best = entry
    return best if best is not None and t - best[1].time <= MAX_AGE else None


def _rev(chosen: dict) -> str:
    key = "|".join(f"{s}:{e[1].time.isoformat()}:{e[2]}" for s, e in sorted(chosen.items()) if e)
    return hashlib.sha1(key.encode()).hexdigest()[:10]


def frames(per_site: dict[str, list[tuple]], step_minutes: int, count: int) -> list[dict]:
    if not any(per_site.values()):
        return []
    step = timedelta(minutes=step_minutes)
    now = datetime.now(timezone.utc)
    t0 = now.replace(second=0, microsecond=0)
    t0 -= timedelta(minutes=t0.minute % step_minutes)
    out = []
    for k in range(count - 1, -1, -1):
        t = t0 - k * step
        chosen = {s: pick(sw, _slot_id(t)) for s, sw in per_site.items()}
        n = sum(1 for e in chosen.values() if e)
        if n:
            out.append({"id": _slot_id(t), "time": t.isoformat(), "complete": True, "radars": n, "rev": _rev(chosen)})
    live = {s: pick(sw, "L") for s, sw in per_site.items()}
    if any(live.values()):
        newest = max(e[1].time for e in live.values() if e)
        rev = _rev(live)
        if not out or out[-1]["rev"] != rev:
            out.append({"id": "L", "time": newest.isoformat(), "complete": False,
                        "radars": sum(1 for e in live.values() if e), "rev": "L" + rev})
    return out


# ---------------- rendering ----------------

def values(entries: list[tuple], box, w: int, h: int, product: str, interpolation: str, cfg) -> np.ndarray:
    """entries: [(radar_lat, radar_lon, sweep)] -> float32 [h, w] of mosaicked values (NaN = none)."""
    n_px = w * h
    if not entries:
        return np.full((h, w), np.nan, np.float32)
    geo = [R.geometry(lat, lon, box, w, h) for lat, lon, _ in entries]
    S = np.stack([g[0].ravel() for g in geo])
    AZ = [g[1].ravel() for g in geo]
    reach = np.array([min(R.max_range(sw), cfg.radius_km * 1000) for _, _, sw in entries], np.float32)
    cover = S <= reach[:, None]
    smin = np.where(cover, S, np.inf).min(axis=0)

    blend = max(cfg.blend_km * 1000.0, 1.0)
    acc = np.zeros(n_px, np.float32)
    wsum = np.zeros(n_px, np.float32)
    wall = np.zeros(n_px, np.float32)
    for i, (_, _, sw) in enumerate(entries):
        idx = np.nonzero(cover[i] & (S[i] - smin < blend))[0]
        if idx.size == 0:
            continue
        wt = 1.0 - (S[i, idx] - smin[idx]) / blend  # 1 for the owner, fading to 0 across the seam
        v = R.sample(sw, S[i, idx], AZ[i][idx], interpolation)
        ok = ~np.isnan(v)
        wall[idx] += wt
        acc[idx] += np.where(ok, v * wt, 0)
        wsum[idx] += np.where(ok, wt, 0)
    out = np.where((wall > 0) & (wsum >= 0.5 * wall), acc / np.maximum(wsum, 1e-6), np.nan).astype(np.float32)

    if product in FILL_PRODUCTS and cfg.fill_min is not None:
        holes = np.isnan(out) & np.isfinite(smin)
        for i, (_, _, sw) in enumerate(entries):
            idx = np.nonzero(holes & cover[i])[0]
            if idx.size == 0:
                continue
            rough = R.sample(sw, S[i, idx], AZ[i][idx], "nearest")  # cheap pre-check
            idx = idx[np.nan_to_num(rough, nan=-99) >= cfg.fill_min - 5]
            if idx.size == 0:
                continue
            v = R.sample(sw, S[i, idx], AZ[i][idx], interpolation)
            v = np.where(v >= cfg.fill_min, v, np.nan)
            out[idx] = np.fmax(out[idx], v)
    return out.reshape(h, w)
