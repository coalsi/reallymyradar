"""Color palettes as (value, RGBA) stops in the product's native units.

Add a palette here and it shows up in the settings UI automatically.
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np


def _hex(h: str, a: float = 1.0):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) + (int(a * 255),)


# smooth: interpolate between stops; step: hard bands (classic NWS look)
PALETTES: dict[str, dict] = {
    "hd": {"units": "dBZ", "smooth": True, "stops": [
        (-5, _hex("#1b2b3a", 0.0)), (5, _hex("#2f5f82", 0.45)), (12, _hex("#3f8fc4", 0.7)),
        (18, _hex("#38c3a0", 0.85)), (22, _hex("#2fd35a")), (28, _hex("#1f9e3a")), (33, _hex("#16702a")),
        (35, _hex("#f4e04d")), (40, _hex("#f5b63b")), (45, _hex("#f07a2a")), (50, _hex("#e0302b")),
        (55, _hex("#b5122e")), (60, _hex("#7d0a2a")), (63, _hex("#d63fd0")), (68, _hex("#f0a6ff")),
        (75, _hex("#ffffff")),
    ]},
    "nws": {"units": "dBZ", "smooth": False, "stops": [
        (5, _hex("#04e9e7")), (10, _hex("#019ff4")), (15, _hex("#0300f4")), (20, _hex("#02fd02")),
        (25, _hex("#01c501")), (30, _hex("#008e00")), (35, _hex("#fdf802")), (40, _hex("#e5bc00")),
        (45, _hex("#fd9500")), (50, _hex("#fd0000")), (55, _hex("#d40000")), (60, _hex("#bc0000")),
        (65, _hex("#f800fd")), (70, _hex("#9854c6")), (75, _hex("#fdfdfd")),
    ]},
    "nws_smooth": {"units": "dBZ", "smooth": True, "stops": [
        (5, _hex("#04e9e7", 0.6)), (10, _hex("#019ff4")), (15, _hex("#0300f4")), (20, _hex("#02fd02")),
        (25, _hex("#01c501")), (30, _hex("#008e00")), (35, _hex("#fdf802")), (40, _hex("#e5bc00")),
        (45, _hex("#fd9500")), (50, _hex("#fd0000")), (55, _hex("#d40000")), (60, _hex("#bc0000")),
        (65, _hex("#f800fd")), (70, _hex("#9854c6")), (75, _hex("#fdfdfd")),
    ]},
    "mono": {"units": "dBZ", "smooth": True, "stops": [
        (-5, _hex("#0d1b2a", 0.0)), (10, _hex("#24476b", 0.6)), (25, _hex("#4f86b8")), (40, _hex("#a9d1f5")),
        (55, _hex("#ffffff")), (65, _hex("#ffd84d")), (75, _hex("#ff4d4d")),
    ]},
    "velocity": {"units": "m/s", "smooth": True, "stops": [
        (-40, _hex("#c8fbff")), (-32, _hex("#2fe7f0")), (-24, _hex("#00ff6a")), (-16, _hex("#00c23a")),
        (-8, _hex("#0b7a1f")), (-1.5, _hex("#2e4a33")), (0, _hex("#6b6b6b", 0.6)), (1.5, _hex("#4d2b2b")),
        (8, _hex("#8a0c0c")), (16, _hex("#d10f0f")), (24, _hex("#ff4b2b")), (32, _hex("#ffa86b")),
        (40, _hex("#fff0c8")),
    ]},
    "width": {"units": "m/s", "smooth": True, "stops": [
        (0, _hex("#2b2b2b", 0.0)), (2, _hex("#5a5a5a", 0.7)), (5, _hex("#9a9a9a")), (8, _hex("#e8d33a")),
        (11, _hex("#f07a2a")), (14, _hex("#d42020")), (15, _hex("#ff7af0")),
    ]},
    "zdr": {"units": "dB", "smooth": True, "stops": [
        (-2, _hex("#3a3a3a")), (-0.5, _hex("#8f8f8f")), (0, _hex("#2c2ca8")), (0.5, _hex("#3a80d9")),
        (1, _hex("#39c2e0")), (1.5, _hex("#35c47a")), (2, _hex("#9ee03a")), (2.5, _hex("#f4e04d")),
        (3, _hex("#f5a33b")), (4, _hex("#e0302b")), (5, _hex("#b5126e")), (6, _hex("#ffffff")),
    ]},
    "cc": {"units": "", "smooth": True, "stops": [
        (0.2, _hex("#1a1446")), (0.45, _hex("#3b2a8f")), (0.6, _hex("#5b5bd6")), (0.7, _hex("#7fb2f0")),
        (0.8, _hex("#3fcf7a")), (0.85, _hex("#a6e03a")), (0.9, _hex("#f4e04d")), (0.93, _hex("#f5a33b")),
        (0.96, _hex("#e0302b")), (0.98, _hex("#9e0f3a")), (1.0, _hex("#e6a3ff")), (1.05, _hex("#ffffff")),
    ]},
    "phi": {"units": "deg", "smooth": True, "stops": [
        (0, _hex("#3b1c8f")), (60, _hex("#2f6fd6")), (120, _hex("#2fc4b0")), (180, _hex("#7ad63a")),
        (240, _hex("#f4d04d")), (300, _hex("#f0702a")), (360, _hex("#c41e3a")),
    ]},
}

LUT_SIZE = 2048


@lru_cache(maxsize=64)
def lut(palette: str, vmin: float, vmax: float) -> np.ndarray:
    """RGBA uint8 [LUT_SIZE, 4] spanning vmin..vmax."""
    p = PALETTES[palette]
    vals = np.array([s[0] for s in p["stops"]], dtype=np.float64)
    cols = np.array([s[1] for s in p["stops"]], dtype=np.float64)
    x = np.linspace(vmin, vmax, LUT_SIZE)
    if p["smooth"]:
        out = np.stack([np.interp(x, vals, cols[:, c]) for c in range(4)], axis=1)
    else:
        idx = np.clip(np.searchsorted(vals, x, side="right") - 1, 0, len(vals) - 1)
        out = cols[idx]
        out[x < vals[0]] = 0
    return np.round(out).astype(np.uint8)


def palettes_for_ui() -> dict:
    return {k: {"units": v["units"], "smooth": v["smooth"],
                "stops": [[s[0], "#%02x%02x%02x" % s[1][:3], round(s[1][3] / 255, 3)] for s in v["stops"]]}
            for k, v in PALETTES.items()}
