"""Announce the radar server on the local network (Bonjour / mDNS: `_reallymyradar._tcp`).

Apps like the Apple TV app find the server this way with no address to type. The TXT record
carries ready-to-use URLs: `url` (by IP) and `host_url` (by .local name, which survives DHCP changes).

On macOS the system's own responder does the announcing (via `dns-sd`): a VPN that owns the default
route stops python-zeroconf's multicast from reaching the home network, but not mDNSResponder's.
Elsewhere python-zeroconf is used.
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys

log = logging.getLogger("radar")
SERVICE = "_reallymyradar._tcp.local."


class Announcer:
    def __init__(self, port: int, version: str = "1"):
        self.port, self.version = port, version
        self._zc = None
        self._info = None
        self._proc: subprocess.Popen | None = None

    def _txt(self) -> tuple[str, dict[str, str]]:
        from .netinfo import lan_ips
        host = socket.gethostname().split(".")[0]
        return host, {"url": f"http://{lan_ips()[0]}:{self.port}", "host_url": f"http://{host}.local:{self.port}",
                      "version": self.version}

    async def start(self) -> None:
        if sys.platform == "darwin":
            self._start_macos()
            return
        try:
            from zeroconf import ServiceInfo
            from zeroconf.asyncio import AsyncZeroconf

            from .netinfo import lan_ips
            host, txt = self._txt()
            self._info = ServiceInfo(
                SERVICE, f"ReallymyRadar on {host}.{SERVICE}",
                addresses=[socket.inet_aton(ip) for ip in lan_ips()], port=self.port, server=f"{host}.local.",
                properties=txt)
            self._zc = AsyncZeroconf()
            await self._zc.async_register_service(self._info, allow_name_change=True)
            log.info("announced on the network as %s", self._info.name)
        except Exception as e:  # discovery is a convenience; never block the server on it
            log.warning("bonjour announcement failed: %s", e)
            self._zc = None

    def _start_macos(self) -> None:
        host, txt = self._txt()
        record = [f"{k}={v}" for k, v in txt.items()]
        # dns-sd runs until killed; the shell wrapper ends it when this process goes away, crash included.
        cmd = ["dns-sd", "-R", f"ReallymyRadar on {host}", "_reallymyradar._tcp", "local", str(self.port), *record]
        watcher = f'"$@" >/dev/null 2>&1 & child=$!; while kill -0 {os.getpid()} 2>/dev/null; do sleep 5; done; kill $child'
        try:
            self._proc = subprocess.Popen(["/bin/sh", "-c", watcher, "sh", *cmd], start_new_session=True)
            log.info("announced on the network as 'ReallymyRadar on %s' (%s)", host, txt["url"])
        except OSError as e:
            log.warning("bonjour announcement failed: %s", e)

    async def stop(self) -> None:
        if self._proc:
            try:
                os.killpg(self._proc.pid, 15)  # the wrapper and its dns-sd
            except OSError:
                pass
            self._proc = None
        if self._zc and self._info:
            try:
                await self._zc.async_unregister_service(self._info)
                await self._zc.async_close()
            except Exception:
                pass
