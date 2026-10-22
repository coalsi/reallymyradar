"""On-demand site manager.

A site is only downloaded/processed while something is asking for it (any API
or tile request touches it) or it is pinned in settings. Each active site gets:
  - archive backfill: the last N complete volumes, for the animation loop
  - realtime: the in-progress volume assembled from live chunks, re-decoded as
    new tilts complete (lowest tilt is usually ready ~1 minute after scan start)
Decoding runs in a process pool so many volumes/sites decode in parallel.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import fetch
from .config import DATA_DIR, SettingsStore
from .decode import Volume, decode

log = logging.getLogger("radar")
SITES: dict[str, dict] = json.loads((Path(__file__).parent / "nexrad_sites.json").read_text())
for _k, _v in SITES.items():
    # Terminal Doppler Weather Radars (airports): finer gates, shorter range, no dual-pol. TJUA is a WSR-88D.
    _v["kind"] = "TDWR" if _k.startswith("T") and _k != "TJUA" else "WSR-88D"


def nearest_sites(lat: float, lon: float, n: int = 8, wsr_only: bool = False) -> list[dict]:
    import math
    out = []
    for k, v in SITES.items():
        if wsr_only and v["kind"] == "TDWR":
            continue
        dlat, dlon = math.radians(v["lat"] - lat), math.radians(v["lon"] - lon)
        a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat)) * math.cos(math.radians(v["lat"])) * math.sin(dlon / 2) ** 2
        out.append({"id": k, "lat": v["lat"], "lon": v["lon"], "kind": v["kind"], "km": round(6371 * 2 * math.asin(math.sqrt(a)), 1)})
    return sorted(out, key=lambda s: s["km"])[:n]


def frame_id(t: datetime) -> str:
    return t.strftime("%Y%m%dT%H%M%S")


@dataclass
class SiteState:
    site: str
    last_request: float = field(default_factory=time.time)
    volumes: dict[datetime, Volume] = field(default_factory=dict)
    gens: dict[datetime, int] = field(default_factory=dict)   # processing_version each volume was decoded with
    raw_paths: dict[datetime, Path] = field(default_factory=dict)
    live_vol: int | None = None
    live_keys: list[str] = field(default_factory=list)
    live_buf: bytearray = field(default_factory=bytearray)
    live_start: datetime | None = None
    last_archive_check: float = 0.0
    next_poll: float = 0.0
    busy: bool = False
    status: str = "starting"
    error: str | None = None


class Manager:
    def __init__(self, store: SettingsStore):
        self.store = store
        self.sites: dict[str, SiteState] = {}
        workers = max(2, (os.cpu_count() or 8) - 2)
        self.pool = ProcessPoolExecutor(max_workers=workers, initializer=_exit_with_parent, initargs=(os.getpid(),))
        self.workers = workers
        self.raw_dir = DATA_DIR / "raw"
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()

    # ---------- lifecycle ----------

    async def start(self):
        # warm workers (first Py-ART import is slow)
        loop = asyncio.get_running_loop()
        await asyncio.gather(*[loop.run_in_executor(self.pool, _warm) for _ in range(self.workers)])
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task:
            self._task.cancel()
        self.pool.shutdown(cancel_futures=True)

    def touch(self, site: str) -> SiteState:
        site = site.upper()
        if site not in SITES:
            raise KeyError(site)
        st = self.sites.get(site)
        if st is None:
            st = self.sites[site] = SiteState(site)
            self._wake.set()
        st.last_request = time.time()
        return st

    def active_sites(self) -> list[str]:
        s = self.store.settings.data
        ttl = s.active_ttl_minutes * 60
        now = time.time()
        pinned = {p.upper() for p in s.pinned_sites}
        return [k for k, st in self.sites.items() if k in pinned or now - st.last_request < ttl]

    async def _run(self):
        while True:
            try:
                self.store.reload_if_changed()
                for p in self.store.settings.data.pinned_sites:
                    if p.upper() in SITES and p.upper() not in self.sites:
                        self.sites[p.upper()] = SiteState(p.upper())
                active = set(self.active_sites())
                now = time.time()
                for k, st in list(self.sites.items()):
                    if k not in active:
                        if now - st.last_request > 2 * self.store.settings.data.active_ttl_minutes * 60 and st.volumes:
                            st.volumes.clear(); st.live_buf = bytearray(); st.status = "idle (released)"
                        continue
                    if not st.busy and now >= st.next_poll:
                        st.busy = True
                        asyncio.create_task(self._refresh(st))
            except Exception:
                log.exception("manager loop")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass

    # ---------- per-site refresh ----------

    async def _refresh(self, st: SiteState):
        cfg = self.store.settings.data
        try:
            await self._reprocess_stale(st)
            if time.time() - st.last_archive_check > 240 or len(st.volumes) < min(cfg.frames_to_keep, 3):
                await self._backfill(st)
                st.last_archive_check = time.time()
            if cfg.realtime_chunks:
                await self._realtime(st)
            self._prune(st)
            st.error = None
            st.status = f"ok · {len(st.volumes)} volumes"
        except Exception as e:  # keep serving what we have
            log.exception("refresh %s", st.site)
            st.error = f"{type(e).__name__}: {e}"
        finally:
            st.next_poll = time.time() + cfg.poll_seconds
            st.busy = False

    async def _decode(self, st: SiteState, raw: bytes, complete: bool) -> Volume:
        proc = self.store.settings.processing.model_dump()
        gen = self.store.processing_version
        vol = await asyncio.get_running_loop().run_in_executor(self.pool, decode, raw, st.site, proc, complete)
        st.volumes[vol.start] = vol
        st.gens[vol.start] = gen
        return vol

    async def _backfill(self, st: SiteState):
        cfg = self.store.settings.data
        since = datetime.now(timezone.utc) - timedelta(minutes=cfg.frames_to_keep * 11 + 15)
        listing = (await fetch.list_archive(st.site, since))[-cfg.frames_to_keep:]
        have = {t.replace(microsecond=0) for t, v in st.volumes.items() if v.complete}
        todo = [(t, url) for t, url in listing if t not in have]
        if not todo:
            return
        st.status = f"downloading {len(todo)} volumes"

        async def one(t, url):
            raw = await fetch.get_bytes(url)
            path = self.raw_dir / st.site / f"{frame_id(t)}.ar2"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            vol = await self._decode(st, raw, complete=True)
            st.raw_paths[vol.start] = path

        await asyncio.gather(*[one(t, u) for t, u in todo])

    async def _realtime(self, st: SiteState):
        if st.live_vol is None:
            st.live_vol = await fetch.latest_chunk_volume(st.site)
            st.live_keys, st.live_buf = [], bytearray()
            if st.live_vol is None:
                return
        keys = await fetch.chunk_keys(st.site, st.live_vol)
        if keys and st.live_keys and keys[0] != st.live_keys[0]:
            st.live_keys, st.live_buf = [], bytearray()  # dir number wrapped onto a new volume
        new = keys[len(st.live_keys):]
        if new:
            blobs = await asyncio.gather(*[fetch.get_bytes(f"{fetch.CHUNKS}/{k}") for k in new])
            for k, b in zip(new, blobs):
                st.live_keys.append(k)
                st.live_buf += b
            done = st.live_keys[-1].endswith("-E")
            if len(st.live_keys) >= 3:  # need header + some radials before Py-ART can parse
                try:
                    vol = await self._decode(st, bytes(st.live_buf), complete=done)
                    st.live_start = vol.start
                    if done:
                        path = self.raw_dir / st.site / f"{frame_id(vol.start)}.ar2"
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(bytes(st.live_buf))
                        st.raw_paths[vol.start] = path
                except Exception as e:
                    log.warning("live decode %s: %s", st.site, e)
        if st.live_keys and st.live_keys[-1].endswith("-E"):
            nxt = fetch.next_vol(st.live_vol)
            if await fetch.chunk_keys(st.site, nxt):
                st.live_vol, st.live_keys, st.live_buf = nxt, [], bytearray()
                await self._realtime(st)
        elif not keys:
            st.live_vol = None  # feed restarted; rediscover next poll

    async def _reprocess_stale(self, st: SiteState):
        gen = self.store.processing_version
        stale = [t for t, g in st.gens.items() if g != gen and t in st.raw_paths and t in st.volumes]
        if stale:
            st.status = f"reprocessing {len(stale)} volumes"
            await asyncio.gather(*[self._decode(st, st.raw_paths[t].read_bytes(), True) for t in stale])
        if st.live_buf and st.live_start in st.gens and st.gens[st.live_start] != gen and st.live_start not in st.raw_paths:
            await self._decode(st, bytes(st.live_buf), complete=False)

    def _prune(self, st: SiteState):
        keep = self.store.settings.data.frames_to_keep
        for t in sorted(st.volumes)[:-keep]:
            st.volumes.pop(t, None)
            st.gens.pop(t, None)
            p = st.raw_paths.pop(t, None)
            if p:
                p.unlink(missing_ok=True)

    # ---------- queries ----------

    def frames(self, site: str, product: str, tilt: int) -> list[dict]:
        st = self.sites.get(site)
        if not st:
            return []
        out = []
        for t in sorted(st.volumes):
            v = st.volumes[t]
            sw = _sweep(v, product, tilt)
            if sw is None:
                continue
            out.append({"id": frame_id(sw.time), "volume": frame_id(v.start), "time": sw.time.isoformat(),
                        "elev": sw.elev, "complete": v.complete, "vcp": v.vcp,
                        "rev": f"{frame_id(sw.time)}-{st.gens.get(t, 0)}"})
        # the live volume can repeat a frame id (SAILS) -- keep the latest occurrence
        seen, uniq = set(), []
        for f in reversed(out):
            if f["id"] not in seen:
                seen.add(f["id"]); uniq.append(f)
        return list(reversed(uniq))

    def sweep_for(self, site: str, product: str, tilt: int, fid: str):
        st = self.sites.get(site)
        if not st:
            return None, None
        for t in sorted(st.volumes, reverse=True):
            v = st.volumes[t]
            sw = _sweep(v, product, tilt)
            if sw is not None and (fid == "latest" or frame_id(sw.time) == fid):
                return v, sw
        return None, None

    def sweeps(self, site: str, product: str, tilt: int) -> list[tuple]:
        """[(volume, sweep, gen)] oldest -> newest, one per distinct sweep time."""
        st = self.sites.get(site)
        if not st:
            return []
        by_time = {}
        for t in sorted(st.volumes):
            v = st.volumes[t]
            sw = _sweep(v, product, tilt)
            if sw is not None:
                by_time[sw.time] = (v, sw, st.gens.get(t, 0))
        return [by_time[k] for k in sorted(by_time)]

    def tilts(self, site: str) -> list[float]:
        st = self.sites.get(site)
        if not st or not st.volumes:
            return []
        return max(st.volumes.values(), key=lambda v: (v.complete, v.start)).tilts

    def status(self) -> dict:
        active = set(self.active_sites())
        return {"workers": self.workers, "processing_version": self.store.processing_version, "sites": [
            {"site": k, "active": k in active, "status": st.status, "error": st.error, "volumes": len(st.volumes),
             "live_volume": st.live_vol, "live_chunks": len(st.live_keys),
             "latest": frame_id(max(st.volumes)) if st.volumes else None,
             "idle_seconds": int(time.time() - st.last_request)} for k, st in self.sites.items()]}


def _sweep(v: Volume, product: str, tilt: int):
    lst = v.sweeps.get(product) or []
    if product == "CREF":
        tilt = 0
    return lst[tilt] if 0 <= tilt < len(lst) else None


def _exit_with_parent(parent_pid: int):
    """Decode workers quit on their own if the app dies without shutting the pool down (killed, crashed,
    or tray mode, where no clean shutdown runs). Otherwise they'd linger, each holding Py-ART in memory."""
    import threading

    def watch():
        while True:
            time.sleep(3)
            if os.getppid() != parent_pid:
                os._exit(0)

    threading.Thread(target=watch, daemon=True).start()


def _warm():
    import os
    os.environ["PYART_QUIET"] = "1"
    import pyart  # noqa: F401
    return True
