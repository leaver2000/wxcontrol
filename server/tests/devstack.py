"""Offline dev stack: fake NOAA bucket -> ingest -> API + client on http://127.0.0.1:8000

    python -m tests.devstack            # from server/, after `pip install -e .[test]`

Useful for working on the client or API without network access to NOAA.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from tests import fakenoaa

RUN = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
LEVELS = [1000.0, 975.0, 950.0, 925.0, 900.0, 850.0, 800.0, 750.0, 700.0, 650.0, 600.0, 550.0, 500.0, 450.0, 400.0, 350.0, 300.0, 250.0, 200.0, 150.0, 100.0]


def main() -> None:
    work = Path(os.environ.get("DEVSTACK_DIR") or tempfile.mkdtemp(prefix="wxdev-"))
    fhrs = list(range(0, int(os.environ.get("DEVSTACK_FHRS", "12")) + 1))
    print(f"building fake bucket in {work} ...", flush=True)
    fakenoaa.make_bucket(work / "bucket" / "hrrr", "hrrr", "hrrr.{date}/conus/hrrr.t{cycle:02d}z.{product}f{fhr:02d}.grib2", RUN, fhrs, LEVELS)
    base, _srv = fakenoaa.serve(work / "bucket")
    client = Path(__file__).resolve().parents[2] / "client"
    os.environ.update({
        "WX_STORE": "local", "WX_LOCAL_PATH": str(work / "icechunk"), "WX_MODELS": "hrrr", "WX_HRRR_BASE_URL": f"{base}/hrrr",
        "WX_LEVELS": ",".join(str(int(x)) for x in LEVELS), "WX_MAX_FHR": str(fhrs[-1]), "WX_DERIVE_STRIDE": "1", "WX_MAP_RES": "0.25",
        "WX_CLIENT_DIR": str(client),
    })
    from wxserver import ingest

    asyncio.run(ingest.main_async(["once", "--run", RUN.strftime("%Y%m%d%H")]))
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    print(f"serving http://127.0.0.1:{port}", flush=True)
    uvicorn.run("wxserver.api:app", host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    sys.exit(main())
