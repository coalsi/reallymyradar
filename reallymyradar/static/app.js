// ReallymyRadar viewer
const PRODUCT_INFO = {
  REF: ["REF", "Refl"], VEL: ["VEL", "Vel"], SW: ["SW", "Width"], ZDR: ["ZDR", "Zdr"],
  CC: ["CC", "Corr"], PHI: ["PHI", "Phase"], CREF: ["CREF", "Comp"],
};
const PRODUCT_NAMES = { REF: "Reflectivity", VEL: "Velocity", SW: "Spectrum width", ZDR: "Differential reflectivity",
  CC: "Correlation coefficient", PHI: "Differential phase", CREF: "Composite reflectivity" };
const VEL_CONV = { kt: 1.94384, "m/s": 1, mph: 2.23694 };
const MOSAIC_PRODUCTS = ["CREF", "REF", "ZDR", "CC"]; // VEL/SW/PHI only make sense relative to one radar
const TOKEN = new URLSearchParams(location.search).get("token");
const tq = TOKEN ? `token=${encodeURIComponent(TOKEN)}&` : "";

const S = {
  cfg: null, site: null, product: "REF", tilt: 0, tilts: [], frames: [], idx: -1,
  playing: false, layers: new Map(), stale: [], shown: null, version: 1, followLatest: true, settingsProduct: "REF",
};
const $ = (id) => document.getElementById(id);
const api = async (path, opts) => {
  const r = await fetch(path + (path.includes("?") ? "&" : "?") + tq.slice(0, -1), opts);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
};

// ---------- map ----------
const hash = Object.fromEntries(new URLSearchParams(location.hash.slice(1)));
const map = L.map("map", {
  zoomControl: false, attributionControl: true, zoomSnap: 0.25, wheelPxPerZoomLevel: 90, maxZoom: 15, minZoom: 4,
  // Leaflet's default fling speed is unlimited, so a quick drag could throw the map across the Pacific
  inertiaMaxSpeed: 900, inertiaDeceleration: 5000,
  maxBounds: [[5, -185], [75, -55]], maxBoundsViscosity: 0.9, // CONUS, Alaska, Hawaii, Puerto Rico
});
map.createPane("radar").style.zIndex = 350;
map.createPane("labels").style.zIndex = 450;
map.getPane("labels").style.pointerEvents = "none";
const BASEMAPS = {
  dark: ["Canvas/World_Dark_Gray_Base", "Canvas/World_Dark_Gray_Reference"],
  light: ["Canvas/World_Light_Gray_Base", "Canvas/World_Light_Gray_Reference"],
  satellite: ["World_Imagery", "Reference/World_Boundaries_and_Places"],
};
const esri = (layer, pane) => L.tileLayer(`https://services.arcgisonline.com/ArcGIS/rest/services/${layer}/MapServer/tile/{z}/{y}/{x}`, {
  maxNativeZoom: 16, maxZoom: 19, pane, attribution: pane === "tilePane" ? "Basemap © Esri · NEXRAD Level II: NOAA / Unidata" : "",
});
let basemapName = "dark";
try { basemapName = localStorage.getItem("rmr_basemap") || "dark"; } catch {}
if (!BASEMAPS[basemapName]) basemapName = "dark";
let baseLayer = esri(BASEMAPS[basemapName][0], "tilePane").addTo(map);
let labelsLayer = esri(BASEMAPS[basemapName][1], "labels").addTo(map);
function setBasemap(name) {
  const showLabels = map.hasLayer(labelsLayer);
  map.removeLayer(baseLayer); map.removeLayer(labelsLayer);
  basemapName = name;
  baseLayer = esri(BASEMAPS[name][0], "tilePane").addTo(map);
  labelsLayer = esri(BASEMAPS[name][1], "labels");
  if (showLabels) labelsLayer.addTo(map);
  try { localStorage.setItem("rmr_basemap", name); } catch {}
}
const ringsLayer = L.layerGroup();
const siteMarkers = L.layerGroup().addTo(map);
let homeMarker = null, meMarker = null;

// ---------- boot ----------
(async function boot() {
  S.cfg = await api("/api/config");
  S.version = S.cfg.version;
  const home = S.cfg.home;
  S.site = (hash.site || (S.cfg.settings.mosaic.default_view ? "MOSAIC" : home.site) || "KOKX").toUpperCase();
  S.product = (hash.p || S.cfg.settings.render.default_product || "CREF").toUpperCase();
  S.tilt = +(hash.t || 0);
  const site = S.cfg.sites.find((s) => s.id === (S.site === "MOSAIC" ? home.site || "KOKX" : S.site));
  const lat = +(hash.lat || home.lat || site.lat), lon = +(hash.lon || home.lon || site.lon);
  map.setView([lat, lon], +(hash.z || 8));
  if (home.lat != null) {
    homeMarker = L.marker([home.lat, home.lon], { icon: L.divIcon({ className: "", html: '<div class="home-marker"></div>', iconSize: [18, 18] }), interactive: false, pane: "labels" }).addTo(map);
  }
  buildProducts();
  drawSiteMarkers();
  await loadSite(true);
  const want = new URLSearchParams(location.search).get("settings"); // tray menu "Settings" / "Set up remote access…"
  if (want !== null) { S.scrollToRemote = want === "ngrok"; $("settingsBtn").click(); }
  setInterval(() => loadSite(false), 20000);
  setInterval(updateAge, 15000);
})();

