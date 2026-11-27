"""Async HTTP: .idx inventories and byte-range GRIB message downloads."""

from __future__ import annotations

import asyncio
import logging

import httpx

from .idx import Entry, plan_ranges

log = logging.getLogger(__name__)


class Fetcher:
    def __init__(self, concurrency: int = 8, timeout: float = 120.0, merge_gap: int = 262_144):
        self.sem = asyncio.Semaphore(concurrency)
        self.merge_gap = merge_gap
        self.client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    async def close(self) -> None:
        await self.client.aclose()

    async def text(self, url: str) -> str | None:
        """Return the body, or None if the object does not exist (yet)."""
        async with self.sem:
            for attempt in range(3):
                try:
                    r = await self.client.get(url)
                    if r.status_code in (403, 404):
                        return None
                    r.raise_for_status()
                    return r.text
                except httpx.TransportError as e:
                    log.warning("GET %s failed (%s), retry %d", url, e, attempt + 1)
                    await asyncio.sleep(2**attempt)
        return None

    async def _range(self, url: str, start: int, end: int | None) -> bytes:
        hdr = {"Range": f"bytes={start}-{'' if end is None else end}"}
        async with self.sem:
            for attempt in range(4):
                try:
                    r = await self.client.get(url, headers=hdr)
                    if r.status_code not in (200, 206):
                        r.raise_for_status()
                    body = r.content
                    if r.status_code == 200 and start:  # server ignored Range
                        body = body[start : None if end is None else end + 1]
                    return body
                except (httpx.TransportError, httpx.HTTPStatusError) as e:
                    if attempt == 3:
                        raise
                    log.warning("range %s %s failed (%s), retry", url, hdr["Range"], e)
                    await asyncio.sleep(2**attempt)
        raise RuntimeError("unreachable")

    async def messages(self, url: str, entries: list[Entry]) -> dict[Entry, bytes]:
        """Download the GRIB messages for ``entries`` from ``url``; returns raw message bytes."""
        groups = plan_ranges(entries, self.merge_gap)
        blobs = await asyncio.gather(*(self._range(url, s, e) for s, e, _ in groups))
        out: dict[Entry, bytes] = {}
        for (s, _, members), blob in zip(groups, blobs):
            for m in members:
                a = m.start - s
                b = None if m.end is None else m.end - s + 1
                out[m] = blob[a:b]
        return out
