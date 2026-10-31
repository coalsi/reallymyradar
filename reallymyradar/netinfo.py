"""Which address other devices on the home network should use to reach this computer.

"Whatever interface reaches the internet" is wrong whenever a VPN owns the default route, so this
looks at the real adapters, skips tunnels / VM bridges / containers, and prefers home-network ranges.
"""
from __future__ import annotations

import ipaddress
import socket

_SKIP = ("utun", "tun", "tap", "wg", "ppp", "ipsec", "tailscale", "zt", "bridge", "vmnet", "vboxnet",
         "docker", "veth", "br-", "virbr", "lo", "awdl", "llw", "anpi", "gif", "stf")


def lan_ips() -> list[str]:
    """Candidate home-network IPv4 addresses, best first."""
    found: list[tuple[int, str]] = []
    try:
        import ifaddr
        for adapter in ifaddr.get_adapters():
            name = (adapter.nice_name or adapter.name or "").lower()
            if name.startswith(_SKIP) or "virtual" in name or "vpn" in name or "hyper-v" in name:
                continue
            for ip in adapter.ips:
                if not isinstance(ip.ip, str):
                    continue  # IPv6
                addr = ipaddress.ip_address(ip.ip)
                if not addr.is_private or addr.is_loopback or addr.is_link_local:
                    continue
                rank = 0 if ip.ip.startswith("192.168.") else 1 if ip.ip.startswith("10.") else 2
                found.append((rank, ip.ip))
    except Exception:
        pass
    ips = [ip for _, ip in sorted(found)]
    if not ips:  # fall back to the route-based guess
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("10.255.255.255", 1))  # no packet is sent
                ips = [s.getsockname()[0]]
        except OSError:
            ips = ["127.0.0.1"]
    return ips


def lan_ip() -> str:
    return lan_ips()[0]