function setHash() {
  const c = map.getCenter();
  history.replaceState(null, "", `${location.search}#site=${S.site}&p=${S.product}&t=${S.tilt}&z=${map.getZoom()}&lat=${c.lat.toFixed(4)}&lon=${c.lng.toFixed(4)}`);
}
let moveTimer;
map.on("moveend", () => {
  setHash(); drawSiteMarkers();
  // the mosaic's radars (and a per-radar product's auto-picked radar) follow the view
  if (S.site === "MOSAIC") { clearTimeout(moveTimer); moveTimer = setTimeout(() => loadSite(false), 400); }
});

// Which tile source to use: the mosaic, or for per-radar products in mosaic mode the radar nearest the view.
function srcSite() {
  if (S.site !== "MOSAIC" || MOSAIC_PRODUCTS.includes(S.product)) return S.site;
  const c = map.getCenter();
  let best = null, bd = Infinity;
  for (const s of S.cfg.sites) {
    if (s.kind === "TDWR") continue;
    const d = distKm(c.lat, c.lng, s.lat, s.lon);
    if (d < bd) { bd = d; best = s.id; }
  }
  return best;
}

// ---------- sites ----------
function drawSiteMarkers() {
  if (!S.cfg) return;
  siteMarkers.clearLayers();
  const b = map.getBounds().pad(0.2);
  for (const s of S.cfg.sites) {
    if (!b.contains([s.lat, s.lon])) continue;
    const cur = s.id === S.site || (S.site === "MOSAIC" && (S.members || []).includes(s.id)) || s.id === S.autoSite;
    const m = L.marker([s.lat, s.lon], { icon: L.divIcon({ className: "", html: `<div class="site-marker ${cur ? "cur" : ""} ${s.kind === "TDWR" ? "tdwr" : ""}"></div>${map.getZoom() >= 6 ? `<div class="site-label">${s.id}</div>` : ""}`, iconSize: [9, 9] }), pane: "labels" });
    m.on("click", () => switchSite(s.id));
    m.getElement?.()?.style && (m.getElement().style.pointerEvents = "auto");
    siteMarkers.addLayer(m);
  }
  map.getPane("labels").querySelectorAll(".leaflet-marker-icon").forEach((el) => (el.style.pointerEvents = "auto"));
}

$("siteBtn").onclick = async () => {
  const menu = $("siteMenu");
  if (!menu.classList.contains("hidden")) return menu.classList.add("hidden");
  const c = map.getCenter();
  const near = await api(`/api/sites/near?lat=${c.lat}&lon=${c.lng}&n=10`);
  menu.innerHTML = `<button data-id="MOSAIC" class="${S.site === "MOSAIC" ? "cur" : ""}"><span>Mosaic</span><span class="muted">best radar per spot</span></button>` + near.map((s) => `<button data-id="${s.id}" class="${s.id === S.site ? "cur" : ""}"><span>${s.id}${s.kind === "TDWR" ? ' <span class="muted">TDWR</span>' : ""}</span><span class="muted">${Math.round(s.km)} km</span></button>`).join("");
  menu.querySelectorAll("button").forEach((b) => (b.onclick = () => { menu.classList.add("hidden"); switchSite(b.dataset.id); }));
  menu.classList.remove("hidden");
};

function switchSite(id) {
  if (id === S.site) return;
  S.site = id;
  clearLayers();
  S.frames = []; S.idx = -1; S.followLatest = true;
  drawSiteMarkers();
  if (ringsLayer._map) drawRings();
  setHash();
  loadSite(true);
}

// ---------- products & tilts ----------
function buildProducts() {
  $("products").innerHTML = S.cfg.products.map((p) => `<button data-p="${p}" title="${PRODUCT_NAMES[p]}">${PRODUCT_INFO[p][0]}<small>${PRODUCT_INFO[p][1]}</small></button>`).join("");
  $("products").querySelectorAll("button").forEach((b) => (b.onclick = () => setProduct(b.dataset.p)));
  markProduct();
}
function markProduct() {
  $("products").querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.p === S.product));
  $("tilts").style.visibility = S.product === "CREF" ? "hidden" : "visible";
  drawLegend();
}
function setProduct(p) {
  if (p === S.product) return;
  S.product = p; S.settingsProduct = p;
  markProduct(); clearLayers(); setHash(); loadSite(true);
  if (!$("drawer").classList.contains("hidden")) buildSettings();
}
$("tiltUp").onclick = () => setTilt(S.tilt + 1);
$("tiltDown").onclick = () => setTilt(S.tilt - 1);
function setTilt(t) {
  const max = Math.max(0, S.tilts.length - 1);
  t = Math.max(0, Math.min(max, t));
  if (t === S.tilt) return;
  S.tilt = t; clearLayers(); setHash(); loadSite(true);
}

