"""HTTP API + web apps.

  /                      interactive viewer (Leaflet)
  /pi                    full-screen kiosk loop for a Raspberry Pi / spare screen
  /image/latest.png      finished picture (basemap + radar + labels + legend)
  /image/loop.webp       animated loop of the same
  /tiles/...             XYZ radar tiles for any map client (site MOSAIC = best radar per spot)
  /api/...               settings, sites, frames, status
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import compose as C
from . import mosaic as M
from . import render as R
from .colormaps import palettes_for_ui
from .config import SettingsStore
from .manager import SITES, Manager, nearest_sites

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("radar")
STATIC = Path(__file__).parent / "static"
PRODUCTS = ["CREF", "REF", "VEL", "SW", "ZDR", "CC", "PHI"]

store = SettingsStore()
manager = Manager(store)


class LRU(OrderedDict):
    def __init__(self, max_bytes: int):
        super().__init__()
        self.max_bytes, self.size = max_bytes, 0

    def put(self, k, v, nbytes: int):
        self[k] = (v, nbytes)
        self.size += nbytes
        while self.size > self.max_bytes:
            _, (_, n) = self.popitem(last=False)
            self.size -= n

    def get_(self, k):
        if k in self:
            self.move_to_end(k)
            return self[k][0]

    def reset(self):
        self.clear()
        self.size = 0


tile_cache = LRU(1_500_000_000)
image_cache = LRU(400_000_000)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await ensure_home()
    await manager.start()
    home = home_site()
    if home:
        manager.touch(home)
    yield
    await manager.stop()


app = FastAPI(title="ReallymyRadar", lifespan=lifespan)


@app.middleware("http")
async def revalidate_app_files(request: Request, call_next):
    """Make browsers re-check the viewer's HTML/JS/CSS so updates reach every device."""
    resp = await call_next(request)
    if request.url.path in ("/", "/pi") or request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.middleware("http")
