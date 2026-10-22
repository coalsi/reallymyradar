"""Anonymous access to the NOAA/Unidata NEXRAD Level II buckets on AWS.

archive: unidata-nexrad-level2/YYYY/MM/DD/SITE/SITEYYYYMMDD_HHMMSS_V06   (complete volumes, ~10 min lag)
chunks:  unidata-nexrad-level2-chunks/SITE/<vol 1-999>/YYYYMMDD-HHMMSS-<seq>-<S|I|E>  (live, ~seconds lag)
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx

ARCHIVE = "https://unidata-nexrad-level2.s3.amazonaws.com"
CHUNKS = "https://unidata-nexrad-level2-chunks.s3.amazonaws.com"
_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=30, limits=httpx.Limits(max_connections=32),
                                    headers={"User-Agent": "ReallymyRadar/0.1"})
    return _client


async def _list(bucket: str, prefix: str, delimiter: str | None = None, max_keys: int = 1000):
    keys, prefixes, token = [], [], None
    while True:
        params = {"list-type": "2", "prefix": prefix, "max-keys": str(max_keys)}
        if delimiter:
            params["delimiter"] = delimiter
        if token:
            params["continuation-token"] = token
        r = await client().get(bucket + "/", params=params)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        keys += [c.findtext(f"{_NS}Key") for c in root.iter(f"{_NS}Contents")]
        prefixes += [p.findtext(f"{_NS}Prefix") for p in root.iter(f"{_NS}CommonPrefixes")]
        token = root.findtext(f"{_NS}NextContinuationToken")
        if not token or max_keys < 1000:
            return keys, prefixes


async def get_bytes(url: str) -> bytes:
    r = await client().get(url)
    r.raise_for_status()
    return r.content


# ---------- archive ----------

_ARCH_RE = re.compile(r"/([A-Z]{4})(\d{8})_(\d{6})_V\d\d$")


async def list_archive(site: str, since: datetime) -> list[tuple[datetime, str]]:
    """Complete volumes for `site` from `since` until now, oldest first."""
    out = []
    day = since.date()
    today = datetime.now(timezone.utc).date()
    while day <= today:
        keys, _ = await _list(ARCHIVE, f"{day:%Y/%m/%d}/{site}/")
        for k in keys:
            m = _ARCH_RE.search(k)
            if not m:   # skips *_MDM metadata files
                continue
            t = datetime.strptime(m.group(2) + m.group(3), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            if t >= since:
                out.append((t, f"{ARCHIVE}/{k}"))
        day += timedelta(days=1)
    return sorted(out)


# ---------- realtime chunks ----------

_CHUNK_RE = re.compile(r"/(\d+)/(\d{8}-\d{6})-(\d{3})-([SIE])$")


def parse_chunk_key(key: str):
    m = _CHUNK_RE.search(key)
    if not m:
        return None
    t = datetime.strptime(m.group(2), "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
    return int(m.group(1)), t, int(m.group(3)), m.group(4)


async def chunk_keys(site: str, vol: int) -> list[str]:
    keys, _ = await _list(CHUNKS, f"{site}/{vol}/")
    parsed = [(parse_chunk_key(k), k) for k in keys]
    return [k for p, k in sorted((p, k) for p, k in parsed if p)]


async def _vol_start(site: str, vol: int) -> datetime | None:
    keys, _ = await _list(CHUNKS, f"{site}/{vol}/", max_keys=1)
    p = parse_chunk_key(keys[0]) if keys else None
    return p[1] if p else None


async def latest_chunk_volume(site: str) -> int | None:
    """Volume dirs are numbered 1..999 and wrap, so the list is a rotated sorted
    array of start times. Binary-search for its maximum (~10 requests)."""
    _, prefixes = await _list(CHUNKS, f"{site}/", delimiter="/")
    vols = sorted(int(p.rstrip("/").split("/")[-1]) for p in prefixes if p.rstrip("/").split("/")[-1].isdigit())
    if not vols:
        return None
    cache: dict[int, datetime | None] = {}

    async def t(i: int) -> datetime:
        if vols[i] not in cache:
            cache[vols[i]] = await _vol_start(site, vols[i])
        return cache[vols[i]] or datetime.min.replace(tzinfo=timezone.utc)

    lo, hi = 0, len(vols) - 1
    t0 = await t(0)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if await t(mid) >= t0:
            lo = mid
        else:
            hi = mid - 1
    best = vols[lo]
    start = await t(lo)
    if datetime.now(timezone.utc) - start > timedelta(minutes=30):
        return None  # radar offline / feed stale
    return best


def next_vol(vol: int) -> int:
    return 1 if vol >= 999 else vol + 1