// ---------- frames & layers ----------
async function loadSite(initial) {
  const site = srcSite(), mode = S.site, product = S.product, tilt = S.tilt;
  let info;
  try {
    if (site === "MOSAIC") {
      const b = map.getBounds();
      info = await api(`/api/mosaic?product=${product}&tilt=${tilt}&south=${b.getSouth()}&west=${b.getWest()}&north=${b.getNorth()}&east=${b.getEast()}`);
    } else info = await api(`/api/site/${site}?product=${product}&tilt=${tilt}`);
  } catch (e) { toast(`Error: ${e.message}`); return; }
  if (site !== srcSite() || mode !== S.site || product !== S.product || tilt !== S.tilt) return; // user moved on
  S.tilts = info.tilts;
  S.members = site === "MOSAIC" ? info.members.map((m) => m.id) : [];
  const prevAuto = S.autoSite;
  S.autoSite = mode === "MOSAIC" && site !== "MOSAIC" ? site : null;
  if (S.autoSite !== prevAuto) { clearLayers(); drawSiteMarkers(); }
  if (site === "MOSAIC") drawSiteMarkers();
  const h = S.cfg.home;
  if (site === "MOSAIC") {
    $("siteName").textContent = "Mosaic";
    const ready = info.members.filter((m) => m.frames).length;
    $("siteDist").textContent = ` ${ready}/${info.members.length} radars`;
  } else {
    $("siteName").textContent = mode === "MOSAIC" ? `Auto · ${site}` : site;
    $("siteDist").textContent = h.lat != null ? ` ${Math.round(distKm(h.lat, h.lon, info.lat, info.lon))} km` : "";
  }
  $("tiltLabel").textContent = S.tilts[S.tilt] != null ? `${S.tilts[S.tilt].toFixed(1)}°` : "–";
  if (info.version !== S.version) { S.version = info.version; retireLayers(); }
  const prevRev = S.frames[S.idx]?.rev;
  S.frames = info.frames;
  const latest = S.frames[S.frames.length - 1];
  const fresh = latest && Date.now() - new Date(latest.time) < 12 * 60e3;
  $("liveDot").classList.toggle("live", !!fresh);
  if (!S.frames.length) {
    if (!info.loading && !info.error) {
      toast(`${site} has no ${PRODUCT_NAMES[product].toLowerCase()} at this tilt${info.kind === "TDWR" ? " (TDWR radars: REF/VEL/SW only)" : ""}.`);
      clearLayers(); renderTicks(); updateAge();
      return;
    }
    toast(site === "MOSAIC" ? `Fetching ${info.members.length} radars from NOAA…`
      : info.error ? `${site}: ${info.error}` : `Fetching ${site} from NOAA… (${info.status})`);
    setTimeout(() => srcSite() === site && loadSite(false), 3000);
    renderTicks();
    return;
  }
  hideToast();
  if (info.loading) setTimeout(() => srcSite() === site && loadSite(false), 4000); // more radars still arriving
  // drop layers for frames that no longer exist or were re-processed (after a beat, so the
  // replacement can load underneath instead of the map flashing empty)
  const revs = new Set(S.frames.map((f) => f.rev));
  for (const [rev, l] of S.layers) if (!revs.has(rev)) { S.layers.delete(rev); S.stale.push(l); }
  if (S.followLatest || initial) S.idx = S.frames.length - 1;
  else S.idx = Math.max(0, S.frames.findIndex((f) => f.rev === prevRev));
  S.frames.forEach((f) => ensureLayer(f));
  showFrame(S.idx);
}

function tileUrl(f) {
  return `/tiles/${srcSite()}/${S.product}/${S.tilt}/${f.id}/{z}/{x}/{y}.png?${tq}v=${S.version}&r=${f.rev}`;
}
function ensureLayer(f) {
  if (S.layers.has(f.rev)) return S.layers.get(f.rev);
  const size = S.cfg.settings.render.tile_size;
  const l = L.tileLayer(tileUrl(f), {
    pane: "radar", tileSize: size, zoomOffset: size === 512 ? -1 : 0, opacity: 0, maxNativeZoom: 14, maxZoom: 15,
    className: "radar-tiles", keepBuffer: 1, updateWhenZooming: false,
  });
  l.on("load", () => {
    l._ready = true;
    if (S.frames[S.idx]?.rev === f.rev) showFrame(S.idx); // it was waiting on us
    else renderTicks();
  });
  l.addTo(map);
  S.layers.set(f.rev, l);
  return l;
}
function clearLayers() {
  for (const l of S.layers.values()) map.removeLayer(l);
  for (const l of S.stale) map.removeLayer(l);
  S.layers.clear(); S.stale = []; S.shown = null;
}
// Same, but the current picture stays up until its replacement has loaded (settings tweaks, re-renders).
function retireLayers() {
  for (const l of S.layers.values()) S.stale.push(l);
  S.layers.clear();
}
// Swap to a frame only once its tiles are in; until then the previous picture stays up,
// so live updates and settings changes never flash an empty map.
function showFrame(i) {
  if (!S.frames.length) return;
  S.idx = (i + S.frames.length) % S.frames.length;
  const target = S.layers.get(S.frames[S.idx].rev);
  if (target && (target._ready || !S.shown)) {
    for (const l of S.layers.values()) l.setOpacity(l === target ? 1 : 0);
    for (const l of S.stale) map.removeLayer(l);
    S.stale = []; S.shown = target;
  }
  renderTicks();
  updateAge();
}
function renderTicks() {
  $("ticks").innerHTML = S.frames.map((f, i) => {
    const l = S.layers.get(f.rev);
    return `<div class="t ${l?._ready ? "loaded" : ""} ${i === S.idx ? "cur" : ""} ${f.complete ? "" : "partial"}" title="${fmtTime(f.time)}"></div>`;
  }).join("");
}
function updateAge() {
  const f = S.frames[S.idx];
  if (!f) { $("clock").textContent = "--:--"; $("age").textContent = ""; return; }
  $("clock").textContent = fmtTime(f.time);
  const mins = Math.round((Date.now() - new Date(f.time)) / 60000);
  $("age").textContent = `${mins} min ago${f.complete ? "" : " · live"}`;
}
const fmtTime = (t) => new Date(t).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

