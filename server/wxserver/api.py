"""Read API for the Wx Analysis Console.

GET /api/health
GET /api/catalog                     models, runs, available forecast hours and levels
GET /api/map    model run fhr level  fields resampled from the native grid onto a regular lat/lon
                bands bbox res       grid; float32 little-endian, base64 per band
GET /api/point  model run lat lon    every stored forecast hour at the nearest grid point: the
                                     column, surface values and nzthermo parcel analysis
"""

from __future__ import annotations

import base64
import logging
import threading
from datetime import datetime, timedelta
from functools import lru_cache

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from scipy.spatial import cKDTree

from . import thermo
from .config import get_settings
from .store import WxStore, parse_run

log = logging.getLogger("wxserver.api")
settings = get_settings()
app = FastAPI(title="wxserver", version="0.1.0")
app.add_middleware(GZipMiddleware, minimum_size=2048)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins.split(","), allow_methods=["GET"], allow_headers=["*"])

_store: WxStore | None = None
_lock = threading.Lock()


def store() -> WxStore:
    global _store
    with _lock:
        if _store is None:
            _store = WxStore(settings)
        return _store


def _xyz(lat, lon):
    la, lo = np.radians(lat), np.radians(lon)
    return np.stack([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)], -1)


class GridIndex:
    """Nearest-neighbour lookup on a curvilinear grid (any projection) via a KD-tree on the sphere."""

    def __init__(self, lat: np.ndarray, lon: np.ndarray):
        self.shape = lat.shape
        self.tree = cKDTree(_xyz(lat.ravel().astype(np.float64), lon.ravel().astype(np.float64)))
        ny, nx = lat.shape
        d = _xyz(lat[ny // 2, nx // 2], lon[ny // 2, nx // 2]) - _xyz(lat[ny // 2, nx // 2 + 1], lon[ny // 2, nx // 2 + 1])
        self.spacing = float(np.linalg.norm(d))  # chord length of one grid step

    def query(self, lat, lon, max_steps: float = 1.5):
        dist, k = self.tree.query(_xyz(lat, lon), distance_upper_bound=self.spacing * max_steps, workers=-1)
        ok = np.isfinite(dist)
        k = np.where(ok, k, 0)
        j, i = np.unravel_index(k, self.shape)
        return j, i, ok


@lru_cache(maxsize=4)
def grid_index(model: str, grid_id: str) -> GridIndex:
    g = store().reader()[f"{model}/grid"]
    return GridIndex(g["lat"][:], g["lon"][:])


def _model(model: str):
    root = store().reader()
    if model not in root:
        raise HTTPException(404, f"unknown model {model}")
    return root[model]


def _run(model: str, run: str | None):
    mg = _model(model)
    keys = sorted((k for k in mg["runs"].group_keys() if mg["runs"][k].attrs.get("done")), reverse=True)
    if not keys:
        raise HTTPException(404, f"no runs stored for {model}")
    rk = keys[0] if run in (None, "", "latest") else run
    if rk not in keys:
        raise HTTPException(404, f"run {rk} not stored for {model}")
    return mg, mg["runs"][rk], rk


def _b64(a: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(a, dtype="<f4").tobytes()).decode()


@app.get("/api/health")
def health():
    return {"ok": True, "store": settings.store}


@app.get("/api/catalog")
def catalog():
    return {"models": store().catalog()}


SFC_BANDS = {"t": "s_t2m", "td": "s_td2m", "u": "s_u10", "v": "s_v10", "iso": "s_mslp", "cf": "s_tcc", "pr": "s_prate", "sn": "s_csnow", "refc": "s_refc", "gust": "s_gust", "psfc": "s_psfc", "sbcape": "s_sbcape", "sbcin": "s_sbcin", "mucape": "s_mucape", "mlcape": "s_mlcape", "lcl": "s_lcl", "li": "s_li"}
LEVEL_BANDS = {"t": "m_t", "td": "m_td", "u": "m_u", "v": "m_v", "iso": "m_z"}
SFC_ONLY = {"pr", "sn", "refc", "gust", "psfc", "sbcape", "sbcin", "mucape", "mlcape", "lcl", "li"}


@app.get("/api/map")
def map_fields(
    model: str,
    fhr: int,
    level: str = "sfc",
    run: str | None = None,
    bands: str = "t,rh,u,v,iso,cf,bg,pr,sn",
    bbox: str | None = None,
    res: float | None = None,
):
    mg, rg, rk = _run(model, run)
    a = rg.attrs
    if fhr not in a["done"]:
        raise HTTPException(404, f"f{fhr:03d} not stored for {model} {rk}")
    fi = a["fhrs"].index(fhr)
    want = [b.strip() for b in bands.split(",") if b.strip()]
    lon0, lat0, lon1, lat1 = (float(x) for x in bbox.split(",")) if bbox else mg.attrs["bbox"]
    res = max(float(res or settings.map_res), 0.01)
    nx, ny = int(np.ceil((lon1 - lon0) / res)), int(np.ceil((lat1 - lat0) / res))
    if nx * ny > settings.max_map_cells:
        raise HTTPException(400, f"{nx}x{ny} cells exceeds limit; use a coarser res or smaller bbox")
    lons = lon0 + (np.arange(nx) + 0.5) * res
    lats = lat0 + (np.arange(ny) + 0.5) * res
    LO, LA = np.meshgrid(lons, lats)
    gi = grid_index(model, mg.attrs["grid_id"])
    j, i, ok = gi.query(LA.ravel(), LO.ravel())
    if not ok.any():
        raise HTTPException(404, "bbox does not overlap the model domain")
    j0, j1, i0, i1 = j[ok].min(), j[ok].max() + 1, i[ok].min(), i[ok].max() + 1
    jj, ii = j - j0, i - i0

    is_sfc = level.lower() in ("sfc", "surface")
    li = None
    if not is_sfc:
        try:
            li = [float(x) for x in a["levels"]].index(float(level))
        except ValueError:
            raise HTTPException(404, f"level {level} not stored; have {a['levels']}") from None

    cache: dict[str, np.ndarray] = {}

    def plane(name: str) -> np.ndarray:
        if name not in cache:
            arr = rg[name]
            cache[name] = arr[fi, li, j0:j1, i0:i1] if name.startswith("m_") else arr[fi, j0:j1, i0:i1]
        return cache[name]

    def pick(name: str) -> np.ndarray:
        out = plane(name)[jj, ii].astype(np.float32)
        out[~ok] = np.nan
        return out.reshape(ny, nx)

    out: dict[str, str] = {}
    for b in want:
        if b == "rh":
            t = pick(SFC_BANDS["t"] if is_sfc else LEVEL_BANDS["t"])
            td = pick(SFC_BANDS["td"] if is_sfc else LEVEL_BANDS["td"])
            out[b] = _b64(thermo.rh_from_td(t, td))
        elif b == "cf" and not is_sfc:
            rh = thermo.rh_from_td(pick("m_t"), pick("m_td"))
            out[b] = _b64(np.clip((rh - 72) / 26, 0, 1) ** 1.3)
        elif b == "bg":
            out[b] = _b64(np.zeros((ny, nx), np.float32) if is_sfc else (pick("s_psfc") < a["levels"][li]).astype(np.float32))
        elif is_sfc and b in SFC_BANDS:
            out[b] = _b64(pick(SFC_BANDS[b]))
        elif not is_sfc and b in LEVEL_BANDS:
            out[b] = _b64(pick(LEVEL_BANDS[b]))
        elif not is_sfc and b in SFC_ONLY and b in SFC_BANDS:
            out[b] = _b64(pick(SFC_BANDS[b]))
    init = datetime.fromisoformat(a["init"])
    return {
        "model": model, "run": rk, "fhr": fhr, "valid": (init + timedelta(hours=fhr)).isoformat(), "level": "sfc" if is_sfc else a["levels"][li],
        "lon0": lon0, "lat0": lat0, "dlon": res, "dlat": res, "nx": nx, "ny": ny, "bands": out,
    }


@app.get("/api/point")
def point(model: str, lat: float, lon: float, run: str | None = None):
    mg, rg, rk = _run(model, run)
    a = rg.attrs
    gi = grid_index(model, mg.attrs["grid_id"])
    j, i, ok = gi.query(np.array([lat]), np.array([lon]), max_steps=3)
    if not ok[0]:
        raise HTTPException(404, "point is outside the model domain")
    j, i = int(j[0]), int(i[0])
    fhrs = a["fhrs"]
    done = [f for f in fhrs if f in a["done"]]
    idx = [fhrs.index(f) for f in done]
    levels = np.asarray(a["levels"], np.float64)
    col = {v: rg[f"c_{v}"][:, :, j, i][idx] for v in ("z", "t", "td", "u", "v")}
    sf = {k: rg[f"s_{k}"][:, j, i][idx] for k in ("t2m", "td2m", "u10", "v10", "psfc", "mslp", "gust", "prate", "csnow", "tcc", "refc", "sbcape", "mucape", "mlcape", "li")}
    g = mg["grid"]
    hgt = float(g["hgt"][j, i])
    P, T, D = thermo.surface_profiles(levels, col["t"].astype(np.float64), col["td"].astype(np.float64), sf["psfc"].astype(np.float64), sf["t2m"].astype(np.float64), sf["td2m"].astype(np.float64))
    parcels = thermo.sounding_parcels(P, T, D)
    init = datetime.fromisoformat(a["init"])

    def r(x, nd=2):
        x = float(x)
        return None if not np.isfinite(x) else round(x, nd)

    steps = []
    for n, f in enumerate(done):
        steps.append({
            "fhr": f,
            "valid": (init + timedelta(hours=f)).isoformat(),
            "sfc": {"p": r(sf["psfc"][n]), "T": r(sf["t2m"][n]), "Td": r(sf["td2m"][n]), "u": r(sf["u10"][n]), "v": r(sf["v10"][n]), "gust": r(sf["gust"][n]), "mslp": r(sf["mslp"][n]), "pr": r(sf["prate"][n], 3), "snow": bool(sf["csnow"][n] >= 0.5), "tcc": r(sf["tcc"][n]), "refc": r(sf["refc"][n], 1)},
            "lev": {k: [r(x) for x in col[k][n]] for k in ("z", "t", "td", "u", "v")},
            "parcel": parcels[n],
        })
    return {
        "model": model, "run": rk, "init": a["init"], "lat": lat, "lon": lon, "grid_lat": r(g["lat"][j, i], 4), "grid_lon": r(g["lon"][j, i], 4), "h": r(hgt, 1), "levels": a["levels"], "steps": steps,
    }


def run_time(rk: str) -> datetime:
    return parse_run(rk)


if settings.client_dir:  # optional: serve the built client from the same origin (dev / single container)
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=settings.client_dir, html=True), name="client")
