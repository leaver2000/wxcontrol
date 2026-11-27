"""The compose stack stores Icechunk in MinIO over the S3 API. This exercises the same code path
(path-style S3 over plain HTTP with static keys) against moto's S3 server."""

import numpy as np
import pytest

moto_server = pytest.importorskip("moto.server")


@pytest.fixture(scope="module")
def s3_endpoint():
    srv = moto_server.ThreadedMotoServer(ip_address="127.0.0.1", port=0)
    srv.start()
    host, port = srv.get_host_and_port()
    url = f"http://{host}:{port}"
    import boto3

    boto3.client("s3", endpoint_url=url, aws_access_key_id="k", aws_secret_access_key="s", region_name="us-east-1").create_bucket(Bucket="wxdata")
    yield url
    srv.stop()


@pytest.mark.parametrize("conditional", [True, False])
def test_store_roundtrip_over_s3(s3_endpoint, conditional):
    from datetime import datetime, timezone

    from wxserver.config import Settings
    from wxserver.decode import Grid
    from wxserver.store import WxStore

    s = Settings(store="s3", s3_bucket="wxdata", s3_prefix=f"t{int(conditional)}", s3_conditional_writes=conditional, s3_endpoint=s3_endpoint, s3_access_key="k", s3_secret_key="s", s3_allow_http=True)
    st = WxStore(s)
    lat, lon = np.meshgrid(np.linspace(20, 50, 30), np.linspace(-120, -70, 40), indexing="ij")
    st.ensure_model("hrrr", "HRRR", Grid(lat.astype("f4"), lon.astype("f4"), "abc", "lambert"), 1, None)
    run = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
    lv = {k: np.full((3, 30, 40), 1.5, "f4") for k in ("z", "t", "td", "u", "v")}
    sf = {"t2m": np.full((30, 40), 7.0, "f4")}
    st.write_step("hrrr", run, [0, 1], 0, [1000.0, 850.0, 500.0], lv, sf, 2)
    st2 = WxStore(s)  # fresh process view
    cat = st2.catalog()
    assert cat[0]["runs"][0]["fhrs"] == [0]
    assert float(st2.reader()["hrrr/runs/2026112712/s_t2m"][0, 5, 5]) == 7.0
    assert len(list(st2.repo.ancestry(branch="main"))) >= 3  # root, grid, f000 commits