// scrub
const ticks = $("ticks");
const scrub = (e) => {
  const r = ticks.getBoundingClientRect();
  const x = (e.touches ? e.touches[0].clientX : e.clientX) - r.left;
  const i = Math.floor((x / r.width) * S.frames.length);
  S.followLatest = i >= S.frames.length - 1;
  pause(); showFrame(Math.max(0, Math.min(S.frames.length - 1, i)));
};
let dragging = false;
ticks.addEventListener("pointerdown", (e) => { dragging = true; ticks.setPointerCapture(e.pointerId); scrub(e); });
ticks.addEventListener("pointermove", (e) => dragging && scrub(e));
ticks.addEventListener("pointerup", () => (dragging = false));

// play
let timer = null;
$("playBtn").onclick = () => (S.playing ? pause() : play());
function play() {
  S.playing = true; $("playBtn").textContent = "❚❚";
  const step = () => {
    const last = S.idx === S.frames.length - 1;
    showFrame(S.idx + 1);
    timer = setTimeout(step, last ? 450 : (S.idx === S.frames.length - 1 ? 1600 : 450));
  };
  timer = setTimeout(step, 300);
}
function pause() {
  S.playing = false; $("playBtn").textContent = "▶"; clearTimeout(timer);
  S.followLatest = S.idx === S.frames.length - 1;
}
document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
  if (e.code === "Space") { e.preventDefault(); S.playing ? pause() : play(); }
  if (e.key === "ArrowLeft") { pause(); showFrame(S.idx - 1); S.followLatest = false; }
  if (e.key === "ArrowRight") { pause(); showFrame(S.idx + 1); S.followLatest = S.idx === S.frames.length - 1; }
  if (e.key === "ArrowUp") setTilt(S.tilt + 1);
  if (e.key === "ArrowDown") setTilt(S.tilt - 1);
  const p = S.cfg?.products[+e.key - 1];
  if (p) setProduct(p);
});

// ---------- legend ----------
function drawLegend() {
  if (!S.cfg) return;
  const st = S.cfg.settings.render.styles[S.product];
  const pal = S.cfg.palettes[st.palette];
  const c = $("legendBar"), ctx = c.getContext("2d");
  ctx.clearRect(0, 0, c.width, c.height);
  const stops = pal.stops;
  for (let x = 0; x < c.width; x++) {
    const v = st.vmin + (st.vmax - st.vmin) * (x / (c.width - 1));
    let col = stops[0];
    if (pal.smooth) {
      let j = stops.findIndex((s) => s[0] >= v);
      if (j <= 0) col = stops[Math.max(0, j)];
      else {
        const a = stops[j - 1], b = stops[j], t = (v - a[0]) / (b[0] - a[0]);
        col = [v, mix(a[1], b[1], t)];
      }
    } else {
      for (const s of stops) if (s[0] <= v) col = s;
    }
    ctx.fillStyle = col[1];
    ctx.globalAlpha = st.threshold != null && v < st.threshold ? 0.15 : 1;
    ctx.fillRect(x, 0, 1, c.height);
  }
  ctx.globalAlpha = 1;
  const conv = S.product === "VEL" || S.product === "SW" ? VEL_CONV[S.cfg.settings.render.velocity_units] : 1;
  const unit = S.product === "VEL" || S.product === "SW" ? S.cfg.settings.render.velocity_units : pal.units;
  const n = 6, span = st.vmax - st.vmin;
  $("legendTicks").innerHTML = Array.from({ length: n }, (_, i) => {
    const v = (st.vmin + (span * i) / (n - 1)) * conv;
    return `<span>${span > 5 ? Math.round(v) : v.toFixed(2)}</span>`;
  }).join("");
  $("legendTitle").textContent = `${PRODUCT_NAMES[S.product]}${unit ? " · " + unit : ""}`;
}
function mix(a, b, t) {
  const p = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  const A = p(a), B = p(b);
  return "#" + A.map((x, i) => Math.round(x + (B[i] - x) * t).toString(16).padStart(2, "0")).join("");
}

