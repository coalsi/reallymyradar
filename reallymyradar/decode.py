"""Level II bytes -> clean, uniform-azimuth polar sweeps per product.

Runs inside worker processes (see manager.py). Everything returned is plain
numpy + dataclasses so it pickles cheaply back to the server process.
"""
from __future__ import annotations

import io
import os
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

from .geo import ground_to_slant

os.environ.setdefault("PYART_QUIET", "1")
warnings.filterwarnings("ignore")

# product -> Py-ART field name
FIELDS = {
    "REF": "reflectivity",
    "VEL": "velocity",
    "SW": "spectrum_width",
    "ZDR": "differential_reflectivity",
    "CC": "cross_correlation_ratio",
    "PHI": "differential_phase",
}
DUALPOL = ("ZDR", "CC", "PHI")


@dataclass
class Sweep:
    product: str
    elev: float          # nominal elevation angle (deg)
    time: datetime       # sweep start
    az0: float           # azimuth (deg) of row 0 centre
    daz: float           # row spacing (deg): 0.5 super-res or 1.0
    r0: float            # range of first gate centre (m)
    dr: float            # gate spacing (m)
    data: np.ndarray     # float16 [naz, ngates], NaN = no data
    ground: bool = False  # ranges are ground distance (composite) rather than slant
    nyquist: float | None = None


@dataclass
class Volume:
    site: str
    start: datetime
    vcp: int | None
    complete: bool
    lat: float
    lon: float
    tilts: list[float] = field(default_factory=list)
    sweeps: dict[str, list[Sweep]] = field(default_factory=dict)  # product -> per tilt (None if absent)
    decode_seconds: float = 0.0


