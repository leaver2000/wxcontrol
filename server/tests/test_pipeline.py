"""End to end: fake NOAA bucket -> idx/range fetch -> ecCodes -> nzthermo -> Icechunk -> API."""

import asyncio
import base64
import importlib
from datetime import datetime, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tests import fakenoaa

RUN = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
LEVELS = [1000.0, 925.0, 850.0, 700.0, 600.0, 500.0, 400.0, 300.0, 250.0, 200.0, 150.0, 100.0]
HRRR_PATH = "hrrr.{date}/conus/hrrr.t{cycle:02d}z.{product}f{fhr:02d}.grib2"
RRFS_PATH = "rrfs.{date}/{cycle:02d}/rrfs.t{cycle:02d}z.{product}.3km.f{fhr:03d}.conus.grib2"


@pytest.fixture(scope="module")
def env(tmp_path_factory, monkeypatch_module):
    root = tmp_path_factory.mktemp("bucket")
    fakenoaa.make_bucket(root / "hrrr", "hrrr", HRRR_PATH, RUN, [0, 1, 2], LEVELS)
    fakenoaa.make_bucket(root / "rrfs", "rrfs", RRFS_PATH, RUN, [0, 1], LEVELS, rrfs_style=True)
    base, srv = fakenoaa.serve(root)
    store_dir = tmp_path_factory.mktemp("icechunk")
    for k, v in {
        "WX_STORE": "local", "WX_LOCAL_PATH": str(store_dir), "WX_MODELS": "hrrr,rrfs",
        "WX_HRRR_BASE_URL": f"{base}/hrrr", "WX_RRFS_BASE_URL": f"{base}/rrfs",
        "WX_LEVELS": ",".join(str(int(x)) for x in LEVELS), "WX_MAX_FHR": "2", "WX_DERIVE_STRIDE": "2", "WX_MAP_RES": "0.5",
    }.items():
        monkeypatch_module.setenv(k, v)
    from wxserver import config
    config.get_settings.cache_clear()
    yield base
    srv.shutdown()


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


@pytest.fixture(scope="module")
def ingested(env):
    from wxserver import ingest
    asyncio.run(ingest.main_async(["once", "--run", "2026112712"]))
    return env


@pytest.fixture(scope="module")
def client(ingested):
    import wxserver.api as api
    importlib.reload(api)
    return TestClient(api.app)


def test_catalog(client):
    cat = client.get("/api/catalog").json()["models"]
    by = {m["id"]: m for m in cat}
    assert set(by) == {"hrrr", "rrfs"}
    assert by["hrrr"]["runs"][0]["fhrs"] == [0, 1, 2]
    assert by["rrfs"]["runs"][0]["fhrs"] == [0, 1]  # f02 not published -> stops cleanly
    assert by["hrrr"]["runs"][0]["levels"] == LEVELS
    lon0, lat0, lon1, lat1 = by["hrrr"]["bbox"]
    assert -140 < lon0 < lon1 < -50 and 15 < lat0 < lat1 < 60


def _band(js, name):
    return np.frombuffer(base64.b64decode(js["bands"][name]), "<f4").reshape(js["ny"], js["nx"])


def test_map_surface_and_level(client):
    js = client.get("/api/map", params={"model": "hrrr", "fhr": 1, "level": "sfc", "bands": "t,rh,u,v,iso,pr,sbcape,mucape,li"}).json()
    t = _band(js, "t")
    assert js["nx"] > 10 and np.isfinite(t).mean() > 0.5
    assert -40 < np.nanmin(t) < np.nanmax(t) < 45
    cape = _band(js, "sbcape")
    assert np.nanmax(cape) > 500  # warm moist south is unstable
    assert np.nanmin(_band(js, "iso")) > 1000 - 20
    js = client.get("/api/map", params={"model": "hrrr", "fhr": 1, "level": "500", "bands": "t,iso,bg,cf"}).json()
    z = _band(js, "iso")
    assert 5000 < np.nanmean(z) < 6200
    w = _band(client.get("/api/map", params={"model": "hrrr", "fhr": 1, "level": "500", "bands": "w"}).json(), "w")
    assert np.nanmin(w) < -0.3 and np.nanmax(w) > 0.1  # omega (Pa/s): rising south, sinking north
    bg = client.get("/api/map", params={"model": "hrrr", "fhr": 1, "level": "850", "bands": "bg"}).json()
    assert np.nanmax(_band(bg, "bg")) == 1.0  # terrain pokes through 850 hPa


def test_point_sounding_and_series(client):
    js = client.get("/api/point", params={"model": "hrrr", "lat": 30.0, "lon": -95.0}).json()
    assert [s["fhr"] for s in js["steps"]] == [0, 1, 2]
    s0 = js["steps"][0]
    assert len(s0["lev"]["t"]) == len(LEVELS)
    assert s0["parcel"]["sbcape"] > 500 and s0["parcel"]["lcl_p"] < s0["sfc"]["p"]
    assert s0["parcel"]["trace"][0][0] == pytest.approx(s0["sfc"]["p"], abs=0.2)
    hi = client.get("/api/point", params={"model": "hrrr", "lat": 40.0, "lon": -106.0}).json()
    assert hi["h"] > 1000 and hi["steps"][0]["sfc"]["p"] < 900  # mountain station


def test_rrfs_uses_rh_and_prmsl(client):
    js = client.get("/api/point", params={"model": "rrfs", "lat": 32.0, "lon": -90.0}).json()
    s = js["steps"][-1]
    assert all(td <= t for t, td in zip(s["lev"]["t"], s["lev"]["td"]))
    assert 990 < s["sfc"]["mslp"] < 1020


def test_reingest_is_idempotent(ingested):
    from wxserver import ingest
    from wxserver.config import get_settings
    from wxserver.models import registry
    from wxserver.store import WxStore
    from wxserver.fetch import Fetcher

    s = get_settings()

    async def go():
        f = Fetcher()
        try:
            return await ingest.ingest_run(registry(s)["hrrr"], RUN, s, WxStore(s), f)
        finally:
            await f.close()

    assert asyncio.run(go()) == 0


def test_errors(client):
    assert client.get("/api/map", params={"model": "nope", "fhr": 0}).status_code == 404
    assert client.get("/api/map", params={"model": "hrrr", "fhr": 9}).status_code == 404
    assert client.get("/api/point", params={"model": "hrrr", "lat": -40, "lon": 10}).status_code == 404
