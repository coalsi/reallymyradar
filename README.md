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

### 1. Download

Get the file for your computer from the **[latest release](../../releases/latest)**:

| Computer | Download |
|---|---|
| Mac with Apple chip (M1 or newer) | `ReallymyRadar-mac-apple-silicon.zip` |
| Mac with Intel chip | `ReallymyRadar-mac-intel.zip` |
| Windows 10 / 11 | `ReallymyRadar-windows.zip` |
| Linux (64-bit) | `ReallymyRadar-linux.tar.gz` |

Not sure which Mac you have? Apple menu → **About This Mac**: "Chip: Apple M…" means Apple chip.

### 2. Open it

The app isn't signed with a paid Apple or Microsoft certificate yet, so the first launch needs one extra click.

**Mac**
1. Double-click the zip, then drag **ReallymyRadar** into your **Applications** folder.
2. Open it. macOS will say it can't verify the app: click **Done**.
3. Open **System Settings → Privacy & Security**, scroll down, and click **Open Anyway** next to ReallymyRadar. Confirm.
4. A radar icon appears in your **menu bar** (top right). There's no Dock icon, by design.

**Windows**
1. Right-click the zip → **Extract All**, then open the folder and run **ReallymyRadar.exe**.
2. If "Windows protected your PC" appears: click **More info → Run anyway**.
3. When Windows Firewall asks, tick **Private networks** and click **Allow**, so your phone and other devices can see the radar.
4. A radar icon appears in the **system tray** by the clock. Click the **^** arrow if it's hidden.

**Linux**
```bash
tar xzf ReallymyRadar-linux.tar.gz && ./ReallymyRadar/ReallymyRadar
```
The icon shows in the system tray on most desktops. On GNOME, the tray needs the "AppIndicator" extension; without it,
the app still runs and you use it from the browser.

### 3. Set it up

Your browser opens a short welcome: type your address, city or ZIP (or use your location), and you're done.
Then, in the radar icon's menu, tick **Start at login** so it's always there.

### Other ways to run it

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
The exceptions:
- A fresh install with no home set makes one request to `ipinfo.io` to guess a rough starting location from your IP
  address. To skip it, set `data.home_lat` / `data.home_lon` in `settings.json` before the first launch.
- Searching for a place sends what you typed to the US Census geocoder (street addresses) or OpenStreetMap's
  Nominatim (cities and ZIP codes).
- Remote access, when you turn it on, goes through your own ngrok account.

Your home location stays in your local settings file.

## Away from home (ngrok)

The app has ngrok built in, so there's nothing to install and no terminal.

1. Make a free account at [ngrok.com](https://dashboard.ngrok.com/signup) and copy your
   [authtoken](https://dashboard.ngrok.com/get-started/your-authtoken).
2. Paste it in ⚙ → **Away from home**. If ngrok is already set up on this computer, its authtoken is found automatically.
3. Click **Start remote access**, in the menu-bar menu or in that same settings section.

The menu then shows your public address and **Copy phone link**, which includes your radar password. Open that link
once on your phone: ngrok's free plan shows a one-time "Visit Site" notice, then a cookie remembers the password for a
year. Remote access stays on, even after restarts, until you choose **Stop remote access**.

- A radar password (`server.access_token`) is generated automatically if you haven't set one. Anything arriving
  through the tunnel without it gets a 401. With no password set, tunnel traffic is refused outright.
- Remote access can only be switched on or off from your home network, never through the tunnel.
- Free ngrok accounts get a permanent address. To use a specific domain, set it in ⚙ → Away from home.
- Your ngrok authtoken stays in your local settings file and is masked in every API response.
- Prefer running ngrok yourself? The settings section has the per-OS terminal commands, and `scripts/tunnel.sh` still works.

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
  remote.py     built-in ngrok tunnel for remote access         keepawake.py  stop idle sleep while running
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
