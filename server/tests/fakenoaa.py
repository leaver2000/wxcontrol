"""A miniature stand-in for NOAA's open-data buckets: real GRIB2 files (Lambert conformal, encoded
with ecCodes) plus wgrib2-style .idx sidecars, served over HTTP with Range support."""

from __future__ import annotations

import http.server
import os
import re
import threading
from datetime import datetime
from pathlib import Path

import eccodes
import numpy as np

NX, NY = int(__import__("os").environ.get("FAKE_NX", 64)), int(__import__("os").environ.get("FAKE_NY", 44))


def _template() -> int:
    gid = eccodes.codes_grib_new_from_samples("GRIB2")
    eccodes.codes_set(gid, "gridDefinitionTemplateNumber", 30)
    for k, v in {
        "Nx": NX, "Ny": NY,
        "latitudeOfFirstGridPointInDegrees": 21.14, "longitudeOfFirstGridPointInDegrees": 237.28,
        "LaDInDegrees": 38.5, "LoVInDegrees": 262.5, "Latin1InDegrees": 38.5, "Latin2InDegrees": 38.5,
        "DxInMetres": int(4.8e6 / NX), "DyInMetres": int(4.8e6 / NX),
    }.items():
        eccodes.codes_set(gid, k, v)
    eccodes.codes_set(gid, "packingType", "grid_simple")
    eccodes.codes_set(gid, "bitsPerValue", 16)
    return gid


def latlon() -> tuple[np.ndarray, np.ndarray]:
    gid = _template()
    eccodes.codes_set_values(gid, np.zeros(NX * NY))
    lat = eccodes.codes_get_array(gid, "latitudes").reshape(NY, NX)
    lon = eccodes.codes_get_array(gid, "longitudes").reshape(NY, NX)
    eccodes.codes_release(gid)
    return lat, ((lon + 180) % 360) - 180


def encode(values: np.ndarray) -> bytes:
    gid = _template()
    eccodes.codes_set_values(gid, values.astype(np.float64).ravel())
    b = eccodes.codes_get_message(gid)
    eccodes.codes_release(gid)
    return b


def zstd(p):
    return 44330.8 * (1 - (p / 1013.25) ** 0.190263)


def fields(fhr: int, levels):
    """A warm, moist, unstable south; cold dry north; a ridge of terrain near 105W."""
    lat, lon = latlon()
    hgt = 1800 * np.exp(-(((lon + 106) / 5) ** 2)) * (lat > 30)
    t0 = 302 - 0.75 * (lat - 25) + 2 * np.sin(2 * np.pi * fhr / 24)
    moist = np.clip((40 - lat) / 15, 0, 1)
    lv = {}
    for p in levels:
        z = zstd(p) + 80 * np.sin(np.radians(lon * 4 + fhr * 3)) * (1 - p / 1100)
        t = np.where(z < 11500, t0 - 0.0068 * z, t0 - 0.0068 * 11500)
        dep = 2 + (1 - moist) * 12 + (1000 - p) / 900 * 25
        lv[p] = {
            "HGT": z, "TMP": t, "DPT": t - dep,
            "RH": 100 * np.exp(17.67 * (t - dep - 273.15) / (t - dep - 29.65) - 17.67 * (t - 273.15) / (t - 29.65)),
            "UGRD": 5 + 35 * (1 - p / 1000) + 0 * lat, "VGRD": 8 * np.sin(np.radians(lon * 3)) + 0 * lat,
            # rising (negative omega) in the moist south, sinking in the north; peaks mid-troposphere
            "VVEL": (0.4 - 1.2 * moist) * np.sin(np.pi * np.clip((1000 - p) / 850, 0, 1)),
        }
    psfc = 101300 * np.exp(-hgt / 8000)
    t2 = t0 - 0.0068 * hgt + 1.5
    sfc = {
        ("TMP", "2 m above ground"): t2, ("DPT", "2 m above ground"): t2 - 2 - (1 - moist) * 12,
        ("RH", "2 m above ground"): 60 + 30 * moist,
        ("UGRD", "10 m above ground"): 4 + 0 * lat, ("VGRD", "10 m above ground"): 6 * moist,
        ("PRES", "surface"): psfc, ("HGT", "surface"): hgt, ("GUST", "surface"): 9 + 0 * lat,
        ("MSLMA", "mean sea level"): 101300 - 800 * moist, ("PRMSL", "mean sea level"): 101300 - 800 * moist,
        ("PRATE", "surface"): moist * 0.0006, ("CSNOW", "surface"): (lat > 48).astype(float),
        ("TCDC", "entire atmosphere"): 100 * moist, ("REFC", "entire atmosphere"): 10 + 40 * moist,
    }
    return lv, sfc


def write_file(path: Path, run: datetime, fcst: str, msgs: list[tuple[str, str, np.ndarray]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines, off = [], 0
    with open(path, "wb") as f:
        for n, (var, level, vals) in enumerate(msgs, 1):
            b = encode(vals)
            f.write(b)
            fc = fcst if var != "APCP" else fcst.replace("hour fcst", "hour acc fcst").replace("anl", "0-0 day acc fcst")
            lines.append(f"{n}:{off}:d={run:%Y%m%d%H}:{var}:{level}:{fc}:")
            off += len(b)
    Path(str(path) + ".idx").write_text("\n".join(lines) + "\n")


def make_bucket(root: Path, model: str, path_tmpl: str, run: datetime, fhrs, levels, *, rrfs_style: bool = False) -> None:
    lvl_prod, sfc_prod = ("prslev", "2dfld") if rrfs_style else ("wrfprs", "wrfsfc")
    for fhr in fhrs:
        fcst = "anl" if fhr == 0 else f"{fhr} hour fcst"
        lv, sfc = fields(fhr, levels)
        lmsgs = []
        for p in levels:  # grouped by level, like the real files
            for var in ("HGT", "TMP", "RH" if rrfs_style else "DPT", "UGRD", "VGRD", "VVEL"):
                lmsgs.append((var, f"{int(p)} mb", lv[p][var]))
        smsgs = [(v, l, a) for (v, l), a in sfc.items() if not (rrfs_style and v in ("MSLMA", "DPT")) and not (not rrfs_style and v == "PRMSL")]
        smsgs.append(("APCP", "surface", sfc[("PRATE", "surface")] * 3600))
        rel = lambda prod: path_tmpl.format(date=run.strftime("%Y%m%d"), cycle=run.hour, fhr=fhr, product=prod)
        write_file(root / rel(lvl_prod), run, fcst, lmsgs)
        write_file(root / rel(sfc_prod), run, fcst, smsgs)


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            self.send_error(404)
            return
        size = os.path.getsize(path)
        m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        with open(path, "rb") as f:
            if m:
                a = int(m.group(1))
                b = int(m.group(2)) if m.group(2) else size - 1
                f.seek(a)
                body = f.read(b - a + 1)
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
            else:
                body = f.read()
                self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(root: Path) -> tuple[str, http.server.ThreadingHTTPServer]:
    handler = lambda *a, **k: _Handler(*a, directory=str(root), **k)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv
