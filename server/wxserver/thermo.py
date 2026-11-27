"""Thermodynamics. Simple conversions are plain numpy; parcel work (CAPE/CIN, LCL, LFC, EL, LI,
DCAPE, parcel traces) is done with nzthermo, vectorised over N profiles at once.

Pressure-level model output includes levels that lie below the terrain. Profiles are therefore
rebuilt as ``[surface, levels above ground..., NaN padding]`` before being handed to nzthermo,
which ignores trailing NaNs.
"""

from __future__ import annotations

import warnings

import numpy as np
import nzthermo as nzt

C2K = 273.15


def esat(t_c):
    return 6.112 * np.exp(17.67 * t_c / (t_c + 243.5))


def td_from_e(e_hpa):
    a = np.log(np.maximum(e_hpa, 1e-6) / 6.112)
    return 243.5 * a / (17.67 - a)


def td_from_rh(t_c, rh_pct):
    return td_from_e(esat(t_c) * np.clip(rh_pct, 0.5, 100.0) / 100.0)


def td_from_q(p_hpa, q):
    w = q / np.maximum(1.0 - q, 1e-9)
    return td_from_e(w * p_hpa / (0.622 + w))


def rh_from_td(t_c, td_c):
    return np.clip(100.0 * esat(td_c) / esat(t_c), 0.0, 100.0)


def surface_profiles(levels_hpa, t_c, td_c, psfc_hpa, t2m_c, td2m_c):
    """Build (N, Z+1) pressure [Pa], temperature [K] and dewpoint [K] profiles that start at the
    surface and contain only levels above ground, NaN padded at the top.

    levels_hpa: (Z,) descending; t_c, td_c: (N, Z); psfc_hpa, t2m_c, td2m_c: (N,)
    """
    lv = np.asarray(levels_hpa, dtype=np.float64)
    n, z = t_c.shape
    above = lv[None, :] < (psfc_hpa[:, None] - 1.0)  # (N, Z), a suffix per row
    count = above.sum(1)
    k = z - count  # levels below ground per row
    cols = np.arange(z)[None, :] + k[:, None]
    valid = cols < z
    cols = np.minimum(cols, z - 1)
    rows = np.arange(n)[:, None]
    P = np.where(valid, np.broadcast_to(lv, (n, z))[rows, cols], np.nan)
    T = np.where(valid, t_c[rows, cols], np.nan)
    D = np.where(valid, td_c[rows, cols], np.nan)
    P = np.concatenate([psfc_hpa[:, None], P], axis=1) * 100.0
    T = np.concatenate([t2m_c[:, None], T], axis=1) + C2K
    D = np.concatenate([np.minimum(td2m_c, t2m_c)[:, None], np.minimum(D, T[:, 1:] - C2K)], axis=1) + C2K
    return P.astype(np.float64), T.astype(np.float64), D.astype(np.float64)


def interp_logp(P, X, p_target):
    """Value of X at pressure p_target [Pa] for each row of (N, Z) arrays (NaN-padded)."""
    lp = np.log(P)
    lt = np.log(p_target)
    below = P >= p_target
    j = np.clip(below.sum(1) - 1, 0, P.shape[1] - 2)
    r = np.arange(P.shape[0])
    p0, p1 = lp[r, j], lp[r, j + 1]
    x0, x1 = X[r, j], X[r, j + 1]
    f = (lt - p0) / (p1 - p0)
    out = x0 + (x1 - x0) * f
    out[(P[:, 0] < p_target) | ~np.isfinite(out)] = np.nan
    return out


def _quiet():
    ctx = warnings.catch_warnings()
    ctx.__enter__()
    warnings.simplefilter("ignore")
    np.seterr(all="ignore")
    return ctx


def _by_depth(P, T, D, fn):
    """Call ``fn(P, T, D) -> dict[str, (n,) array]`` once per group of profiles that share the
    same number of levels above ground, on NaN-free trimmed arrays, and scatter the results."""
    count = np.isfinite(P).sum(1)
    out: dict[str, np.ndarray] = {}
    for c in np.unique(count):
        rows = np.nonzero(count == c)[0]
        if c < 3:
            continue
        res = fn(P[rows, :c], T[rows, :c], D[rows, :c])
        for k, v in res.items():
            if k not in out:
                out[k] = np.full(P.shape[0], np.nan, np.float32)
            out[k][rows] = v
    return out