// ---------- tools ----------
$("labelsBtn").onclick = (e) => {
  if (map.hasLayer(labelsLayer)) map.removeLayer(labelsLayer); else labelsLayer.addTo(map);
  e.currentTarget.classList.toggle("on", map.hasLayer(labelsLayer));
};
$("baseBtn").onclick = () => {
  const names = Object.keys(BASEMAPS);
  setBasemap(names[(names.indexOf(basemapName) + 1) % names.length]);
  toast(`Basemap: ${basemapName}`, 1200);
};
$("ringsBtn").onclick = (e) => {
  if (ringsLayer._map) map.removeLayer(ringsLayer); else { drawRings(); ringsLayer.addTo(map); }
  e.currentTarget.classList.toggle("on", !!ringsLayer._map);
};
function drawRings() {
  ringsLayer.clearLayers();
  const s = S.cfg.sites.find((x) => x.id === (S.site === "MOSAIC" ? srcSite() === "MOSAIC" ? S.cfg.home.site : srcSite() : S.site));
  if (!s) return;
  for (const km of [50, 100, 150, 200, 250]) {
    L.circle([s.lat, s.lon], { radius: km * 1000, color: "#fff", weight: 1, opacity: 0.18, fill: false, interactive: false, pane: "labels" }).addTo(ringsLayer);
  }
}
$("homeBtn").onclick = () => {
  const h = S.cfg.home;
  if (h.lat == null) return toast("No home set yet — use ◎ then “Set as home” in settings.");
  map.flyTo([h.lat, h.lon], Math.max(map.getZoom(), 8), { duration: 0.8 });
  if (S.site !== "MOSAIC" && h.site && h.site !== S.site) switchSite(h.site);
};
$("locateBtn").onclick = () => {
  if (!navigator.geolocation) return toast("Location not available in this browser.");
  if (!isSecureContext) return toast("Browsers only share GPS over https or localhost — use the ngrok URL on the road.");
  toast("Locating…");
  navigator.geolocation.getCurrentPosition(async (p) => {
    const { latitude: lat, longitude: lon } = p.coords;
    S.me = { lat, lon };
    if (meMarker) meMarker.remove();
    meMarker = L.marker([lat, lon], { icon: L.divIcon({ className: "", html: '<div class="me-marker"></div>', iconSize: [14, 14] }), interactive: false, pane: "labels" }).addTo(map);
    map.flyTo([lat, lon], Math.max(map.getZoom(), 8), { duration: 0.8 });
    const near = await api(`/api/sites/near?lat=${lat}&lon=${lon}&n=1&wsr_only=true`);
    hideToast();
    if (S.site !== "MOSAIC") switchSite(near[0].id);
  }, (err) => toast(`Location failed: ${err.message}`), { enableHighAccuracy: true, timeout: 15000 });
};

let toastTimer;
function toast(msg, ms = 0) {
  $("toast").textContent = msg; $("toast").classList.remove("hidden");
  clearTimeout(toastTimer);
  if (ms) toastTimer = setTimeout(hideToast, ms);
}
function hideToast() { $("toast").classList.add("hidden"); }
function distKm(a, b, c, d) {
  const r = Math.PI / 180, x = Math.sin(((c - a) * r) / 2) ** 2 + Math.cos(a * r) * Math.cos(c * r) * Math.sin(((d - b) * r) / 2) ** 2;
  return 12742 * Math.asin(Math.sqrt(x));
}

// ---------- settings drawer ----------
$("settingsBtn").onclick = () => { $("drawer").classList.toggle("hidden"); if (!$("drawer").classList.contains("hidden")) buildSettings(); };
$("closeDrawer").onclick = () => $("drawer").classList.add("hidden");

const FIELDS = [
  ["Look", "Colors and thresholds for the product picked below. Changes show up within a second.", "style"],
  ["Rendering", null, [
    ["render.default_product", "Default product (viewer, Pi, images)", "select", ["CREF", "REF", "VEL", "SW", "ZDR", "CC", "PHI"]],
    ["render.interpolation", "Gate blending", "select", ["bilinear", "nearest"], "bilinear = smooth HD; nearest = raw gates"],
    ["render.supersample", "Edge smoothing (supersample)", "range", [1, 4, 1]],
    ["render.tile_size", "Tile size (512 = retina)", "select", [512, 256]],
    ["render.velocity_units", "Velocity units", "select", ["kt", "mph", "m/s"]],
  ]],
  ["Processing", "Re-decodes cached volumes (a few seconds).", [
    ["processing.despeckle_gates", "Despeckle: drop blobs smaller than (gates)", "range", [0, 40, 1]],
    ["processing.rhohv_clutter_filter", "Clutter filter: remove echo with CC below", "range", [0, 0.95, 0.01], "0 = off. Removes bugs, birds and ground clutter."],
    ["processing.clutter_max_dbz", "…only when weaker than (dBZ)", "range", [0, 60, 1], "Keeps strong low-CC echo such as tornado debris."],
    ["processing.dualpol_min_ref", "Dual-pol (ZDR/CC/PHI) only where REF ≥ (dBZ)", "range", [-20, 30, 1]],
    ["processing.dealias_velocity", "Dealias velocity", "check"],
    ["processing.keep_tilts", "Elevation tilts kept", "range", [1, 12, 1]],
  ]],
  ["Mosaic", "Every spot comes from the closest radar that covers it, blended at the seams.", [
    ["mosaic.default_view", "Open in mosaic mode (viewer, Pi, images)", "check"],
    ["mosaic.blend_km", "Seam blend width (km)", "range", [0, 60, 1]],
    ["mosaic.fill_min", "Fill blocked spots from farther radars at ≥ (dBZ)", "range", [0, 60, 1], "Repairs terrain-blocked beams. Lower = fills more, but adds blurry halos around storms."],
    ["mosaic.radius_km", "Trust each radar out to (km)", "range", [150, 460, 10]],
    ["mosaic.max_radars", "Most radars per view", "range", [1, 20, 1]],
    ["mosaic.include_tdwr", "Include airport TDWRs", "check", null, "Finer near airports, but C-band: weakens in heavy rain."],
    ["mosaic.step_minutes", "Minutes between loop frames", "range", [2, 15, 1]],
  ]],
  ["Data", null, [
    ["data.realtime_chunks", "Live partial volumes (~1 min latency)", "check"],
    ["data.frames_to_keep", "Frames in loop", "range", [2, 40, 1]],
    ["data.poll_seconds", "Poll interval (s)", "range", [5, 120, 5]],
    ["data.active_ttl_minutes", "Keep a site warm after last view (min)", "range", [5, 240, 5]],
    ["data.pinned_sites", "Always-on sites (comma separated)", "list"],
  ]],
  ["Pi / image output", "Defaults for /pi and /image/* — any of them can be overridden in the URL.", [
    ["image.width", "Width", "number"], ["image.height", "Height", "number"],
    ["image.zoom", "Zoom", "range", [4, 12, 0.25]],
    ["image.scale", "Pixel density (retina screens: 2)", "select", [1, 2, 3]],
    ["image.basemap", "Basemap", "select", ["dark", "light", "satellite", "none"]],
    ["image.labels", "Map labels", "check"], ["image.range_rings", "Range rings", "check"],
    ["image.show_legend", "Legend", "check"], ["image.show_home", "Home marker", "check"],
  ]],
  ["This computer", null, [
    ["server.keep_awake", "Keep this computer awake while the radar runs", "check", null, "So kiosks and other devices never lose the radar. The display can still sleep."],
  ]],
  ["Away from home (ngrok)", "See this radar on your phone anywhere, through a free ngrok tunnel. Devices on your home network keep working as they do now.", "remote"],
];

