"""Ingest worker: discover the latest HRRR/RRFS cycle, pull the needed GRIB2 messages with byte-range
requests, decode them, derive nzthermo fields, and commit each forecast hour to Icechunk.

    python -m wxserver.ingest loop                          # follow new cycles (compose service)
    python -m wxserver.ingest once --model hrrr --fhr 0-6   # latest cycle, f00..f06
    python -m wxserver.ingest once --model rrfs --run 2026112712
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

import numpy as np

from . import idx as idxmod
from . import thermo
from .config import Settings, get_settings
from .decode import Grid, decode
from .fetch import Fetcher
from .models import ModelSpec, registry
from .store import WxStore, run_key

log = logging.getLogger("wxserver.ingest")


class StepNotAvailable(Exception):
    pass


def _fcst_ok(e: idxmod.Entry, fhr: int) -> bool:
    if fhr == 0:
        return e.fcst == "anl" or e.fcst.startswith("0 ")
    return e.fcst.startswith(f"{fhr} ") or f"-{fhr} " in e.fcst


async def fetch_step(spec: ModelSpec, run: datetime, fhr: int, s: Settings, fetcher: Fetcher):
    lvl_url = spec.url(run, fhr, spec.level_product)
    sfc_url = spec.url(run, fhr, spec.sfc_product)
    lvl_txt, sfc_txt = await asyncio.gather(fetcher.text(lvl_url + ".idx"), fetcher.text(sfc_url + ".idx"))
    if lvl_txt is None:
        raise StepNotAvailable(lvl_url)
    lvl_e = [e for e in idxmod.parse(lvl_txt) if _fcst_ok(e, fhr)]
    sfc_e = [e for e in idxmod.parse(sfc_txt) if _fcst_ok(e, fhr)] if sfc_txt else []

    want = s.level_list()
    avail = idxmod.pressure_levels(lvl_e, "TMP")
    levels = [p for p in want if p in avail]
    if len(levels) < 5:
        raise RuntimeError(f"{spec.id}: only {len(levels)} of the requested pressure levels found in {lvl_url}.idx")

    # humidity source: dewpoint if present on every level, else RH, else specific humidity
    hum = None
    for key, name in (("td", "DPT"), ("rh", "RH"), ("q", "SPFH")):
        found = idxmod.pressure_levels(lvl_e, name)
        if all(p in found for p in levels):
            hum = (key, name)
            break
    if hum is None:
        raise RuntimeError(f"{spec.id}: no humidity field on pressure levels")

    level_pick: dict[str, dict[float, idxmod.Entry]] = {}
    for key, name in (("z", "HGT"), ("t", "TMP"), ("u", "UGRD"), ("v", "VGRD"), hum):
        found = idxmod.pressure_levels(lvl_e, name)
        level_pick[key] = {p: found[p] for p in levels if p in found}
    vvel = idxmod.pressure_levels(lvl_e, "VVEL")  # optional: omega for vertical-motion products
    if all(p in vvel for p in levels):
        level_pick["w"] = {p: vvel[p] for p in levels}

    sfc_pick: dict[str, tuple[str, idxmod.Entry]] = {}
    for sv in spec.sfc_vars:
        e = idxmod.pick(sfc_e, sv.names, sv.level)
        src = "sfc"
        if e is None:
            e, src = idxmod.pick(lvl_e, sv.names, sv.level), "lvl"
        if e is not None:
            sfc_pick[sv.key] = (src, e)
        elif sv.required:
            raise RuntimeError(f"{spec.id} f{fhr:03d}: required surface field {sv.key} missing")

    lvl_entries = [e for d in level_pick.values() for e in d.values()] + [e for src, e in sfc_pick.values() if src == "lvl"]
    sfc_entries = [e for src, e in sfc_pick.values() if src == "sfc"]
    t0 = time.monotonic()
    lvl_msgs, sfc_msgs = await asyncio.gather(fetcher.messages(lvl_url, lvl_entries), fetcher.messages(sfc_url, sfc_entries) if sfc_entries else _empty())
    nbytes = sum(map(len, lvl_msgs.values())) + sum(map(len, sfc_msgs.values()))
    log.info("%s %s f%03d: %d messages, %.1f MB in %.1fs", spec.id, run_key(run), fhr, len(lvl_msgs) + len(sfc_msgs), nbytes / 1e6, time.monotonic() - t0)
    return levels, hum[0], level_pick, sfc_pick, lvl_msgs, sfc_msgs


async def _empty():
    return {}


def build_step(levels, hum_key, level_pick, sfc_pick, lvl_msgs, sfc_msgs, stride: int):
    """Decode messages into unit-converted arrays on the (strided) native grid."""
    grid: Grid | None = None
    sl = (slice(None, None, stride), slice(None, None, stride))

    def dec(e, msgs):
        nonlocal grid
        vals, g = decode(msgs[e], want_grid=grid is None)
        if g is not None:
            grid = Grid(g.lat[sl], g.lon[sl], g.grid_id, g.grid_type)
        return vals[sl]

    level_fields: dict[str, np.ndarray] = {}
    for key, per in level_pick.items():
        level_fields[key] = np.stack([dec(per[p], lvl_msgs) for p in levels]).astype(np.float32)
    level_fields["t"] -= 273.15
    if hum_key == "td":
        level_fields["td"] -= 273.15
    elif hum_key == "rh":
        level_fields["td"] = thermo.td_from_rh(level_fields["t"], level_fields.pop("rh")).astype(np.float32)
    else:
        p = np.asarray(levels, np.float32)[:, None, None]
        level_fields["td"] = thermo.td_from_q(p, level_fields.pop("q")).astype(np.float32)
    level_fields["td"] = np.minimum(level_fields["td"], level_fields["t"])

    sfc: dict[str, np.ndarray] = {}
    for key, (src, e) in sfc_pick.items():
        sfc[key] = dec(e, sfc_msgs if src == "sfc" else lvl_msgs).astype(np.float32)
    sfc["t2m"] -= 273.15
    if "td2m" in sfc:
        sfc["td2m"] -= 273.15
    elif "rh2m" in sfc:
        sfc["td2m"] = thermo.td_from_rh(sfc["t2m"], sfc["rh2m"]).astype(np.float32)
    else:
        sfc["td2m"] = level_fields["td"][0].copy()
    sfc.pop("rh2m", None)
    sfc["td2m"] = np.minimum(sfc["td2m"], sfc["t2m"])
    sfc["psfc"] /= 100.0
    if "mslp" in sfc:
        sfc["mslp"] /= 100.0
    else:  # hypsometric reduction as a fallback
        h = sfc.get("hgt", np.zeros_like(sfc["psfc"]))
        sfc["mslp"] = sfc["psfc"] * np.exp(h / (29.27 * (sfc["t2m"] + 273.15 + 0.0065 * h / 2)))
    if "prate" in sfc:
        sfc["prate"] = np.maximum(sfc["prate"] * 3600.0, 0)  # kg m-2 s-1 -> mm/h
    if "tcc" in sfc:
        sfc["tcc"] = np.clip(sfc["tcc"] / 100.0, 0, 1)
    if "refc" in sfc:
        sfc["refc"] = np.where(sfc["refc"] < -20, np.nan, sfc["refc"])
    return grid, level_fields, sfc


def derive(levels, level_fields, sfc, ds: int) -> dict[str, np.ndarray]:
    """Gridded nzthermo indices on every ``ds``-th point, expanded back to the full grid."""
    t = level_fields["t"][:, ::ds, ::ds]
    td = level_fields["td"][:, ::ds, ::ds]
    nl, ny, nx = t.shape
    P, T, D = thermo.surface_profiles(
        levels,
        t.reshape(nl, -1).T.astype(np.float64),
        td.reshape(nl, -1).T.astype(np.float64),
        sfc["psfc"][::ds, ::ds].ravel().astype(np.float64),
        sfc["t2m"][::ds, ::ds].ravel().astype(np.float64),
        sfc["td2m"][::ds, ::ds].ravel().astype(np.float64),
    )
    res = thermo.gridded_indices(P, T, D)
    full_shape = sfc["t2m"].shape
    out = {}
    for k, v in res.items():
        a = v.reshape(ny, nx)
        if ds > 1:
            a = np.repeat(np.repeat(a, ds, 0), ds, 1)
        out[k] = a[: full_shape[0], : full_shape[1]].astype(np.float32)
    return out


async def ingest_step(spec: ModelSpec, run: datetime, fhr: int, s: Settings, store: WxStore, fetcher: Fetcher) -> None:
    levels, hum_key, level_pick, sfc_pick, lvl_msgs, sfc_msgs = await fetch_step(spec, run, fhr, s, fetcher)
    t0 = time.monotonic()
    grid, level_fields, sfc = await asyncio.to_thread(build_step, levels, hum_key, level_pick, sfc_pick, lvl_msgs, sfc_msgs, s.stride)
    del lvl_msgs, sfc_msgs
    store.ensure_model(spec.id, spec.name, grid, s.stride, sfc.get("hgt"))
    derived = await asyncio.to_thread(derive, levels, level_fields, sfc, max(1, s.derive_stride))
    t1 = time.monotonic()
    sfc.pop("hgt", None)
    sfc.update(derived)
    fhrs = list(range(0, min(s.max_fhr, spec.max_fhr(run)) + 1))
    if fhr not in fhrs:
        fhrs.append(fhr)
    st = store.run_state(spec.id, run)
    if st is not None:
        fhrs = st["fhrs"]
        if st["levels"] != levels:
            raise RuntimeError(f"level set changed within run {run_key(run)}")
    await asyncio.to_thread(store.write_step, spec.id, run, fhrs, fhr, levels, level_fields, sfc, s.derive_stride)
    log.info("%s %s f%03d: decoded+derived %.1fs, stored %.1fs", spec.id, run_key(run), fhr, t1 - t0, time.monotonic() - t1)


def _cycle_ok(s: Settings, spec: ModelSpec, h: int) -> bool:
    return s.cycles == "all" or h in spec.synoptic_cycles


async def latest_run(spec: ModelSpec, s: Settings, fetcher: Fetcher, now: datetime | None = None) -> datetime | None:
    now = (now or datetime.now(timezone.utc)).replace(minute=0, second=0, microsecond=0)
    for h in range(s.lookback_hours + 1):
        run = now - timedelta(hours=h)
        if not _cycle_ok(s, spec, run.hour):
            continue
        if await fetcher.text(spec.url(run, 0, spec.level_product) + ".idx") is not None:
            return run
    return None


async def ingest_run(spec: ModelSpec, run: datetime, s: Settings, store: WxStore, fetcher: Fetcher, fhrs: list[int] | None = None) -> int:
    """Ingest every not-yet-stored forecast hour that is available; returns how many were added."""
    st = store.run_state(spec.id, run)
    done = set(st["done"]) if st else set()
    plan = fhrs if fhrs is not None else list(range(0, min(s.max_fhr, spec.max_fhr(run)) + 1))
    added = 0
    for fhr in plan:
        if fhr in done:
            continue
        try:
            await ingest_step(spec, run, fhr, s, store, fetcher)
            added += 1
        except StepNotAvailable:
            log.info("%s %s f%03d not published yet", spec.id, run_key(run), fhr)
            break
    if added and not st:
        dropped = store.prune(spec.id, s.retain_runs)
        if dropped:
            log.info("%s: pruned runs %s", spec.id, dropped)
    return added


def _parse_fhrs(text: str | None) -> list[int] | None:
    if not text:
        return None
    if "-" in text:
        a, b = text.split("-")
        return list(range(int(a), int(b) + 1))
    return [int(x) for x in text.split(",")]


async def main_async(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="wxserver.ingest")
    ap.add_argument("mode", choices=["once", "loop"])
    ap.add_argument("--model", action="append", help="model id (repeatable); default: WX_MODELS")
    ap.add_argument("--run", help="YYYYMMDDHH or 'latest' (default)")
    ap.add_argument("--fhr", help="e.g. 0-6 or 0,3,6")
    a = ap.parse_args(argv)
    s = get_settings()
    specs = registry(s)
    ids = a.model or list(specs)
    store = WxStore(s)
    fetcher = Fetcher(s.http_concurrency, s.http_timeout, s.merge_gap_bytes)
    try:
        last_gc = 0.0
        while True:
            for mid in ids:
                spec = specs[mid]
                try:
                    if a.run and a.run != "latest":
                        run = datetime.strptime(a.run, "%Y%m%d%H").replace(tzinfo=timezone.utc)
                    else:
                        run = await latest_run(spec, s, fetcher)
                    if run is None:
                        log.warning("%s: no cycle found in the last %dh", mid, s.lookback_hours)
                        continue
                    n = await ingest_run(spec, run, s, store, fetcher, _parse_fhrs(a.fhr))
                    log.info("%s %s: %d new forecast hours", mid, run_key(run), n)
                except Exception:
                    log.exception("%s: ingest failed", mid)
                    if a.mode == "once":
                        raise
            if a.mode == "once":
                break
            if time.monotonic() - last_gc > 6 * 3600:
                await asyncio.to_thread(store.collect_garbage)
                last_gc = time.monotonic()
            await asyncio.sleep(s.poll_seconds)
    finally:
        await fetcher.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
