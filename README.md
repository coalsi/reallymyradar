# ReallymyRadar

Host your own high-resolution weather radar. ReallymyRadar pulls raw **NEXRAD Level II** data straight from
NOAA's public AWS buckets, only for the area you're looking at. It decodes every moment at full
super-resolution (0.5° × 250 m gates), stitches the nearby radars into one seamless mosaic, and serves it to
your whole network as:

- a **browser viewer** with live tuning
- **map tiles** and **finished images** for any app, dashboard, Raspberry Pi or e-ink screen
- a **JSON API** ("is it raining at this spot, and how hard?")

It runs as a menu-bar / system-tray app on macOS, Windows and Linux, or headless on a server or in Docker.

## Install

**Download the app** from the [Releases](../../releases) page (macOS Apple Silicon, Windows, Linux) and open it.
A radar icon appears in your menu bar / system tray, and the viewer opens in your browser.

> These builds are unsigned for now. macOS: right-click the app → Open the first time.
> Windows: SmartScreen → More info → Run anyway.

**From source** (any OS with Python 3.12+ and [uv](https://docs.astral.sh/uv/)):

```bash
git clone https://github.com/coalsi/reallymyradar && cd reallymyradar
uv run reallymyradar            # tray app
uv run radar                    # headless server
```

**Docker** (Linux servers, NAS boxes, Raspberry Pi 4/5):

```bash
docker build -t reallymyradar .
docker run -d --name radar --restart unless-stopped -p 8600:8600 -v radar-data:/data reallymyradar
```

Then open `http://<that-machine>:8600`.

## Knowing it's running

The tray icon is the status light. Its blip is **green** when radar data is flowing, **amber** while radars
download, and **red** on errors. Hover it for details. Its menu has: Open Radar · Settings · Kiosk view ·
API docs · Copy network link · **Keep computer awake** · **Start at login** · Open data folder · Quit.

While it runs, the app keeps the computer from idle-sleeping so kiosks and other devices never lose the radar
(the display can still sleep). It releases the moment the app quits. Toggle it in the tray menu or ⚙ → This computer.

Headless installs report the same thing at `GET /api/status`. The Docker image has a built-in health check.

## For other apps: the API

Interactive docs live on every install at **`/docs`**. The main endpoints:

| | |
|---|---|
| `GET /api/point?lat=&lon=` | Values at one spot from the best radar for it: `precipitation` (none / light / moderate / heavy / intense / extreme) plus CREF, REF, VEL, SW, ZDR, CC, PHI with units, source radar and scan time. Add `&wait=60` on a first call so it can download. |
| `GET /tiles/MOSAIC/{product}/0/L/{z}/{x}/{y}.png` | Live mosaic map tiles (XYZ, 512 px) for Leaflet, MapLibre, Home Assistant, etc. Use a radar id instead of `MOSAIC` for one radar, and a frame id instead of `L` for history. |
| `GET /api/mosaic?south=&west=&north=&east=&product=` | Animation frames available for an area, plus which radars are feeding it. |
| `GET /image/latest.png` (`.jpg`, `.webp`) | A finished picture: basemap, radar, labels, legend and timestamp. Query: `lat`, `lon`, `zoom`, `w`, `h`, `scale`, `product`, `site`, `basemap`. |
| `GET /image/loop.webp` | Animated loop of the same (`frames`, `ms`). |
| `GET /pi` | Full-screen kiosk loop for a Raspberry Pi or spare screen. All rendering happens on the host, so the Pi only flips images. |
| `GET/PUT /api/settings` | Read or patch any setting (partial JSON). |
| `GET /api/sites/near?lat=&lon=` | Nearest radars. |

CORS is open for `GET`, so any web page on your network can fetch the API and draw the tiles, e.g. as an OpenLayers
`XYZ` source with `tileSize: 512` and `crossOrigin: 'anonymous'`, or a Leaflet `tileLayer` with `tileSize: 512, zoomOffset: -1`.

Products: `CREF` composite (default), `REF` reflectivity, `VEL` velocity, `SW` spectrum width, `ZDR`, `CC`, `PHI`.
VEL, SW and PHI are relative to each radar, so the mosaic serves them from the radar nearest the view.

## How it works

| | |
|---|---|
| **Data** | Live chunks from `unidata-nexrad-level2-chunks`: the in-progress scan is re-decoded as each tilt finishes, so the lowest tilt is ~1 min old. Complete volumes from `unidata-nexrad-level2` backfill the animation loop. |
| **Mosaic** | Beams widen with range, so every spot comes from the closest radar covering it, with neighbors blended over a 20 km seam. A farther radar fills spots the closest can't see (terrain-blocked beams) when it sees ≥ 20 dBZ. Frames sit on a 5-minute grid, plus a live frame. |
| **Rendering** | Every pixel is projected back to each radar (WGS84 azimuthal-equidistant + 4/3-earth beam model) and sampled from the native gates, with smooth bilinear blending. There is no intermediate grid, so zooming in keeps getting sharper. |
| **Cleanup** | Bugs, birds and ground clutter are removed when CC < 0.85 *and* the echo is weaker than 30 dBZ, so strong low-CC signatures like tornado debris stay. Isolated specks are despeckled. Velocity is dealiased. |
| **On demand** | Radars download only while something is looking at their area (or they're pinned), and go idle after `active_ttl_minutes`. |
| **Sites** | All 162 WSR-88D sites, plus 45 airport TDWRs (optional in the mosaic). |

## Settings

Everything lives in one `settings.json` in your data folder (tray → Open data folder):

- macOS: `~/Library/Application Support/ReallymyRadar`
- Windows: `%LOCALAPPDATA%\ReallymyRadar`
- Linux: `~/.local/share/reallymyradar`
- Docker: `/data`, or anywhere via `RMR_HOME`

It can be edited three ways, and all three take effect live: the ⚙ drawer in the viewer, `PUT /api/settings`,
or the file itself. Palettes live in `reallymyradar/colormaps.py`.

## Privacy

Nothing leaves your machine except requests for public data: radar files from NOAA's AWS buckets and map tiles from Esri.
The one exception is a fresh install with no home location set: it makes a single request to `ipinfo.io` to guess a
rough home location from your IP address. To skip that, set `data.home_lat` / `data.home_lon` in `settings.json`
before the first launch, or use ⚙ → Home afterwards. Your home location stays in your local settings file.

## Away from home (ngrok)

The viewer's ⚙ → **Away from home** walks through this with copy buttons, and ticks off steps already done on the
radar computer. Run everything on the computer the radar runs on.

1. Make a free account at [ngrok.com](https://dashboard.ngrok.com/signup) and copy your
   [authtoken](https://dashboard.ngrok.com/get-started/your-authtoken).
2. Install ngrok:

   macOS (Terminal, needs [Homebrew](https://brew.sh)):
   ```bash
   brew install ngrok
   ```
   Windows (PowerShell):
   ```powershell
   winget install ngrok -s msstore
   ```
   Debian / Ubuntu / Raspberry Pi OS (other distros: `snap install ngrok`):
   ```bash
   curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc | sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null && echo "deb https://ngrok-agent.s3.amazonaws.com bookworm main" | sudo tee /etc/apt/sources.list.d/ngrok.list && sudo apt update && sudo apt install ngrok
   ```
3. Connect it to your account:
   ```bash
   ngrok config add-authtoken PASTE_YOUR_AUTHTOKEN
   ```
4. Pick a radar password: ⚙ → Away from home → **Generate** (stored as `server.access_token`). Anything arriving through
   the tunnel needs it, while devices on your home network don't.
5. Start the tunnel, and leave it running:
   ```bash
   ngrok http 8600
   ```
   ngrok prints a *Forwarding* address like `https://abcd-1234.ngrok-free.app`. For the same address every time, use your
   free domain from [dashboard.ngrok.com/domains](https://dashboard.ngrok.com/domains):
   `ngrok http 8600 --url https://YOUR-DOMAIN`. (`scripts/tunnel.sh` does this step, and refuses to start without a password.)
6. On your phone, open `https://YOUR-ADDRESS/?token=YOUR-PASSWORD` once. ngrok's free plan shows a one-time "Visit Site"
   notice. After that, a cookie remembers the password for a year and the plain address works. Over https, the
   viewer's ◎ button can use your phone's GPS.

## Headless at boot (Linux systemd)

```ini
# ~/.config/systemd/user/reallymyradar.service   →  systemctl --user enable --now reallymyradar
[Service]
ExecStart=/path/to/uv run --project /path/to/reallymyradar radar
Restart=on-failure
[Install]
WantedBy=default.target
```

## Building the apps

```bash
uv run python -m reallymyradar.icon                          # regenerate icons
uv run pyinstaller packaging/reallymyradar.spec --noconfirm  # build for this OS → dist/
```

GitHub Actions builds and smoke-tests all three platforms on every push, and attaches them to a release on `v*` tags.

## Layout

```
reallymyradar/
  app.py        tray app + headless entry, single-instance check
  autostart.py  start at login (LaunchAgent / Run key / XDG autostart)
  server.py     FastAPI: API, tiles, images, pages
  manager.py    on-demand radar polling, frame store, reprocessing
  fetch.py      S3 listing/downloads (archive + live chunks)
  decode.py     Level II -> cleaned uniform polar sweeps per product (worker processes)
  mosaic.py     radar selection, time slots, closest-radar blending
  render.py     gate-exact polar -> Web Mercator renderer
  compose.py    finished images (basemap, labels, legend, header)
  colormaps.py  palettes          config.py  settings model          icon.py  app icon
  static/       viewer, kiosk page
packaging/      PyInstaller spec      scripts/  ngrok tunnel
```

Basemap © Esri. Radar data: NOAA NEXRAD Level II via NOAA Open Data Dissemination / Unidata.
