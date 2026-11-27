"""Icechunk-backed storage.

Layout (one Icechunk repository, branch ``main``)::

    /{model}                     attrs: name, grid_id, ny, nx, stride, bbox
    /{model}/grid/lat, lon, hgt  (y, x)
    /{model}/runs/{YYYYMMDDHH}   attrs: init, fhrs (planned), done, levels, derive_stride
        m_{v}  (fhr, level, y, x)  chunks (1, 1, 256, 256)   map planes: one level per chunk
        c_{v}  (fhr, level, y, x)  chunks (1, L, 48, 48)     columns: all levels per chunk
        s_{k}  (fhr, y, x)         chunks (1, 256, 256)      surface + nzthermo-derived fields

Pressure-level fields are stored twice on purpose. A map needs one level over the whole domain;
a sounding or meteogram needs every level at one point for every hour. With a single chunk shape
one of those reads would touch hundreds of MB, so each access pattern gets its own copy.

Every forecast hour is written in one transaction and committed as its own snapshot, so readers
always see whole forecast hours.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import icechunk as ic
import numpy as np
import zarr

from .config import Settings

log = logging.getLogger(__name__)

LEVEL_KEYS = ("z", "t", "td", "u", "v")
SFC_KEYS = ("t2m", "td2m", "u10", "v10", "psfc", "mslp", "gust", "prate", "csnow", "tcc", "refc")
DERIVED_KEYS = ("sbcape", "sbcin", "mucape", "mlcape", "lcl", "li")
MAP_CHUNK = 256
COL_CHUNK = 48


def make_storage(s: Settings) -> ic.Storage:
    if s.store == "s3":
        return ic.s3_storage(
            bucket=s.s3_bucket,
            prefix=s.s3_prefix,
            region=s.s3_region,
            endpoint_url=s.s3_endpoint,
            allow_http=s.s3_allow_http,
            access_key_id=s.s3_access_key,
            secret_access_key=s.s3_secret_key,
            force_path_style=bool(s.s3_endpoint),
        )
    if s.store == "memory":
        return ic.in_memory_storage()
    return ic.local_filesystem_storage(s.local_path)


def run_key(run: datetime) -> str:
    return run.strftime("%Y%m%d%H")


def parse_run(key: str) -> datetime:
    return datetime.strptime(key, "%Y%m%d%H").replace(tzinfo=timezone.utc)


class WxStore:
    def __init__(self, settings: Settings, storage: ic.Storage | None = None):
        self.s = settings
        config = None
        if not settings.s3_conditional_writes:
            config = ic.RepositoryConfig(storage=ic.StorageSettings(unsafe_use_conditional_create=False, unsafe_use_conditional_update=False))
        self.repo = ic.Repository.open_or_create(storage or make_storage(settings), config=config)
        self._ro: tuple[float, zarr.Group] | None = None
        self._ensure_root()

    def _ensure_root(self) -> None:
        sess = self.repo.writable_session("main")
        zarr.open_group(sess.store, mode="a")
        if sess.has_uncommitted_changes:
            try:
                sess.commit("initialise root group")
            except ic.ConflictError:  # another process got there first
                pass

    # ---------------------------------------------------------------- read side
    def reader(self, max_age: float = 5.0) -> zarr.Group:
        """Read-only view of the latest snapshot on main (cached for ``max_age`` seconds)."""
        now = time.monotonic()
        if self._ro is None or now - self._ro[0] > max_age:
            sess = self.repo.readonly_session(branch="main")
            self._ro = (now, zarr.open_group(sess.store, mode="r"))
        return self._ro[1]

    def catalog(self) -> list[dict]:
        root = self.reader()
        out = []
        for mid, mg in root.groups():
            if "runs" not in mg:
                continue
            runs = []
            for rk, rg in mg["runs"].groups():
                a = dict(rg.attrs)
                if not a.get("done"):
                    continue
                runs.append({"run": rk, "init": a["init"], "fhrs": sorted(a["done"]), "planned": a["fhrs"], "levels": a["levels"]})
            runs.sort(key=lambda r: r["run"], reverse=True)
            ma = dict(mg.attrs)
            out.append({"id": mid, "name": ma.get("name", mid), "bbox": ma.get("bbox"), "grid": {"ny": ma.get("ny"), "nx": ma.get("nx"), "stride": ma.get("stride")}, "runs": runs})
        return out

    # ---------------------------------------------------------------- write side
    def writer(self) -> tuple[ic.Session, zarr.Group]:
        sess = self.repo.writable_session("main")
        return sess, zarr.open_group(sess.store, mode="a")

    def ensure_model(self, model: str, name: str, grid, stride: int, hgt: np.ndarray | None) -> None:
        sess, root = self.writer()
        mg = root.require_group(model)
        if mg.attrs.get("grid_id") == grid.grid_id and mg.attrs.get("stride") == stride:
            return
        log.info("initialising grid for %s (%s, %dx%d)", model, grid.grid_id, *grid.lat.shape)
        for k in list(mg.keys()):
            del mg[k]
        gg = mg.create_group("grid")
        ny, nx = grid.lat.shape
        for nm, arr in (("lat", grid.lat), ("lon", grid.lon), ("hgt", hgt if hgt is not None else np.zeros_like(grid.lat))):
            a = gg.create_array(nm, shape=(ny, nx), chunks=(min(ny, 512), min(nx, 512)), dtype="float32")
            a[:] = arr
        mg.create_group("runs")
        mg.attrs.update(
            {
                "name": name,
                "grid_id": grid.grid_id,
                "grid_type": grid.grid_type,
                "ny": ny,
                "nx": nx,
                "stride": stride,
                "bbox": [float(np.nanmin(grid.lon)), float(np.nanmin(grid.lat)), float(np.nanmax(grid.lon)), float(np.nanmax(grid.lat))],
            }
        )
        sess.commit(f"{model}: grid {grid.grid_id}")

    def run_state(self, model: str, run: datetime) -> dict | None:
        root = self.reader(max_age=0)
        try:
            return dict(root[f"{model}/runs/{run_key(run)}"].attrs)
        except KeyError:
            return None

    def write_step(
        self,
        model: str,
        run: datetime,
        fhrs: list[int],
        fhr: int,
        levels: list[float],
        level_fields: dict[str, np.ndarray],
        sfc_fields: dict[str, np.ndarray],
        derive_stride: int,
    ) -> None:
        """Write one forecast hour (all fields) and commit it as a single snapshot."""
        sess, root = self.writer()
        mg = root[model]
        ny, nx = mg.attrs["ny"], mg.attrs["nx"]
        rk = run_key(run)
        runs = mg["runs"]
        nf, nl = len(fhrs), len(levels)
        if rk not in runs:
            rg = runs.create_group(rk)
            for v in LEVEL_KEYS:
                rg.create_array(f"m_{v}", shape=(nf, nl, ny, nx), chunks=(1, 1, MAP_CHUNK, MAP_CHUNK), dtype="float32", fill_value=np.nan)
                rg.create_array(f"c_{v}", shape=(nf, nl, ny, nx), chunks=(1, nl, COL_CHUNK, COL_CHUNK), dtype="float32", fill_value=np.nan)
            for k in SFC_KEYS + DERIVED_KEYS:
                rg.create_array(f"s_{k}", shape=(nf, ny, nx), chunks=(1, MAP_CHUNK, MAP_CHUNK), dtype="float32", fill_value=np.nan)
            rg.attrs.update({"init": run.isoformat(), "fhrs": fhrs, "done": [], "levels": levels, "derive_stride": derive_stride})
        rg = runs[rk]
        i = rg.attrs["fhrs"].index(fhr)
        for v, arr in level_fields.items():
            rg[f"m_{v}"][i] = arr
            rg[f"c_{v}"][i] = arr
        for k, arr in sfc_fields.items():
            rg[f"s_{k}"][i] = arr
        rg.attrs["done"] = sorted(set(rg.attrs["done"]) | {fhr})
        sess.commit(f"{model} {rk} f{fhr:03d}")

    def prune(self, model: str, keep: int) -> list[str]:
        sess, root = self.writer()
        try:
            runs = root[f"{model}/runs"]
        except KeyError:
            return []
        keys = sorted(runs.group_keys(), reverse=True)
        drop = keys[keep:]
        for k in drop:
            del runs[k]
        if drop:
            sess.commit(f"{model}: prune {', '.join(drop)}")
        return drop

    def collect_garbage(self, older_than_hours: float = 6.0) -> None:
        """Expire old snapshots and delete the chunks only they referenced."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
        try:
            self.repo.expire_snapshots(older_than=cutoff)
            self.repo.garbage_collect(cutoff)
        except Exception as e:  # pragma: no cover - maintenance must never kill ingest
            log.warning("garbage collection skipped: %s", e)
