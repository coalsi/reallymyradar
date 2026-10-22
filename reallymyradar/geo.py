"""Beam geometry (4/3 effective-earth model) shared by decoder and renderer."""
import numpy as np

RE = 6371000.0 * 4.0 / 3.0


def slant_to_ground(r: np.ndarray, elev_deg: float) -> np.ndarray:
    """Ground (great-circle) distance under a beam at slant range r."""
    e = np.deg2rad(elev_deg)
    h = np.sqrt(r * r + RE * RE + 2.0 * r * RE * np.sin(e)) - RE
    return RE * np.arcsin(r * np.cos(e) / (RE + h))


def ground_to_slant(s: np.ndarray, elev_deg: float, r_max: float = 500_000.0) -> np.ndarray:
    """Inverse of slant_to_ground via a dense monotonic lookup table."""
    if abs(elev_deg) < 1e-6:
        return s
    r_tab = np.linspace(0.0, r_max, 4096)
    s_tab = slant_to_ground(r_tab, elev_deg)
    return np.interp(s, s_tab, r_tab, right=np.inf)
