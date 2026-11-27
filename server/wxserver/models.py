"""Model registry: where each model's GRIB2 lives and which messages to pull from it.

Selection is driven by the wgrib2-style .idx sidecar files that NOAA publishes next to every
GRIB2 file (``n:offset:d=YYYYMMDDHH:VAR:LEVEL:FCST:``), so only the needed messages are
downloaded with HTTP byte-range requests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .config import Settings


@dataclass(frozen=True)
class SfcVar:
    """A surface (2-D) field: one of several GRIB short names at a level string."""

    key: str
    names: tuple[str, ...]
    level: str  # exact .idx level text, or a prefix ending in '*'
    required: bool = False


# Pressure-level fields. Td is preferred directly (DPT); otherwise derived from RH or SPFH.
LEVEL_VARS: dict[str, tuple[str, ...]] = {
    "z": ("HGT",),
    "t": ("TMP",),
    "td": ("DPT",),
    "rh": ("RH",),
    "q": ("SPFH",),
    "u": ("UGRD",),
    "v": ("VGRD",),
}

SFC_VARS: tuple[SfcVar, ...] = (
    SfcVar("t2m", ("TMP",), "2 m above ground", True),
    SfcVar("td2m", ("DPT",), "2 m above ground"),
    SfcVar("rh2m", ("RH",), "2 m above ground"),
    SfcVar("u10", ("UGRD",), "10 m above ground", True),
    SfcVar("v10", ("VGRD",), "10 m above ground", True),
    SfcVar("psfc", ("PRES",), "surface", True),
    SfcVar("hgt", ("HGT",), "surface"),
    SfcVar("mslp", ("MSLMA", "PRMSL", "MSLET"), "mean sea level"),
    SfcVar("gust", ("GUST",), "surface"),
    SfcVar("prate", ("PRATE",), "surface"),
    SfcVar("csnow", ("CSNOW",), "surface"),
    SfcVar("tcc", ("TCDC",), "entire atmosphere*"),
    SfcVar("refc", ("REFC",), "entire atmosphere*"),
)


@dataclass(frozen=True)
class ModelSpec:
    id: str
    name: str
    base_url: str
    path: str
    level_product: str
    sfc_product: str
    max_fhr_synoptic: int
    max_fhr_other: int
    synoptic_cycles: tuple[int, ...] = (0, 6, 12, 18)
    level_vars: dict[str, tuple[str, ...]] = field(default_factory=lambda: dict(LEVEL_VARS))
    sfc_vars: tuple[SfcVar, ...] = SFC_VARS

    def url(self, run: datetime, fhr: int, product: str) -> str:
        rel = self.path.format(date=run.strftime("%Y%m%d"), cycle=run.hour, fhr=fhr, product=product)
        return f"{self.base_url.rstrip('/')}/{rel}"

    def max_fhr(self, run: datetime) -> int:
        return self.max_fhr_synoptic if run.hour in self.synoptic_cycles else self.max_fhr_other


def registry(s: Settings) -> dict[str, ModelSpec]:
    specs = {
        "hrrr": ModelSpec(
            id="hrrr",
            name="HRRR 3 km",
            base_url=s.hrrr_base_url,
            path=s.hrrr_path,
            level_product="wrfprs",
            sfc_product="wrfsfc",
            max_fhr_synoptic=48,
            max_fhr_other=18,
        ),
        "rrfs": ModelSpec(
            id="rrfs",
            name="RRFS 3 km",
            base_url=s.rrfs_base_url,
            path=s.rrfs_path,
            level_product="prslev",
            sfc_product="2dfld",
            max_fhr_synoptic=84,
            max_fhr_other=18,
        ),
    }
    return {k: v for k, v in specs.items() if k in s.model_list()}