function getPath(obj, path) { return path.split(".").reduce((o, k) => o?.[k], obj); }
function patchFor(path, value) {
  const keys = path.split("."), out = {};
  keys.reduce((o, k, i) => (o[k] = i === keys.length - 1 ? value : {}), out);
  return out;
}

function buildSettings() {
  const s = S.cfg.settings, f = $("settingsForm");
  f.innerHTML = "";
  for (const [title, hint, rows] of FIELDS) {
    f.insertAdjacentHTML("beforeend", `<h3>${title}</h3>${hint ? `<p class="hint">${hint}</p>` : ""}`);
    if (rows === "style") { buildStyleRows(f); continue; }
    if (rows === "remote") { buildRemoteAccess(f); continue; }
    for (const [path, label, kind, opts, help] of rows) f.appendChild(row(path, label, kind, opts, getPath(s, path), help));
  }
  f.insertAdjacentHTML("beforeend", `<h3>Home</h3><div class="btnrow"><button id="setHomeHere">Set home = map center</button><button id="setHomeMe">Set home = my GPS</button></div>`);
  $("setHomeHere").onclick = () => { const c = map.getCenter(); setHome(c.lat, c.lng); };
  $("setHomeMe").onclick = () => S.me ? setHome(S.me.lat, S.me.lon) : toast("Tap ◎ first to get your location.", 3000);
  const base = location.origin, t = TOKEN ? `&token=${TOKEN}` : "";
  const q = `site=${S.site}&product=${S.product}`;
  $("links").innerHTML = `<h3>Links for your devices</h3>
    <a href="/pi?${q}${t}" target="_blank">${base}/pi?${q}</a>
    <a href="/image/latest.png?${q}${t}" target="_blank">${base}/image/latest.png?${q}</a>
    <a href="/image/loop.webp?${q}${t}" target="_blank">${base}/image/loop.webp?${q}</a>
    <a href="/api/point?lat=${map.getCenter().lat.toFixed(3)}&lon=${map.getCenter().lng.toFixed(3)}${t}" target="_blank">${base}/api/point?lat=…&lon=…</a>
    <a href="/docs" target="_blank">${base}/docs  (full API)</a>
    <a href="/api/status?${t.slice(1)}" target="_blank">${base}/api/status</a>`;
}

function buildStyleRows(f) {
  const tabs = document.createElement("div");
  tabs.className = "prodtabs";
  tabs.innerHTML = S.cfg.products.map((p) => `<button data-p="${p}" class="${p === S.settingsProduct ? "on" : ""}">${p}</button>`).join("");
  tabs.querySelectorAll("button").forEach((b) => (b.onclick = () => { S.settingsProduct = b.dataset.p; buildSettings(); }));
  f.appendChild(tabs);
  const p = S.settingsProduct, st = S.cfg.settings.render.styles[p];
  const span = Math.abs(st.vmax - st.vmin), step = span > 20 ? 0.5 : span > 5 ? 0.1 : 0.01;
  const lo = p === "CC" ? 0 : p === "PHI" ? 0 : p === "VEL" ? -80 : p === "ZDR" ? -6 : p === "SW" ? 0 : -30;
  const hi = p === "CC" ? 1.1 : p === "PHI" ? 360 : p === "VEL" ? 80 : p === "ZDR" ? 10 : p === "SW" ? 30 : 85;
  const pals = Object.keys(S.cfg.palettes).filter((k) => S.cfg.palettes[k].units === S.cfg.palettes[st.palette].units);
  const pre = `render.styles.${p}.`;
  f.appendChild(row(pre + "palette", "Palette", "select", pals, st.palette));
  f.appendChild(row(pre + "threshold", "Hide values below", "range", [lo, hi, step], st.threshold ?? lo, null, true));
  f.appendChild(row(pre + "vmin", "Color scale from", "range", [lo, hi, step], st.vmin));
  f.appendChild(row(pre + "vmax", "Color scale to", "range", [lo, hi, step], st.vmax));
  f.appendChild(row(pre + "opacity", "Opacity", "range", [0.1, 1, 0.01], st.opacity));
}

