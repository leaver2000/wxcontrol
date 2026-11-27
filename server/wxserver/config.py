from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All settings come from environment variables prefixed with WX_ (see .env.example)."""

    model_config = SettingsConfigDict(env_prefix="WX_", env_file=".env", extra="ignore")

    # --- storage (Icechunk) ---------------------------------------------------------------
    store: str = "local"  # "s3" (MinIO or AWS) or "local"
    local_path: str = "./data/icechunk"
    s3_bucket: str = "wxdata"
    s3_prefix: str = "icechunk"
    s3_endpoint: str | None = None  # e.g. http://db:9000 for MinIO
    s3_region: str = "us-east-1"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_allow_http: bool = True
    # Icechunk commits rely on S3 conditional writes (If-Match / If-None-Match). Leave on for AWS S3
    # and SeaweedFS; turn off only for an S3 server without them (safe with the single ingest writer).
    s3_conditional_writes: bool = True

    # --- sources ----------------------------------------------------------------------------
    models: str = "hrrr,rrfs"
    hrrr_base_url: str = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com"
    rrfs_base_url: str = "https://noaa-rrfs-ops-pds.s3.amazonaws.com"
    # Path templates relative to the base URL. Fields: date (YYYYMMDD), cycle (int), fhr (int), product.
    hrrr_path: str = "hrrr.{date}/conus/hrrr.t{cycle:02d}z.{product}f{fhr:02d}.grib2"
    rrfs_path: str = "rrfs.{date}/{cycle:02d}/rrfs.t{cycle:02d}z.{product}.3km.f{fhr:03d}.conus.grib2"
    http_concurrency: int = 8
    http_timeout: float = 120.0
    merge_gap_bytes: int = 262_144  # merge byte ranges separated by less than this

    # --- ingest -----------------------------------------------------------------------------
    levels: str = "1000-100:25"  # hPa; "a-b:step" or comma list
    max_fhr: int = 18  # cap on forecast hours ingested per run (model limits also apply)
    cycles: str = "synoptic"  # "synoptic" (00/06/12/18) or "all"
    stride: int = 1  # spatial thinning of the native grid at ingest (1 = native 3 km)
    derive_stride: int = 2  # thinning for gridded nzthermo fields (CAPE etc.)
    retain_runs: int = 2
    poll_seconds: int = 300
    lookback_hours: int = 12

    # --- api --------------------------------------------------------------------------------
    cors_origins: str = "*"
    client_dir: str | None = None  # serve a built client from "/" (dev); nginx does this in compose
    map_res: float = 0.1  # default map resample resolution, degrees
    max_map_cells: int = 1_500_000

    def level_list(self) -> list[float]:
        s = self.levels.strip()
        if "-" in s and ":" in s:
            rng, step = s.split(":")
            a, b = (float(x) for x in rng.split("-"))
            st = float(step)
            n = int(round(abs(a - b) / st)) + 1
            sign = -1 if b < a else 1
            return [a + sign * i * st for i in range(n)]
        return sorted((float(x) for x in s.split(",") if x.strip()), reverse=True)

    def model_list(self) -> list[str]:
        return [m.strip().lower() for m in self.models.split(",") if m.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
