"""Remote access: an ngrok tunnel run inside the app (ngrok's own SDK, so nothing to install).

Needs the user's ngrok authtoken, looked up in order:
  settings server.ngrok_authtoken  →  NGROK_AUTHTOKEN env  →  an existing ngrok agent config file.
Anything arriving through the tunnel must carry the radar password (server.access_token), which is
generated automatically on first start if none is set.
"""
from __future__ import annotations

import os
import re
import secrets
import sys
import threading
from pathlib import Path

ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def new_password() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(20))


def _agent_config_token() -> str | None:
    """Reuse the authtoken from an ngrok agent the user already set up (`ngrok config add-authtoken`)."""
    home = Path.home()
    paths = [home / "Library/Application Support/ngrok/ngrok.yml",
             Path(os.environ.get("LOCALAPPDATA", home)) / "ngrok/ngrok.yml",
             Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "ngrok/ngrok.yml",
             home / ".ngrok2/ngrok.yml"]
    for p in paths:
        try:
            m = re.search(r"^\s*authtoken:\s*['\"]?([A-Za-z0-9_\-]+)", p.read_text(), re.M)
        except OSError:
            continue
        if m:
            return m.group(1)
    return None


def find_authtoken(settings) -> tuple[str | None, str]:
    """-> (token, where it came from)"""
    if settings.server.ngrok_authtoken:
        return settings.server.ngrok_authtoken, "settings"
    if os.environ.get("NGROK_AUTHTOKEN"):
        return os.environ["NGROK_AUTHTOKEN"], "environment"
    tok = _agent_config_token()
    return (tok, "ngrok config") if tok else (None, "")


class Tunnel:
    def __init__(self, store):
        self.store = store
        self.url: str | None = None
        self.state = "off"        # off | starting | on | error
        self.error: str | None = None
        self._lock = threading.Lock()

    def status(self) -> dict:
        s = self.store.settings
        token, source = find_authtoken(s)
        pw = s.server.access_token
        link = f"{self.url}/?token={pw}" if self.url and pw else None
        state = self.state if token or self.state == "on" else "needs_authtoken"
        return {"state": state, "url": self.url, "link": link, "error": self.error, "available": _sdk_available(),
                "authtoken_source": source, "enabled": s.server.remote_enabled, "domain": s.server.ngrok_domain}

    def start(self) -> dict:
        """Open the tunnel (blocking, a few seconds). Safe to call when already running."""
        with self._lock:
            if self.state == "on":
                return self.status()
            s = self.store.settings
            token, _ = find_authtoken(s)
            if not token:
                self.state, self.error = "off", None
                return self.status()
            if not s.server.access_token:  # never expose the radar without a password
                self.store.update({"server": {"access_token": new_password()}})
            self.state, self.error = "starting", None
            try:
                import ngrok
                opts = {"authtoken": token}
                if s.server.ngrok_domain:
                    opts["domain"] = s.server.ngrok_domain.removeprefix("https://").removeprefix("http://").strip("/")
                listener = ngrok.forward(f"localhost:{s.server.port}", **opts)
                self.url = listener.url()
                self.state = "on"
                self.store.update({"server": {"remote_enabled": True}})
            except Exception as e:
                self.state, self.url = "error", None
                self.error = _friendly(str(e))
            return self.status()

    def stop(self, remember: bool = True) -> dict:
        with self._lock:
            if self.url:
                try:
                    import ngrok
                    ngrok.disconnect(self.url)
                except Exception:
                    pass
            self.url, self.state, self.error = None, "off", None
            if remember:
                self.store.update({"server": {"remote_enabled": False}})
            return self.status()


def _sdk_available() -> bool:
    """True when ngrok's SDK can load (packaged builds must bundle its native module)."""
    try:
        import ngrok  # noqa: F401
        return True
    except Exception:
        return False


def _friendly(msg: str) -> str:
    m = msg.lower()
    if "authtoken" in m or "err_ngrok_105" in m or "err_ngrok_107" in m:
        return "ngrok rejected the authtoken. Copy it again from dashboard.ngrok.com."
    if "err_ngrok_108" in m or "limited to 1 simultaneous" in m:
        return "Your ngrok account is already running a tunnel elsewhere (free plan allows one). Stop the other one."
    if "domain" in m:
        return "ngrok couldn't use that domain. Check it matches the one on dashboard.ngrok.com/domains."
    return msg.splitlines()[0][:200]