// ---------- ngrok setup guide ----------
const NGROK_INSTALL = {
  mac: { name: "Mac", cmd: "brew install ngrok", where: "Run in Terminal. Needs Homebrew (brew.sh)." },
  windows: { name: "Windows", cmd: "winget install ngrok -s msstore", where: "Run in PowerShell or Command Prompt." },
  linux: {
    name: "Linux / Pi",
    cmd: 'curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc | sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null && echo "deb https://ngrok-agent.s3.amazonaws.com bookworm main" | sudo tee /etc/apt/sources.list.d/ngrok.list && sudo apt update && sudo apt install ngrok',
    where: "Debian, Ubuntu and Raspberry Pi OS. Other distros: snap install ngrok",
  },
};

async function copyText(text, btn) {
  try { await navigator.clipboard.writeText(text); }
  catch { // plain-http LAN pages have no clipboard API; fall back to a hidden textarea
    const ta = Object.assign(document.createElement("textarea"), { value: text });
    ta.style.cssText = "position:fixed;opacity:0";
    document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove();
  }
  btn.textContent = "Copied ✓"; setTimeout(() => (btn.textContent = "Copy"), 1500);
}
function cmdBlock(text) {
  const d = document.createElement("div");
  d.className = "cmd";
  d.innerHTML = `<code></code><button>Copy</button>`;
  d.querySelector("code").textContent = text;
  d.querySelector("button").onclick = (e) => copyText(text, e.currentTarget);
  return d;
}
function newToken() {
  const abc = "abcdefghjkmnpqrstuvwxyz23456789", r = crypto.getRandomValues(new Uint8Array(20));
  return Array.from(r, (b) => abc[b % abc.length]).join("");
}

async function buildRemoteAccess(f) {
  const host = S.cfg.host || { os: "mac", port: 8600, ngrok: {} };
  const box = document.createElement("div");
  box.className = "steps";
  f.appendChild(box);
  let st;
  try { st = await api("/api/remote"); } catch (e) { box.textContent = "Couldn't read remote access status."; return; }
  const sv = S.cfg.settings.server;
  const on = st.state === "on", starting = st.state === "starting";
  const haveToken = st.state !== "needs_authtoken";

  box.innerHTML = `
    <div class="remote-card ${on ? "on" : ""}">
      <div class="remote-state" id="rmState"></div>
      <div id="rmLink"></div>
      <div class="btnrow"><button id="rmToggle"></button></div>
    </div>
    <ol>
      <li><b>Free ngrok account.</b> <a href="https://dashboard.ngrok.com/signup" target="_blank">Sign up</a>, then copy your
        <a href="https://dashboard.ngrok.com/get-started/your-authtoken" target="_blank">authtoken</a>.
        ${st.authtoken_source === "ngrok config" || st.authtoken_source === "environment" ? '<span class="okmark">✓ found your existing ngrok setup</span>' : ""}</li>
      <li><b>Paste the authtoken here</b> (saved in this app only):
        <div class="tokrow"><input id="rmAuth" type="password" placeholder="${st.authtoken_source && st.authtoken_source !== "settings" ? "not needed, using your ngrok setup" : "paste authtoken"}" spellcheck="false" autocomplete="off"></div></li>
      <li><b>Radar password.</b> Phones away from home need it once. One is made for you if you leave it blank.
        <div class="tokrow"><input id="ngTok" type="text" placeholder="made automatically" spellcheck="false"><button id="ngGen">Generate</button></div></li>
      <li><b>Optional: same address every time.</b> Your free fixed domain from
        <a href="https://dashboard.ngrok.com/domains" target="_blank">dashboard.ngrok.com/domains</a>:
        <div class="tokrow"><input id="rmDomain" type="text" placeholder="e.g. your-name.ngrok-free.dev" spellcheck="false"></div></li>
    </ol>
    <p class="hint">After that it's one click here or in the menu-bar menu (<i>Start remote access</i>). It stays on, even after restarts, until you stop it.</p>
    <details class="manual"><summary>Prefer running ngrok yourself in a terminal?</summary><div id="manual"></div></details>`;

  const stateEl = box.querySelector("#rmState"), btn = box.querySelector("#rmToggle"), linkEl = box.querySelector("#rmLink");
  stateEl.innerHTML = on ? '<span class="okmark">● Remote access is on</span>'
    : starting ? "Starting…"
    : st.state === "error" ? `<span class="warnmark">Couldn't start:</span> ${st.error}`
    : !haveToken ? "Remote access is off. Paste your ngrok authtoken below first."
    : "Remote access is off.";
  if (on) {
    linkEl.innerHTML = `<p class="hint">Address: <code></code></p><p class="hint">Phone link with your password (open it once, then bookmark the plain address):</p>`;
    linkEl.querySelector("code").textContent = st.url;
    if (st.link) linkEl.appendChild(cmdBlock(st.link));
  }
  btn.textContent = on ? "Stop remote access" : starting ? "Starting…" : "Start remote access";
  btn.disabled = starting || (!on && !haveToken);
  btn.onclick = async () => {
    btn.disabled = true; btn.textContent = on ? "Stopping…" : "Starting… (a few seconds)";
    try { await api(`/api/remote/${on ? "stop" : "start"}`, { method: "POST" }); }
    catch (e) { toast("Remote access: " + e.message, 5000); }
    S.cfg = await api("/api/config"); buildSettings();
  };

  const auth = box.querySelector("#rmAuth");
  if (sv.ngrok_authtoken) auth.placeholder = `saved (${sv.ngrok_authtoken.slice(-4)})`;
  auth.onchange = () => { if (auth.value.trim()) { send("server.ngrok_authtoken", auth.value.trim(), true); setTimeout(buildSettings, 600); } };
  const tok = box.querySelector("#ngTok");
  tok.value = sv.access_token || "";
  tok.onchange = () => send("server.access_token", tok.value.trim() || null, true);
  box.querySelector("#ngGen").onclick = () => { tok.value = newToken(); tok.onchange(); };
  const dom = box.querySelector("#rmDomain");
  dom.value = sv.ngrok_domain || "";
  dom.onchange = () => send("server.ngrok_domain", dom.value.trim() || null, true);

  // manual route: the commands from before, per OS
  const m = box.querySelector("#manual");
  S.ngrokOs = S.ngrokOs || host.os;
  m.innerHTML = `<div class="ostabs prodtabs">${Object.entries(NGROK_INSTALL).map(([k, v]) => `<button data-os="${k}" class="${k === S.ngrokOs ? "on" : ""}">${v.name}</button>`).join("")}</div>
    <p class="hint">${NGROK_INSTALL[S.ngrokOs].where}</p>`;
  m.querySelectorAll("button").forEach((b) => (b.onclick = () => { S.ngrokOs = b.dataset.os; S.manualOpen = true; buildSettings(); }));
  m.appendChild(cmdBlock(NGROK_INSTALL[S.ngrokOs].cmd));
  m.appendChild(cmdBlock("ngrok config add-authtoken PASTE_YOUR_AUTHTOKEN"));
  m.appendChild(cmdBlock(`ngrok http ${host.port}`));
  if (S.manualOpen) box.querySelector("details").open = true;
  if (starting) setTimeout(() => !$("drawer").classList.contains("hidden") && buildSettings(), 2000);
  if (S.scrollToRemote) { S.scrollToRemote = false; box.previousElementSibling?.previousElementSibling?.scrollIntoView(); }
}

