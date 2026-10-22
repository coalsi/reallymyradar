"""Settings: one JSON file that both the web UI and a human (or Claude) can edit.

Settings are split by what a change costs:
  - render.*      re-render only (instant; tiles are keyed by version)
  - processing.*  re-decode cached volumes (a few seconds)
  - data.*        what gets downloaded and kept
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Literal

from platformdirs import user_data_dir
from pydantic import BaseModel, Field

# Per-user app folder (macOS ~/Library/Application Support/ReallymyRadar, Windows %LOCALAPPDATA%\ReallymyRadar,
# Linux ~/.local/share/reallymyradar). RMR_HOME overrides it, e.g. for Docker volumes or portable installs.
APP_HOME = Path(os.environ.get("RMR_HOME") or user_data_dir("ReallymyRadar", appauthor=False))
DATA_DIR = APP_HOME / "data"
SETTINGS_PATH = APP_HOME / "settings.json"
APP_HOME.mkdir(parents=True, exist_ok=True)


class ProductStyle(BaseModel):
    palette: str
    vmin: float          # palette range start (stored units: dBZ, m/s, dB, unitless, deg)
    vmax: float
    threshold: float | None = None   # values below this are transparent
    opacity: float = 0.85


def _default_styles() -> dict[str, ProductStyle]:
    return {
        "REF": ProductStyle(palette="nws_smooth", vmin=-5, vmax=75, threshold=5),
        "CREF": ProductStyle(palette="nws_smooth", vmin=-5, vmax=75, threshold=5),
        "VEL": ProductStyle(palette="velocity", vmin=-40, vmax=40),
        "SW": ProductStyle(palette="width", vmin=0, vmax=15, threshold=1),
        "ZDR": ProductStyle(palette="zdr", vmin=-2, vmax=6),
        "CC": ProductStyle(palette="cc", vmin=0.2, vmax=1.05, threshold=0.2),
        "PHI": ProductStyle(palette="phi", vmin=0, vmax=360),
    }


class RenderSettings(BaseModel):
    default_product: Literal["CREF", "REF", "VEL", "SW", "ZDR", "CC", "PHI"] = "CREF"
    interpolation: Literal["nearest", "bilinear"] = "bilinear"
    supersample: int = Field(2, ge=1, le=4, description="render at Nx then downsample for smooth edges")
    tile_size: int = Field(512, description="256 or 512 (retina)")
    velocity_units: Literal["kt", "m/s", "mph"] = "kt"
    styles: dict[str, ProductStyle] = Field(default_factory=_default_styles)


class ProcessingSettings(BaseModel):
    despeckle_gates: int = Field(4, ge=0, le=50, description="drop isolated echo blobs smaller than N gates (0=off)")
    rhohv_clutter_filter: float = Field(0.85, ge=0, le=1, description="remove echo with CC below this (0=off): bugs, birds, ground clutter")
    clutter_max_dbz: float = Field(30.0, ge=0, le=99, description="...but only when weaker than this, so tornado debris (low CC, strong echo) stays")
    dualpol_min_ref: float = Field(5.0, ge=-30, le=40, description="only show ZDR/CC/PHI where reflectivity >= this (dBZ)")
    dealias_velocity: bool = True
    keep_tilts: int = Field(4, ge=1, le=20, description="lowest N elevation angles kept per volume")
    composite: bool = True
    composite_az_res: float = Field(0.25, ge=0.1, le=1.0, description="composite grid azimuth step (deg); finer = sharper zoom, more memory")
    composite_gate_m: float = Field(125.0, ge=50, le=500, description="composite grid range step (m)")


class DataSettings(BaseModel):
    home_lat: float | None = None
    home_lon: float | None = None
    home_site: str | None = None         # None = nearest to home
    pinned_sites: list[str] = Field(default_factory=list)  # always kept warm
    realtime_chunks: bool = True         # live partial volumes (~1 min latency) vs archive only (~10 min)
    frames_to_keep: int = Field(12, ge=1, le=60)
    active_ttl_minutes: int = Field(30, ge=1, description="stop polling a site this long after its last request")
    poll_seconds: int = Field(15, ge=5)


class MosaicSettings(BaseModel):
    """Multi-radar view: every spot is drawn from the closest radar that covers it."""
    default_view: bool = Field(True, description="viewer, Pi and images open in mosaic mode")
    radius_km: float = Field(300, ge=100, le=460, description="how far each radar is trusted")
    max_radars: int = Field(10, ge=1, le=30, description="most radars pulled for one view")
    blend_km: float = Field(20, ge=0, le=100, description="width of the soft seam between radars")
    fill_min: float | None = Field(20.0, description="dBZ: a farther radar may fill a spot the closest one sees as empty only at/above this (blocked beams). null = never")
    include_tdwr: bool = Field(False, description="airport TDWRs (C-band: fine gates but attenuate in heavy rain)")
    step_minutes: int = Field(5, ge=2, le=15, description="time between animation frames")


class ImageSettings(BaseModel):
    """Defaults for /image/* composites (e.g. the Raspberry Pi)."""
    width: int = 1920
    height: int = 1080
    zoom: float = 8.0
    scale: int = Field(1, ge=1, le=3, description="pixel density: output is width*scale x height*scale")
    basemap: Literal["dark", "light", "satellite", "none"] = "dark"
    labels: bool = True
    range_rings: bool = False
    show_legend: bool = True
    show_home: bool = True


class ServerSettings(BaseModel):
    port: int = 8600
    access_token: str | None = Field(None, description="if set, requests arriving through a tunnel must carry ?token= (LAN is open)")


class Settings(BaseModel):
    render: RenderSettings = Field(default_factory=RenderSettings)
    processing: ProcessingSettings = Field(default_factory=ProcessingSettings)
    data: DataSettings = Field(default_factory=DataSettings)
    mosaic: MosaicSettings = Field(default_factory=MosaicSettings)
    image: ImageSettings = Field(default_factory=ImageSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)


class SettingsStore:
    def __init__(self, path: Path = SETTINGS_PATH):
        self.path = path
        self._lock = threading.Lock()
        self.version = 1             # bumps on every change; part of tile URLs
        self.processing_version = 1  # bumps when cached volumes must be re-decoded
        if path.exists():
            self.settings = Settings.model_validate_json(path.read_text())
        else:
            self.settings = Settings()
        self.save()

    def save(self) -> None:
        self.path.write_text(json.dumps(self.settings.model_dump(), indent=2))

    def update(self, patch: dict) -> Settings:
        with self._lock:
            merged = _deep_merge(self.settings.model_dump(), patch)
            new = Settings.model_validate(merged)
            if new.processing != self.settings.processing:
                self.processing_version += 1
            self.settings = new
            self.version += 1
            self.save()
            return new

    def reload_if_changed(self) -> bool:
        """Pick up hand edits to settings.json."""
        try:
            on_disk = Settings.model_validate_json(self.path.read_text())
        except Exception:
            return False
        if on_disk != self.settings:
            self.update(on_disk.model_dump())
            return True
        return False


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out