def decode(raw: bytes, site: str, proc: dict, complete: bool) -> Volume:
    import time as _t
    t_start = _t.perf_counter()
    import pyart

    radar = pyart.io.read_nexrad_archive(io.BytesIO(raw))
    units = radar.time["units"]  # "seconds since 2026-10-22T17:28:10Z"
    vstart = datetime.strptime(units.split("since ")[1].strip(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    vcp = radar.metadata.get("vcp_pattern")
    vol = Volume(site=site, start=vstart, vcp=int(vcp) if vcp is not None else None, complete=complete,
                 lat=float(radar.latitude["data"][0]), lon=float(radar.longitude["data"][0]))

    rng = radar.range["data"].astype(np.float64)
    r0, dr = float(rng[0]), float(rng[1] - rng[0])

    # --- per-sweep facts
    info = []
    for i in range(radar.nsweeps):
        s = radar.get_slice(i)
        az = radar.azimuth["data"][s].astype(np.float64)
        n = az.size
        res = 0.5 if n > 500 else 1.0
        if n < 0.97 * (360.0 / res):
            continue  # partial sweep in a live volume
        present = {p for p, f in FIELDS.items() if f in radar.fields and not np.ma.getmaskarray(radar.fields[f]["data"][s]).all()}
        info.append(dict(i=i, slice=s, az=az, res=res, elev=round(float(radar.fixed_angle["data"][i]), 1),
                         time=vstart + timedelta(seconds=float(radar.time["data"][s].min())), present=present,
                         nyq=_nyquist(radar, i)))

    tilts = sorted({x["elev"] for x in info})
    vol.tilts = tilts[: proc["keep_tilts"]]

    def pick(product: str, elev: float):
        cands = [x for x in info if x["elev"] == elev and product in x["present"]]
        if product in ("REF",) + DUALPOL:
            surv = [x for x in cands if "ZDR" in x["present"]]  # surveillance cut of a split cut
            cands = surv or cands
        if product in ("VEL", "SW"):
            best = min((x["res"] for x in cands), default=None)
            cands = [x for x in cands if x["res"] == best]
        return max(cands, key=lambda x: x["time"]) if cands else None  # latest = SAILS refresh

    for product in FIELDS:
        vol.sweeps[product] = []
        for elev in vol.tilts:
            x = pick(product, elev)
            if x is None:
                vol.sweeps[product].append(None)
                continue
            data = _field(radar, FIELDS[product], x["slice"])
            if product == "REF" or product in DUALPOL:
                data = _clean_surveillance(radar, x["slice"], product, data, proc)
            elif product in ("VEL", "SW"):
                if product == "VEL" and proc["dealias_velocity"] and x["nyq"]:
                    data = _dealias(radar, x["i"], data)
                data = _despeckle(data, max(proc["despeckle_gates"], 0))
            vol.sweeps[product].append(_uniform(product, x, data, r0, dr))

    if proc.get("composite", True):
        refs = []
        for x in info:
            if "REF" in x["present"] and "ZDR" in x["present"]:  # skip lower-quality Doppler-cut REF
                d = _clean_surveillance(radar, x["slice"], "REF", _field(radar, "reflectivity", x["slice"]), proc)
                refs.append(_uniform("REF", x, d, r0, dr))
        vol.sweeps["CREF"] = [_composite(refs, proc.get("composite_az_res", 0.25), proc.get("composite_gate_m", 125.0))] if refs else [None]

    vol.decode_seconds = _t.perf_counter() - t_start
    return vol


def _nyquist(radar, i):
    try:
        return float(radar.get_nyquist_vel(i, check_uniform=False))
    except Exception:
        return None


def _field(radar, name, s) -> np.ndarray:
    a = radar.fields[name]["data"][s]
    return np.ma.filled(a.astype(np.float32), np.nan)


def _clean_surveillance(radar, s, product, data, proc):
    ref = _field(radar, "reflectivity", s)
    thr = proc["rhohv_clutter_filter"]
    if thr > 0 and "cross_correlation_ratio" in radar.fields:
        # Non-weather (bugs, birds, ground clutter, chaff) has low CC *and* is weak. Tornado debris
        # also has low CC but is strong, so only weak echo is removed. CC is noisy gate to gate,
        # so it is judged on a 3x3 neighbourhood mean.
        from scipy import ndimage
        cc = np.nan_to_num(_field(radar, "cross_correlation_ratio", s), nan=0.0)
        cc = ndimage.uniform_filter(cc, size=3, mode="nearest")
        bad = (cc < thr) & (np.nan_to_num(ref, nan=-99.0) < proc.get("clutter_max_dbz", 30.0))
        ref = np.where(bad, np.nan, ref)
        if product in ("REF", "ZDR"):
            data = np.where(bad, np.nan, data)
    ref = _despeckle(ref, proc["despeckle_gates"])
    if product != "REF":
        weak = np.nan_to_num(ref, nan=-99.0) < proc.get("dualpol_min_ref", 5.0)
        data = np.where(weak, np.nan, data)  # dual-pol only where there is real echo
    else:
        data = ref
    return data


def _despeckle(data: np.ndarray, min_gates: int) -> np.ndarray:
    if min_gates <= 0:
        return data
    from scipy import ndimage
    valid = ~np.isnan(data)
    labels, n = ndimage.label(valid, structure=np.ones((3, 3)))
    if n == 0:
        return data
    sizes = np.bincount(labels.ravel())
    small = sizes < min_gates
    small[0] = False
    return np.where(small[labels], np.nan, data)


def _dealias(radar, i, data):
    import pyart
    try:
        one = radar.extract_sweeps([i])
        out = pyart.correct.dealias_region_based(one, vel_field="velocity", centered=True)
        return np.ma.filled(out["data"].astype(np.float32), np.nan)
    except Exception:
        return data


def _uniform(product, x, data, r0, dr) -> Sweep:
    """Resample rays onto an exact uniform azimuth grid (nearest ray), trim empty far gates."""
    az, res = x["az"] % 360.0, x["res"]
    naz = int(round(360.0 / res))
    az0 = float(np.median(np.mod(az, res)))
    centres = az0 + res * np.arange(naz)
    order = np.argsort(az)
    saz = az[order]
    pos = np.searchsorted(saz, centres)
    lo, hi = (pos - 1) % saz.size, pos % saz.size
    dlo = np.abs((centres - saz[lo] + 180) % 360 - 180)
    dhi = np.abs((centres - saz[hi] + 180) % 360 - 180)
    pick = np.where(dlo <= dhi, lo, hi)
    gap = np.minimum(dlo, dhi) > res * 0.75
    grid = data[order][pick]
    grid[gap] = np.nan
    valid_cols = np.where(~np.isnan(grid).all(axis=0))[0]
    ng = int(valid_cols[-1] + 1) if valid_cols.size else 1
    return Sweep(product=product, elev=x["elev"], time=x["time"], az0=az0, daz=res, r0=r0, dr=dr,
                 data=grid[:, :ng].astype(np.float16), nyquist=x["nyq"])


def _composite(refs: list[Sweep], daz: float = 0.25, dr: float = 125.0) -> Sweep:
    """Column-max reflectivity on a fine ground-range grid (default 0.25 deg x 125 m).

    Each tilt is sampled with the same bilinear gate blending the renderer uses, so the
    composite keeps up with the native 0.5 deg x 250 m tilts when zoomed in instead of
    stair-stepping between them.
    """
    from .render import sample  # render imports Sweep from here

    r0 = refs[0].r0
    ng = max(int((s.r0 + s.dr * s.data.shape[1] - r0) / dr) for s in refs)
    naz = int(round(360.0 / daz))
    az0 = daz / 2
    gnd, az = np.meshgrid((r0 + dr * np.arange(ng)).astype(np.float32),
                          (az0 + daz * np.arange(naz)).astype(np.float32))
    out = np.full((naz, ng), np.nan, dtype=np.float32)
    for s in refs:
        out = np.fmax(out, sample(s, gnd, az, "bilinear"))
    valid_cols = np.where(~np.isnan(out).all(axis=0))[0]
    out = out[:, : int(valid_cols[-1] + 1) if valid_cols.size else 1]
    latest = max(s.time for s in refs)
    return Sweep(product="CREF", elev=0.0, time=latest, az0=az0, daz=daz, r0=r0, dr=dr,
                 data=out.astype(np.float16), ground=True)