function row(path, label, kind, opts, value, help, nullable) {
  const d = document.createElement("div");
  d.className = "row";
  const id = "f_" + path.replace(/\./g, "_");
  let input;
  if (kind === "range") {
    d.innerHTML = `<label for="${id}">${label}</label><span class="val"></span><input id="${id}" type="range" min="${opts[0]}" max="${opts[1]}" step="${opts[2]}" value="${value}">`;
    input = d.querySelector("input");
    const show = () => (d.querySelector(".val").textContent = (+input.value).toString());
    show();
    input.oninput = () => { show(); send(path, +input.value); };
  } else if (kind === "select") {
    d.innerHTML = `<label for="${id}">${label}</label><select id="${id}">${opts.map((o) => `<option ${o == value ? "selected" : ""}>${o}</option>`).join("")}</select>`;
    input = d.querySelector("select");
    input.onchange = () => send(path, typeof opts[0] === "number" ? +input.value : input.value, true);
  } else if (kind === "check") {
    d.innerHTML = `<label for="${id}">${label}</label><input id="${id}" type="checkbox" ${value ? "checked" : ""}>`;
    input = d.querySelector("input");
    input.onchange = () => send(path, input.checked, true);
  } else {
    const v = kind === "list" ? (value || []).join(", ") : value ?? "";
    d.innerHTML = `<label for="${id}">${label}</label><input id="${id}" type="${kind === "number" ? "number" : "text"}" value="${v}">`;
    input = d.querySelector("input");
    input.onchange = () => {
      let val = input.value.trim();
      if (kind === "number") val = +val;
      if (kind === "list") val = val ? val.split(",").map((x) => x.trim().toUpperCase()).filter(Boolean) : [];
      if (kind === "text" && !val) val = null;
      send(path, val, true);
    };
  }
  if (help) d.insertAdjacentHTML("beforeend", `<p class="hint" style="grid-column:1/-1;margin:2px 0 0">${help}</p>`);
  return d;
}

let pending = {}, sendTimer = null;
function send(path, value, now) {
  const deep = (a, b) => { for (const k in b) { if (b[k] && typeof b[k] === "object" && !Array.isArray(b[k])) { a[k] = a[k] || {}; deep(a[k], b[k]); } else a[k] = b[k]; } return a; };
  deep(pending, patchFor(path, value));
  clearTimeout(sendTimer);
  sendTimer = setTimeout(flush, now ? 0 : 250);
}
async function flush() {
  const patch = pending; pending = {};
  try {
    const r = await api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) });
    const reprocess = r.processing_version !== S.cfg.processing_version;
    const tileSizeChanged = r.settings.render.tile_size !== S.cfg.settings.render.tile_size;
    S.cfg.settings = r.settings; S.cfg.processing_version = r.processing_version;
    S.version = r.version;
    drawLegend();
    if (tileSizeChanged) clearLayers(); else retireLayers();
    if (reprocess) { toast("Re-processing radar volumes…", 4000); setTimeout(() => loadSite(false), 2500); setTimeout(() => loadSite(false), 7000); }
    loadSite(false);
  } catch (e) { toast("Setting rejected: " + e.message, 5000); }
}
async function setHome(lat, lon) {
  const near = await api(`/api/sites/near?lat=${lat}&lon=${lon}&n=1&wsr_only=true`);
  S.cfg.home = await api("/api/home", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ lat, lon }) });
  if (homeMarker) homeMarker.remove();
  homeMarker = L.marker([lat, lon], { icon: L.divIcon({ className: "", html: '<div class="home-marker"></div>', iconSize: [18, 18] }), interactive: false, pane: "labels" }).addTo(map);
  toast(`Home set · nearest radar ${near[0].id}`, 3000);
}