def _grid_fn(P, T, D):
    sbcape, sbcin = nzt.surface_based_cape_cin(P, T, D)
    mucape, _ = nzt.most_unstable_cape_cin(P, T, D)
    mlcape, _ = nzt.mixed_layer_cape_cin(P, T, D)
    lcl_p, _ = nzt.lcl(P[:, 0], T[:, 0], D[:, 0])
    prof = np.asarray(nzt.parcel_profile(P, T[:, 0], D[:, 0]))
    li = interp_logp(P, T, 50000.0) - interp_logp(P, prof, 50000.0)
    return {
        "sbcape": np.asarray(sbcape), "sbcin": np.asarray(sbcin), "mucape": np.asarray(mucape),
        "mlcape": np.asarray(mlcape), "lcl": np.asarray(lcl_p) / 100.0, "li": li,
    }


def gridded_indices(P, T, D) -> dict[str, np.ndarray]:
    """Map fields for N profiles: SBCAPE, SBCIN, MUCAPE, MLCAPE, LCL pressure, lifted index."""
    ctx = _quiet()
    try:
        return _by_depth(P, T, D, _grid_fn)
    finally:
        ctx.__exit__(None, None, None)


def _parcel_field(x):
    return np.asarray(getattr(x, "pressure", x), dtype=np.float64)


def _sounding_fn(P, T, D):
    pp, tt, dd, prof = (np.asarray(a) for a in nzt.parcel_profile_with_lcl(P, T, D))
    sbcape, sbcin = nzt.surface_based_cape_cin(P, T, D)
    mucape, mucin = nzt.most_unstable_cape_cin(P, T, D)
    mlcape, mlcin = nzt.mixed_layer_cape_cin(P, T, D)
    lcl_p, lcl_t = nzt.lcl(P[:, 0], T[:, 0], D[:, 0])
    lfc = _parcel_field(nzt.lfc(pp, tt, dd, prof, which="bottom"))
    el = _parcel_field(nzt.el(pp, tt, dd, prof, which="top"))
    try:
        dcape = np.asarray(nzt.downdraft_cape(P, T, D))
    except Exception:  # pragma: no cover - depends on profile depth
        dcape = np.full(P.shape[0], np.nan)
    li = interp_logp(P, T, 50000.0) - interp_logp(pp, prof, 50000.0)
    res = {
        "sbcape": sbcape, "sbcin": sbcin, "mucape": mucape, "mucin": mucin, "mlcape": mlcape,
        "mlcin": mlcin, "lcl_p": np.asarray(lcl_p) / 100, "lcl_t": np.asarray(lcl_t) - C2K,
        "lfc_p": lfc / 100, "el_p": el / 100, "li": li, "dcape": dcape,
    }
    res = {k: np.asarray(v, np.float64) for k, v in res.items()}
    # parcel traces: keep as object rows, packed into a float array per row via an index map
    res["_trace"] = (pp, prof)
    return res


def sounding_parcels(P, T, D) -> list[dict]:
    """Full surface-parcel analysis for a handful of profiles (one per forecast hour)."""
    ctx = _quiet()
    traces: dict[int, list] = {}
    try:
        count = np.isfinite(P).sum(1)
        scal: dict[str, np.ndarray] = {}
        for c in np.unique(count):
            rows = np.nonzero(count == c)[0]
            if c < 3:
                continue
            r = _sounding_fn(P[rows, :c], T[rows, :c], D[rows, :c])
            pp, prof = r.pop("_trace")
            for i, row in enumerate(rows):
                ok = np.isfinite(pp[i]) & np.isfinite(prof[i])
                traces[int(row)] = [[round(float(a) / 100, 1), round(float(b) - C2K, 2)] for a, b in zip(pp[i][ok], prof[i][ok])]
            for k, v in r.items():
                scal.setdefault(k, np.full(P.shape[0], np.nan))[rows] = v
    finally:
        ctx.__exit__(None, None, None)

    def f(k, i, nd=1):
        x = float(scal[k][i]) if k in scal else float("nan")
        return None if not np.isfinite(x) else round(x, nd)

    out = []
    for i in range(P.shape[0]):
        has = (f("sbcape", i, 0) or 0) > 0
        out.append({
            "trace": traces.get(i, []),
            "sbcape": f("sbcape", i, 0), "sbcin": f("sbcin", i, 0),
            "mucape": f("mucape", i, 0), "mucin": f("mucin", i, 0),
            "mlcape": f("mlcape", i, 0), "mlcin": f("mlcin", i, 0),
            "lcl_p": f("lcl_p", i), "lcl_t": f("lcl_t", i),
            "lfc_p": f("lfc_p", i) if has else None, "el_p": f("el_p", i) if has else None,
            "li": f("li", i), "dcape": f("dcape", i, 0),
        })
    return out
