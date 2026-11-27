# wxcontrol: Wx Analysis Console

A weather analysis platform with three linked views:

- **Map**, in the style of windy.com: color-filled fields (wind, temperature, humidity, clouds, rain/snow, CAPE, composite reflectivity), animated wind particles, and labelled height contours or isobars.
- **Skew-T log-P**, styled after Air Force operational weather squadron products: temperature, dewpoint, a surface parcel with CAPE/CIN shading, LCL/LFC/EL, wind barbs, a hodograph and stability indices.
- **Meteogram**: a time–height cross-section (RH, isotherms, wind) above panels for 2 m T/Td, 10 m wind and gust, MSLP, and precipitation with cloud cover.

All three views share one cursor in **X Y Z T**:

| View      | Controls | Input |
|-----------|----------|-------|
| Map       | Y X      | Click a point, or choose a station |
| Skew-T    | Z        | Click or drag vertically; ↑ ↓ when focused |
| Meteogram | T, Z     | Click or drag for T; height in the time–height panel sets Z; ← → (Shift = 6 h), ↑ ↓, Play |

## Architecture

```
  NOAA open data (S3)                         docker compose
  ───────────────────                         ─────────────────────────────────────────────────────
  noaa-hrrr-bdp-pds     .idx + byte ranges    ingest ──► db (SeaweedFS, S3 API) ◄── server ◄── client
  noaa-rrfs-ops-pds   ─────────────────────►  ecCodes      Icechunk repository       FastAPI    nginx + page
                                              nzthermo     (zarr v3, versioned)      /api/*     :8080
```

- **ingest** (`python -m wxserver.ingest loop`) finds the newest HRRR and RRFS cycles. It reads each forecast hour's `.idx` inventory and downloads only the GRIB2 messages it needs, merging adjacent byte ranges. It decodes them with ecCodes on the native 3 km Lambert grid, then uses **nzthermo** to compute gridded SBCAPE, SBCIN, MUCAPE, MLCAPE, LCL pressure and lifted index. Each forecast hour is committed to **Icechunk** as one snapshot, so readers never see a partly written hour. Old runs are pruned, and expired snapshots are garbage-collected.
- **server** (FastAPI) serves three endpoints:
  - `/api/catalog`: models, runs, available hours and levels.
  - `/api/map`: native-grid fields resampled onto a regular lat/lon grid for a bounding box, using a KD-tree on the sphere so any projection works.
  - `/api/point`: every hour of the column at the nearest grid point, plus a full nzthermo parcel analysis (trace, SB/MU/ML CAPE and CIN, LCL, LFC, EL, LI, DCAPE).
- **db** is SeaweedFS running as a single-node S3 gateway. MinIO's community edition stopped publishing images in October 2025. Icechunk needs an S3 server that supports conditional writes (`If-Match`/`If-None-Match`), which SeaweedFS does.
- **client** is the single-page console behind nginx, which also proxies `/api`. On load the page looks for `/api/catalog`. If the API responds, it switches to live data and shows a model and run picker. If not (when opened as a plain file or published as a static page, for example), it falls back to its built-in synthetic atmosphere.

### Storage layout

```
/{model}                     attrs: name, grid_id, ny, nx, stride, bbox
/{model}/grid/lat, lon, hgt
/{model}/runs/{YYYYMMDDHH}   attrs: init, fhrs (planned), done, levels
    m_{z,t,td,u,v}   (fhr, level, y, x)  chunks (1, 1, 256, 256)   map planes
    c_{z,t,td,u,v}   (fhr, level, y, x)  chunks (1, L, 48, 48)     sounding columns
    s_{...}          (fhr, y, x)         chunks (1, 256, 256)      surface + nzthermo fields
```

Pressure-level fields are stored twice. A map needs one level over the whole domain, while a sounding or meteogram needs every level at one point for every hour. With a single chunk shape, one of those reads would pull hundreds of MB.

## Run it

```sh
cp .env.example .env        # models, levels, forecast hours, strides, retention, credentials
docker compose up --build
open http://localhost:8080
```

The first HRRR forecast hour appears a minute or two after start-up; the page polls the catalog every minute. Ingest cost per forecast hour at native resolution is roughly 37 levels × 5 fields plus about 12 surface fields, a few hundred MB of GRIB2. To trade resolution for speed and storage, set `WX_STRIDE=2` and/or a coarser `WX_LEVELS`.

One-off ingest, for a specific cycle or hours:

```sh
docker compose run --rm ingest python -m wxserver.ingest once --model hrrr --fhr 0-6
docker compose run --rm ingest python -m wxserver.ingest once --model rrfs --run 2026112712
```

## Develop without network access

```sh
cd server
pip install "Cython==3.0.11" numpy setuptools setuptools_scm wheel
pip install --no-build-isolation "git+https://github.com/leaver2000/nzthermo@3435f97683ec826ea1ee625cfc95c98ac38a8381"
pip install -e ".[test]" boto3
pytest -q                      # fake NOAA bucket -> ingest -> Icechunk -> API, plus Icechunk over S3 (moto)
python -m tests.devstack       # same pipeline, then serves API + client on http://127.0.0.1:8000
```

`tests/fakenoaa.py` writes real GRIB2 files (Lambert conformal, encoded with ecCodes) with wgrib2-style `.idx` sidecars and serves them over HTTP with Range support. The pipeline under test is the one that runs against NOAA.

The client is `client/src/index.template.html`. Run `node client/tools/build.js` to inline the basemap into `client/index.html`.

## Notes

- **nzthermo build:** current Cython (3.1 and later) rejects the `bint` ufuncs in `_ufunc.pyx`, and pip 25.2 and later no longer applies constraints to isolated build environments. A plain `pip install git+…/nzthermo` therefore fails, so the Dockerfile builds it with Cython 3.0.11 and `--no-build-isolation`.
- **RRFS paths:** RRFS v1 went operational in October 2026 on `noaa-rrfs-ops-pds`. The default path template follows NCEP's naming (`rrfs.YYYYMMDD/CC/rrfs.tCCz.prslev.3km.fFFF.conus.grib2`). If the bucket layout differs, override it with `WX_RRFS_PATH`.
- **Terrain:** pressure levels below the ground are dropped from each profile before nzthermo runs, and the surface (psfc, 2 m T/Td) becomes the first level. nzthermo calls are grouped by the number of levels above ground, so every call gets NaN-free arrays.
