"""GRIB2 message decoding with ecCodes. Projection-agnostic: grids are described by their 2-D
latitude/longitude arrays, which covers HRRR (Lambert conformal) and RRFS (Lambert conformal or
rotated lat/lon) alike."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import eccodes
import numpy as np


@dataclass
class Grid:
    lat: np.ndarray  # (ny, nx) float32
    lon: np.ndarray  # (ny, nx) float32, -180..180
    grid_id: str
    grid_type: str


def _shape(gid) -> tuple[int, int]:
    for nx_key, ny_key in (("Nx", "Ny"), ("Ni", "Nj")):
        try:
            nx, ny = eccodes.codes_get(gid, nx_key), eccodes.codes_get(gid, ny_key)
            if nx and ny and nx > 0 and ny > 0:
                return int(ny), int(nx)
        except eccodes.KeyValueNotFoundError:
            continue
    raise ValueError("cannot determine grid shape")


def decode(msg: bytes, want_grid: bool = False) -> tuple[np.ndarray, Grid | None]:
    gid = eccodes.codes_new_from_message(msg)
    try:
        ny, nx = _shape(gid)
        vals = eccodes.codes_get_values(gid).astype(np.float32)
        try:
            if eccodes.codes_get(gid, "bitmapPresent"):
                miss = eccodes.codes_get(gid, "missingValue")
                vals[vals == miss] = np.nan
        except eccodes.KeyValueNotFoundError:
            pass
        vals = vals.reshape(ny, nx)
        grid = None
        if want_grid:
            lat = eccodes.codes_get_array(gid, "latitudes").astype(np.float32).reshape(ny, nx)
            lon = eccodes.codes_get_array(gid, "longitudes").astype(np.float32).reshape(ny, nx)
            lon = ((lon + 180.0) % 360.0) - 180.0
            gtype = str(eccodes.codes_get(gid, "gridType"))
            h = hashlib.sha1()
            h.update(f"{gtype}:{ny}x{nx}".encode())
            h.update(np.round(lat[:: max(ny // 7, 1), :: max(nx // 7, 1)], 3).tobytes())
            h.update(np.round(lon[:: max(ny // 7, 1), :: max(nx // 7, 1)], 3).tobytes())
            grid = Grid(lat, lon, h.hexdigest()[:12], gtype)
        return vals, grid
    finally:
        eccodes.codes_release(gid)
