from __future__ import annotations

import numpy as np


def summarize(values: np.ndarray) -> dict[str, float | int | None]:
    data = np.asarray(values, dtype=float)
    finite = data[np.isfinite(data)]
    if not finite.size:
        return {"count": 0, "min": None, "median": None, "p90": None, "max": None}
    return {
        "count": int(finite.size),
        "min": float(np.min(finite)),
        "median": float(np.median(finite)),
        "p90": float(np.percentile(finite, 90)),
        "max": float(np.max(finite)),
    }


def layer_lapse_rate_k_per_km(
    lower_temperature_k: np.ndarray,
    upper_temperature_k: np.ndarray,
    lower_height_m: np.ndarray,
    upper_height_m: np.ndarray,
) -> np.ndarray:
    depth = np.asarray(upper_height_m) - np.asarray(lower_height_m)
    temperature_drop = np.asarray(lower_temperature_k) - np.asarray(upper_temperature_k)
    with np.errstate(divide="ignore", invalid="ignore"):
        result = 1000.0 * temperature_drop / depth
    return np.where(depth > 0, result, np.nan)


def interpolate_at_height(
    heights_m: np.ndarray,
    values: np.ndarray,
    target_height_m: np.ndarray,
) -> np.ndarray:
    """Linearly interpolate vertical columns; vertical dimension must be first."""
    z = np.asarray(heights_m, dtype=float)
    v = np.asarray(values, dtype=float)
    target = np.asarray(target_height_m, dtype=float)
    if z.shape != v.shape or z.ndim < 1 or z.shape[1:] != target.shape:
        raise ValueError("vertical arrays and target grid have incompatible shapes")
    out = np.full(target.shape, np.nan, dtype=float)
    for index in np.ndindex(target.shape):
        column = (slice(None),) + index
        z_col = z[column]
        v_col = v[column]
        valid = np.isfinite(z_col) & np.isfinite(v_col)
        if valid.sum() < 2:
            continue
        z_valid = z_col[valid]
        v_valid = v_col[valid]
        order = np.argsort(z_valid)
        if z_valid[order][0] <= target[index] <= z_valid[order][-1]:
            out[index] = np.interp(target[index], z_valid[order], v_valid[order])
    return out


def bulk_shear_ms(
    lower_u_ms: np.ndarray,
    lower_v_ms: np.ndarray,
    upper_u_ms: np.ndarray,
    upper_v_ms: np.ndarray,
) -> np.ndarray:
    du = np.asarray(upper_u_ms) - np.asarray(lower_u_ms)
    dv = np.asarray(upper_v_ms) - np.asarray(lower_v_ms)
    return np.hypot(du, dv)


def midlevel_rh(rh_percent: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    data = np.asarray(rh_percent, dtype=float)
    if data.ndim < 1:
        raise ValueError("RH must include a vertical dimension")
    return np.nanmean(data, axis=0), np.nanmin(data, axis=0)

