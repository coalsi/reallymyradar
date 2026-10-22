"""ReallymyRadar desktop app: the radar server plus an icon in the menu bar / system tray.

    reallymyradar              server + tray icon (opens the viewer on first launch)
    reallymyradar --headless   server only (Raspberry Pi, Linux server, Docker, launchd/systemd)

The tray icon is how you know it's running: its blip is green when radar data is flowing,
amber while radars download, red on errors. Its menu opens the viewer, settings, the Pi view
and the API docs, copies the network link, toggles start-at-login, and quits.
"""
from __future__ import annotations

import argparse
import json
import logging
import multiprocessing
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

os.environ.setdefault("PYART_QUIET", "1")
log = logging.getLogger("radar")


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent; this just picks the LAN interface
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def already_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1.5) as r:
            return "workers" in json.loads(r.read())
    except Exception:
        return False


def copy_to_clipboard(text: str) -> bool:
    cmds = {"darwin": [["pbcopy"]], "win32": [["clip"]]}.get(sys.platform, [["wl-copy"], ["xclip", "-selection", "clipboard"]])
    for cmd in cmds:
        try:
            subprocess.run(cmd, input=text.encode("utf-16le" if sys.platform == "win32" else "utf-8"), check=True)
            return True
        except Exception:
            continue
    return False


def on_ui_thread(fn) -> None:
    """macOS kills an app that touches menu-bar UI off the main thread, so hop there first.
    (Windows and Linux tray backends are fine being updated from any thread.)"""
    if sys.platform == "darwin":
        from PyObjCTools import AppHelper
        AppHelper.callAfter(fn)
    else:
        fn()


class ServerThread(threading.Thread):
    def __init__(self, port: int):
        super().__init__(daemon=True, name="radar-server")
        import uvicorn
        from .server import app
        self.server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info"))

    def run(self):
        self.server.run()

    def stop(self):
        self.server.should_exit = True
        self.join(timeout=10)


def run_tray(port: int, open_browser: bool):
    import pystray
    from . import autostart
    from .config import APP_HOME
    from .icon import draw
    from .server import manager

    local = f"http://localhost:{port}"
    lan = f"http://{lan_ip()}:{port}"
    srv = ServerThread(port)
    srv.start()

    def state() -> tuple[str, str]:
        try:
            sites = manager.status()["sites"]
        except Exception:
            return "loading", "Starting…"
        active = [s for s in sites if s["active"]]
        if any(s["error"] for s in active):
            return "error", f"Problem: {next(s['error'] for s in active if s['error'])[:60]}"
        if not active or any(s["volumes"] == 0 for s in active):
            return "loading", f"Downloading radar data… ({len(active)} radars)"
        return "ok", f"Running · {len(active)} radar{'s' * (len(active) != 1)} live"

    def open_url(path=""):
        return lambda icon, item: webbrowser.open(local + path)

    def copy_link(icon, item):
        if copy_to_clipboard(lan):
            icon.notify(f"Copied {lan}", "ReallymyRadar") if icon.HAS_NOTIFICATION else None
        else:
            webbrowser.open(lan)

    def toggle_login(icon, item):
        autostart.disable() if autostart.is_enabled() else autostart.enable()

    def open_folder(icon, item):
        opener = {"darwin": ["open"], "win32": ["explorer"]}.get(sys.platform, ["xdg-open"])
        subprocess.Popen([*opener, str(APP_HOME)])

    def quit_app(icon, item):  # menu callbacks already run on the UI thread
        icon.visible = False
        threading.Thread(target=lambda: (srv.stop(), on_ui_thread(icon.stop)), daemon=True).start()

    menu = pystray.Menu(
        pystray.MenuItem(lambda item: state()[1], None, enabled=False),
        pystray.MenuItem(f"On your network: {lan}", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open Radar", open_url(), default=True),
        pystray.MenuItem("Settings", open_url("/?settings=1")),
        pystray.MenuItem("Kiosk view (Raspberry Pi)", open_url("/pi")),
        pystray.MenuItem("API docs", open_url("/docs")),
        pystray.MenuItem("Copy network link", copy_link),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start at login", toggle_login, checked=lambda item: autostart.is_enabled()),
        pystray.MenuItem("Open data folder", open_folder),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit ReallymyRadar", quit_app),
    )
    icon = pystray.Icon("ReallymyRadar", draw(64, "loading", tray=True), "ReallymyRadar", menu)

    def watch(icon):
        on_ui_thread(lambda: setattr(icon, "visible", True))
        if open_browser:
            for _ in range(60):  # wait for the server to answer before opening the page
                if already_running(port):
                    webbrowser.open(local)
                    break
                time.sleep(0.5)
        last = None
        while srv.is_alive():
            st, text = state()

            def refresh(st=st, text=text, changed=st != last):
                if changed:
                    icon.icon = draw(64, st, tray=True)
                icon.title = f"ReallymyRadar — {text}"
                icon.update_menu()

            on_ui_thread(refresh)
            last = st
            time.sleep(5)

    icon.run(setup=watch)  # must own the main thread on macOS


def main():
    multiprocessing.freeze_support()  # decode workers in packaged builds
    p = argparse.ArgumentParser(prog="reallymyradar", description="Self-hosted high-resolution NEXRAD radar server")
    p.add_argument("--headless", action="store_true", help="server only, no tray icon")
    p.add_argument("--port", type=int, help="override the port in settings")
    p.add_argument("--no-browser", action="store_true", help="don't open the viewer on launch")
    args = p.parse_args()

    from .config import APP_HOME, SettingsStore
    store = SettingsStore()
    port = args.port or store.settings.server.port  # --port applies to this run only

    if already_running(port):
        print(f"ReallymyRadar is already running at http://localhost:{port}")
        if not args.headless:
            webbrowser.open(f"http://localhost:{port}")
        return

    print(f"\n  ReallymyRadar  →  http://localhost:{port}   LAN: http://{lan_ip()}:{port}   "
          f"Pi: http://{lan_ip()}:{port}/pi   API: http://localhost:{port}/docs\n  data: {APP_HOME}\n")
    if args.headless:
        import uvicorn
        from .server import app
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
        return
    first_launch = not (APP_HOME / ".launched").exists()
    (APP_HOME / ".launched").touch()
    run_tray(port, open_browser=first_launch and not args.no_browser)


def headless():
    """`radar` command: same as `reallymyradar --headless`."""
    sys.argv.insert(1, "--headless")
    main()


if __name__ == "__main__":
    main()
