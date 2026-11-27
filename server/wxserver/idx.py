"""Parsing of wgrib2-style .idx inventories and byte-range planning."""

from __future__ import annotations

import re
from dataclasses import dataclass

_MB = re.compile(r"^(\d+(?:\.\d+)?) mb$")
_INSTANT = re.compile(r"^(anl|\d+ (hour|min|day) fcst)$")


@dataclass(frozen=True)
class Entry:
    n: int
    start: int
    end: int | None  # inclusive; None = to end of file
    var: str
    level: str
    fcst: str

    @property
    def pressure_hpa(self) -> float | None:
        m = _MB.match(self.level)
        return float(m.group(1)) if m else None

    @property
    def instantaneous(self) -> bool:
        return bool(_INSTANT.match(self.fcst))


def parse(text: str) -> list[Entry]:
    rows = []
    for line in text.splitlines():
        parts = line.strip().split(":")
        if len(parts) < 6 or not parts[0].isdigit():
            continue
        rows.append((int(parts[0]), int(parts[1]), parts[3], parts[4], parts[5]))
    rows.sort(key=lambda r: r[1])
    out = []
    for i, (n, off, var, lvl, fc) in enumerate(rows):
        end = rows[i + 1][1] - 1 if i + 1 < len(rows) else None
        out.append(Entry(n, off, end, var, lvl, fc))
    return out


def _level_match(entry_level: str, want: str) -> bool:
    if want.endswith("*"):
        return entry_level.startswith(want[:-1])
    return entry_level == want


def pick(entries: list[Entry], names: tuple[str, ...], level: str) -> Entry | None:
    """First message for any of ``names`` (in order of preference) at ``level``, preferring
    instantaneous values over accumulations, averages and maxima."""
    for name in names:
        cands = [e for e in entries if e.var == name and _level_match(e.level, level)]
        if cands:
            inst = [e for e in cands if e.instantaneous]
            return (inst or cands)[0]
    return None


def pressure_levels(entries: list[Entry], name: str) -> dict[float, Entry]:
    out: dict[float, Entry] = {}
    for e in entries:
        if e.var == name and (p := e.pressure_hpa) is not None and p not in out:
            out[p] = e
    return out


def plan_ranges(entries: list[Entry], gap: int) -> list[tuple[int, int | None, list[Entry]]]:
    """Merge messages into as few HTTP range requests as possible. Messages separated by less
    than ``gap`` bytes are fetched together (the unused bytes in between are discarded)."""
    es = sorted(set(entries), key=lambda e: e.start)
    groups: list[tuple[int, int | None, list[Entry]]] = []
    for e in es:
        if groups:
            s, en, members = groups[-1]
            if en is not None and e.start - en - 1 <= gap:
                groups[-1] = (s, e.end, members + [e])
                continue
        groups.append((e.start, e.end, [e]))
    return groups