async def tunnel_auth(request: Request, call_next):
    """LAN is open. Anything arriving through a tunnel (ngrok sets X-Forwarded-For) needs the token."""
    token = store.settings.server.access_token
    if token and "x-forwarded-for" in request.headers:
        given = request.query_params.get("token") or request.cookies.get("rmr_token")
        if given != token:
            return JSONResponse({"error": "token required: add ?token=..."}, status_code=401)
        resp = await call_next(request)
        if request.query_params.get("token"):
            resp.set_cookie("rmr_token", token, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
        return resp
    return await call_next(request)


# ---------------- location ----------------

async def ensure_home():
    d = store.settings.data
    if d.home_lat is not None and d.home_lon is not None:
        return
    try:  # coarse IP-based guess; set the exact spot from the viewer's "set home" button
        async with httpx.AsyncClient(timeout=8) as c:
            j = (await c.get("https://ipinfo.io/json")).json()
        lat, lon = (float(x) for x in j["loc"].split(","))
        store.update({"data": {"home_lat": lat, "home_lon": lon}})
        log.info("home location from IP: %.3f, %.3f (%s)", lat, lon, j.get("city"))
    except Exception as e:
        log.warning("could not geolocate by IP: %s", e)


def home_site() -> str | None:
    d = store.settings.data
    if d.home_site:
        return d.home_site.upper()
    if d.home_lat is not None:
        return nearest_sites(d.home_lat, d.home_lon, 1, wsr_only=True)[0]["id"]
    return None


def site_or_404(site: str) -> str:
    site = site.upper()
    if site == "HOME":
        site = home_site() or ""
    if site not in SITES:
        raise HTTPException(404, f"unknown site {site}")
    manager.touch(site)
    return site


# ---------------- API ----------------

@app.get("/api/config")
def api_config():
    d = store.settings.data
    return {"home": {"lat": d.home_lat, "lon": d.home_lon, "site": home_site()}, "products": PRODUCTS,
            "version": store.version, "processing_version": store.processing_version,
            "settings": store.settings.model_dump(), "palettes": palettes_for_ui(),
            "sites": [{"id": k, **v} for k, v in SITES.items()]}


@app.get("/api/settings")
def get_settings():
    return store.settings.model_dump()


@app.put("/api/settings")
async def put_settings(request: Request):
    patch = await request.json()
    try:
        s = store.update(patch)
    except Exception as e:
        raise HTTPException(422, str(e))
    tile_cache.reset()
    image_cache.reset()
    for st in manager.sites.values():
        st.next_poll = 0  # reprocess promptly if processing changed
    return {"version": store.version, "processing_version": store.processing_version, "settings": s.model_dump()}


@app.post("/api/home")
async def set_home(request: Request):
    j = await request.json()
    store.update({"data": {"home_lat": float(j["lat"]), "home_lon": float(j["lon"]), "home_site": j.get("site")}})
    return api_config()["home"]


@app.get("/api/sites/near")
def sites_near(lat: float, lon: float, n: int = 8, wsr_only: bool = False):
    return nearest_sites(lat, lon, n, wsr_only)


def default_site() -> str:
    return "MOSAIC" if store.settings.mosaic.default_view else "home"


def mosaic_view(product: str, tilt: int, south: float, west: float, north: float, east: float, touch: bool = True):
    """Radars for a view. Only whole views (viewer, Pi, image) start radars downloading; single
    tiles draw from radars that are already running, or edge tiles would wake half the country."""
    cfg = store.settings.mosaic
    if touch:
        members = M.candidates(SITES, south, west, north, east, cfg)
        for m in members:
            manager.touch(m)
    else:
        running = {k: SITES[k] for k in manager.active_sites()}
        members = M.candidates(running, south, west, north, east, cfg.model_copy(update={"max_radars": 99}))
    return members, {m: manager.sweeps(m, product, tilt) for m in members}


def mosaic_pick(product: str, tilt: int, fid: str, bounds, touch: bool = True):
    """-> (entries for the renderer, cache key, newest sweep time) for one frame of one area."""
    _, per = mosaic_view(product, tilt, *bounds, touch=touch)
    chosen = {m: M.pick(sw, fid) for m, sw in per.items()}
    chosen = {m: e for m, e in chosen.items() if e}
    entries = [(e[0].lat, e[0].lon, e[1]) for e in chosen.values()]
    key = tuple(sorted((m, e[1].time, e[2]) for m, e in chosen.items()))
    newest = max((e[1].time for e in chosen.values()), default=None)
    return entries, key, newest


@app.get("/api/mosaic")
def mosaic_info(south: float, west: float, north: float, east: float, product: str | None = None, tilt: int = 0):
    product = (product or store.settings.render.default_product).upper()
    if product not in M.MOSAIC_PRODUCTS:
        raise HTTPException(400, f"{product} can't be mosaicked (it's relative to each radar); use a single site")
    members, per = mosaic_view(product, tilt, south, west, north, east)
    cfg = store.settings.mosaic
    tilts = next((manager.tilts(m) for m in members if manager.tilts(m)), [])
    return {"site": "MOSAIC", "product": product, "tilts": tilts, "version": store.version,
            "frames": M.frames(per, cfg.step_minutes, store.settings.data.frames_to_keep),
            "loading": any(not manager.sites[m].volumes for m in members),
            "members": [{"id": m, "lat": SITES[m]["lat"], "lon": SITES[m]["lon"], "kind": SITES[m]["kind"],
                         "frames": len(per[m]), "status": manager.sites[m].status, "error": manager.sites[m].error}
                        for m in members]}


UNITS = {"CREF": "dBZ", "REF": "dBZ", "VEL": "m/s", "SW": "m/s", "ZDR": "dB", "CC": "", "PHI": "deg"}


def _precip(dbz: float | None) -> str:
    if dbz is None or dbz < 15:
        return "none"
    return "light" if dbz < 30 else "moderate" if dbz < 40 else "heavy" if dbz < 50 else "intense" if dbz < 60 else "extreme"


@app.get("/api/point")
async def point(lat: float, lon: float, wait: float = 0):
    """Radar values at one spot, for other apps: "is it raining at my house, and how hard?"

    Mosaic products come from the closest radar with data there; per-radar products (VEL/SW/PHI)
    from the nearest radar. `wait` (seconds) lets a first call block while radars download.
    """
    x, y = R.lonlat_to_merc(lon, lat)
    box = (x - 1, y - 1, x + 1, y + 1)
    bounds = M.box_bounds(box)
    t0 = time.time()
    while True:  # with ?wait=, hold until every radar near the spot has data (the closest may still be downloading)
        members, _ = mosaic_view("CREF", 0, *bounds)
        if time.time() - t0 >= min(wait, 120) or all(manager.sites[m].volumes for m in members):
            break
        await asyncio.sleep(1)
    cfg = store.settings.mosaic
    out, newest = {}, None
    for product in PRODUCTS:
        if product in M.MOSAIC_PRODUCTS:
            entries, used, t = mosaic_pick(product, 0, "L", bounds)
            if not entries:
                continue
            v = float(M.values(entries, box, 1, 1, product, "bilinear", cfg)[0, 0])
            radar = min((m for m, *_ in used), key=lambda m: M._km(lat, lon, SITES[m]["lat"], SITES[m]["lon"]))
        else:
            near = nearest_sites(lat, lon, 1, wsr_only=True)[0]["id"]
            manager.touch(near)
            vol, sw = manager.sweep_for(near, product, 0, "latest")
            if sw is None:
                continue
            s, az = R.geometry(vol.lat, vol.lon, box, 1, 1)
            v, t, radar = float(R.sample(sw, s, az, "bilinear")[0, 0]), sw.time, near
        newest = max(newest or t, t)
        out[product] = {"value": None if v != v else round(v, 2), "units": UNITS[product], "radar": radar,
                        "time": t.isoformat()}
    if not out:
        return JSONResponse({"lat": lat, "lon": lon, "loading": True, "products": {},
                             "hint": "radars for this spot are downloading; retry or pass ?wait=60"}, status_code=202)
    cref = out.get("CREF", {}).get("value")
    return {"lat": lat, "lon": lon, "loading": False, "time": newest.isoformat() if newest else None,
            "precipitation": _precip(cref), "products": out}


@app.get("/api/site/{site}")
def site_info(request: Request, site: str, product: str | None = None, tilt: int = 0):
    if site.upper() == "DEFAULT":
        site = default_site()
    if site.upper() == "MOSAIC" and (product or store.settings.render.default_product).upper() not in M.MOSAIC_PRODUCTS:
        site = "home"
    if site.upper() == "MOSAIC":  # Pi / image clients describe their view instead of a bbox
        p = _img_params(request)
        bounds = M.box_bounds(C.view_box(*_view_center(p), p["zoom"], p["w"], p["h"]))
        return mosaic_info(*bounds, product=product, tilt=tilt)
    site = site_or_404(site)
    product = product or store.settings.render.default_product
    st = manager.sites[site]
    return {"site": site, "lat": SITES[site]["lat"], "lon": SITES[site]["lon"], "tilts": manager.tilts(site),
            "frames": manager.frames(site, product.upper(), tilt), "status": st.status, "error": st.error,
            "kind": SITES[site]["kind"], "loading": not st.volumes, "version": store.version}


@app.get("/api/status")
def status():
    return {**manager.status(), "tile_cache_mb": round(tile_cache.size / 1e6, 1), "settings_version": store.version}


# ---------------- tiles ----------------

@app.get("/tiles/{site}/{product}/{tilt}/{fid}/{z}/{x}/{y}.png")
async def tile(site: str, product: str, tilt: int, fid: str, z: int, x: int, y: int):
    product = product.upper()
    rs = store.settings.render
    size = rs.tile_size
    if site.upper() == "MOSAIC":
        return await mosaic_tile(product, tilt, fid, z, x, y)
    site = site_or_404(site)
    vol, sw = manager.sweep_for(site, product, tilt, fid)
    if sw is None:
        return Response(R.empty_png(size), media_type="image/png", headers={"Cache-Control": "no-store"})
    gen = manager.sites[site].gens.get(vol.start, 0)
    key = (site, product, tilt, sw.time, gen, store.version, z, x, y)
    hdrs = {"Cache-Control": "public, max-age=86400" if fid != "latest" else "no-cache"}
    cached = tile_cache.get_(key)
    if cached is not None:
        return Response(cached, media_type="image/png", headers=hdrs)
    if not R.tile_in_range(vol.lat, vol.lon, z, x, y, R.max_range(sw)):
        return Response(R.empty_png(size), media_type="image/png", headers=hdrs)

    def work():
        img = R.render_box(sw, vol.lat, vol.lon, R.tile_box(z, x, y), size, size, rs.styles[product],
                           rs.interpolation, rs.supersample)
        return R.png_bytes(img)

    png = await run_in_threadpool(work)
    tile_cache.put(key, png, len(png))
    return Response(png, media_type="image/png", headers=hdrs)


async def mosaic_tile(product: str, tilt: int, fid: str, z: int, x: int, y: int):
    if product not in M.MOSAIC_PRODUCTS:
        raise HTTPException(400, f"{product} can't be mosaicked")
    rs, cfg = store.settings.render, store.settings.mosaic
    size, ss = rs.tile_size, rs.supersample
    entries, used, _ = mosaic_pick(product, tilt, fid, M.tile_bounds(z, x, y), touch=False)
    hdrs = {"Cache-Control": "public, max-age=60"}
    if not entries:
        return Response(R.empty_png(size), media_type="image/png", headers=hdrs)
    key = ("MOSAIC", product, tilt, used, store.version, z, x, y)
    cached = tile_cache.get_(key)
    if cached is not None:
        return Response(cached, media_type="image/png", headers=hdrs)

    def work():
        vals = M.values(entries, R.tile_box(z, x, y), size * ss, size * ss, product, rs.interpolation, cfg)
        return R.png_bytes(R.to_image(vals, rs.styles[product], ss))

    png = await run_in_threadpool(work)
    tile_cache.put(key, png, len(png))
    return Response(png, media_type="image/png", headers=hdrs)


# ---------------- finished images ----------------

def _view_center(p) -> tuple[float, float]:
    d = store.settings.data
    lat = p["lat"] if p["lat"] is not None else d.home_lat
    lon = p["lon"] if p["lon"] is not None else d.home_lon
    if lat is None:
        hs = SITES[home_site() or "KOKX"]
        lat, lon = hs["lat"], hs["lon"]
    return lat, lon


async def _compose(site, product, tilt, fid, lat, lon, zoom, w, h, scale, basemap):
    rs, ims, d = store.settings.render, store.settings.image, store.settings.data
    lat, lon = _view_center({"lat": lat, "lon": lon})
    box = C.view_box(lat, lon, zoom, w, h)
    W, H = w * scale, h * scale
    ss = rs.supersample
    if site == "MOSAIC":
        entries, _, newest = mosaic_pick(product, tilt, fid, M.box_bounds(box))
        if not entries:
            return None
        label, site_ll, elev, when = f"Mosaic · {len(entries)} radars", None, entries[0][2].elev, newest
        if fid.startswith("S"):
            when = M._parse_slot(fid)
        render = lambda: R.to_image(M.values(entries, box, W * ss, H * ss, product, rs.interpolation, store.settings.mosaic),
                                    rs.styles[product], ss)
    else:
        vol, sw = manager.sweep_for(site, product, tilt, fid)
        if sw is None:
            return None
        label, site_ll, elev, when = site, (vol.lat, vol.lon), sw.elev, sw.time
        render = lambda: R.render_box(sw, vol.lat, vol.lon, box, W, H, rs.styles[product], rs.interpolation, ss)
    base, labels = await C.base_layers(basemap, box, W, H)
    home = (d.home_lat, d.home_lon) if d.home_lat is not None else None

    def work():
        return C.compose_frame(base, labels, render(), box=box, w=W, h=H, scale=scale, site=label,
                               site_lat=site_ll[0] if site_ll else None, site_lon=site_ll[1] if site_ll else None,
                               product=product, tilt=elev, sweep_time=when, style=rs.styles[product],
                               img_settings=ims, home=home, vel_units=rs.velocity_units)

    return await run_in_threadpool(work)


async def _wait_for_data(site: str, product: str, tilt: int, timeout: float = 90, bounds=None):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if site == "MOSAIC":
            if mosaic_pick(product, tilt, "L", bounds)[0]:
                return True
        elif manager.sweep_for(site, product, tilt, "latest")[1] is not None:
            return True
        await asyncio.sleep(1)
    return False


def _img_params(request: Request):
    q, ims = request.query_params, store.settings.image
    return dict(
        site=q.get("site", default_site()), product=q.get("product", store.settings.render.default_product).upper(), tilt=int(q.get("tilt", 0)),
        lat=float(q["lat"]) if "lat" in q else None, lon=float(q["lon"]) if "lon" in q else None,
        zoom=float(q.get("zoom", ims.zoom)), w=int(q.get("w", ims.width)), h=int(q.get("h", ims.height)),
        scale=int(q.get("scale", ims.scale)), basemap=q.get("basemap", ims.basemap))


def _source(site: str, product: str) -> str:
    """'MOSAIC' or a concrete site id. Per-radar products (VEL/SW/PHI) fall back to the nearest radar."""
    if site.upper() == "DEFAULT":
        site = default_site()
    if site.upper() == "MOSAIC":
        if product in M.MOSAIC_PRODUCTS:
            return "MOSAIC"
        site = "home"
    return site_or_404(site)


def mosaic_view_frames(product, tilt, bounds):
    _, per = mosaic_view(product, tilt, *bounds)
    return M.frames(per, store.settings.mosaic.step_minutes, store.settings.data.frames_to_keep)


@app.get("/image/latest.{fmt}")
@app.get("/image/frame/{fid}.{fmt}")
async def image(request: Request, fmt: str, fid: str = "latest"):
    p = _img_params(request)
    site = _source(p.pop("site"), p["product"])
    if fmt not in ("png", "jpg", "webp"):
        raise HTTPException(400, "png, jpg or webp")
    bounds = M.box_bounds(C.view_box(*_view_center(p), p["zoom"], p["w"], p["h"]))
    await _wait_for_data(site, p["product"], p["tilt"], bounds=bounds)
    if site == "MOSAIC":
        if fid == "latest":
            fid = "L"
        data_key = mosaic_pick(p["product"], p["tilt"], fid, bounds)[1]
    else:
        vol, sw = manager.sweep_for(site, p["product"], p["tilt"], fid)
        data_key = (sw.time if sw else None, manager.sites[site].gens.get(vol.start if vol else None))
    key = ("img", site, fid, data_key, tuple(sorted(p.items())), fmt, store.version)
    cached = image_cache.get_(key)
    if cached is None:
        img = await _compose(site, p["product"], p["tilt"], fid, p["lat"], p["lon"], p["zoom"], p["w"], p["h"],
                             p["scale"], p["basemap"])
        if img is None:
            raise HTTPException(503, "no radar data yet for this site/product; try again shortly")
        cached = C.encode(img, fmt)
        image_cache.put(key, cached, len(cached[0]))
    data, mime = cached
    return Response(data, media_type=mime, headers={"Cache-Control": "no-cache"})


@app.get("/image/loop.webp")
async def loop(request: Request):
    p = _img_params(request)
    site = _source(p.pop("site"), p["product"])
    n = int(request.query_params.get("frames", 10))
    ms = int(request.query_params.get("ms", 450))
    bounds = M.box_bounds(C.view_box(*_view_center(p), p["zoom"], p["w"], p["h"]))
    await _wait_for_data(site, p["product"], p["tilt"], bounds=bounds)
    if site == "MOSAIC":
        frames = mosaic_view_frames(p["product"], p["tilt"], bounds)[-n:]
    else:
        frames = manager.frames(site, p["product"], p["tilt"])[-n:]
    imgs = []
    for f in frames:
        img = await _compose(site, p["product"], p["tilt"], f["id"], p["lat"], p["lon"], p["zoom"], p["w"], p["h"],
                                p["scale"], p["basemap"])
        if img is not None:
            imgs.append(img)
    if not imgs:
        raise HTTPException(503, "no radar data yet")
    data = await run_in_threadpool(C.encode_loop, imgs, ms, ms * 4)
    return Response(data, media_type="image/webp", headers={"Cache-Control": "no-cache"})


# ---------------- pages ----------------

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/pi")
def pi():
    return FileResponse(STATIC / "pi.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
