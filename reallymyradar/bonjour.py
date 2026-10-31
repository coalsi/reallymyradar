"""Announce the radar server on the local network (Bonjour / mDNS: `_reallymyradar._tcp`).

Apps like the Apple TV app find the server this way with no address to type. The TXT record
carries ready-to-use URLs: `url` (by IP) and `host_url` (by .local name, which survives DHCP changes).
"""
from __future__ import annotations

import logging
import socket

log = logging.getLogger("radar")
SERVICE = "_reallymyradar._tcp.local."


class Announcer:
    def __init__(self, port: int, version: str = "1"):
        self.port, self.version = port, version
        self._zc = None
        self._info = None

    async def start(self) -> None:
        try:
            from zeroconf import ServiceInfo
            from zeroconf.asyncio import AsyncZeroconf

            from .netinfo import lan_ips
            ips = lan_ips()
            host = socket.gethostname().split(".")[0]
            local = f"{host}.local"
            self._info = ServiceInfo(
                SERVICE, f"ReallymyRadar on {host}.{SERVICE}",
                addresses=[socket.inet_aton(ip) for ip in ips], port=self.port, server=f"{local}.",
                properties={"url": f"http://{ips[0]}:{self.port}", "host_url": f"http://{local}:{self.port}",
                            "version": self.version})
            self._zc = AsyncZeroconf()
            await self._zc.async_register_service(self._info, allow_name_change=True)
            log.info("announced on the network as %s", self._info.name)
        except Exception as e:  # discovery is a convenience; never block the server on it
            log.warning("bonjour announcement failed: %s", e)
            self._zc = None

    async def stop(self) -> None:
        if self._zc and self._info:
            try:
                await self._zc.async_unregister_service(self._info)
                await self._zc.async_close()
            except Exception:
                pass
