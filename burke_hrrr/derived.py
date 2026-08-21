from __future__ import annotations

from collections.abc import Iterable

import numpy as np

from .config import Bounds
from .decode import Field, bounds_mask
from .diagnostics import interpolate_at_height, layer_lapse_rate_k_per_km, summarize


def _name(field: Field) -> str:
    return field.short_name.lower()


def _find(
    fields: Iterable[Field],
    names: set[str],
    *,
    type_contains: str | None = None,
    level: float | None = None,
    step_type: str | None = None,
) -> Field | None:
    for field in fields:
        if _name(field) not in names:
            continue
        if type_contains and type_contains.lower() not in field.type_of_level.lower():
            continue
        if level is not None and (field.level is None or abs(field.level - level) > 0.1):
            continue
        if step_type and step_type.lower() != field.step_type.lower():
            continue
        return field
    return None


def _pressure_field(fields: Iterable[Field], names: set[str], level: float) -> Field | None:
    return _find(fields, names, type_contains="isobaric", level=level)


def _surface_field(fields: list[Field], names: set[str], level: float) -> Field | None:
    return _find(fields, names, type_contains="heightAboveGround", level=level)


def _terrain_height(fields: list[Field]) -> Field | None:
    return _find(fields, {"orog", "gh", "hgt"}, type_contains="surface")


def _summary_for_array(
    values: np.ndarray,
    reference: Field,
    bounds: Bounds | None,
) -> dict[str, float | int | None]:
    data = np.asarray(values, dtype=float)
    if bounds is None:
        return summarize(data)
    mask = bounds_mask(reference, bounds)
    if data.shape != mask.shape:
        return summarize(np.asarray([], dtype=float))
    return summarize(data[mask])


def _metric(
    values: np.ndarray,
    reference: Field,
    units: str,
    method: str,
    bounds: Bounds | None,
) -> dict[str, object]:
    return {
        "units": units,
        "method": method,
        "summary": _summary_for_array(values, reference, bounds),
    }


def relative_humidity_from_t_td_percent(
    temperature_k: np.ndarray,
    dewpoint_k: np.ndarray,
) -> np.ndarray:
    """Return RH percent from temperature/dewpoint using the Magnus formula."""
    temperature_c = np.asarray(temperature_k, dtype=float) - 273.15
    dewpoint_c = np.asarray(dewpoint_k, dtype=float) - 273.15
    e = np.exp((17.625 * dewpoint_c) / (243.04 + dewpoint_c))
    es = np.exp((17.625 * temperature_c) / (243.04 + temperature_c))
    with np.errstate(divide="ignore", invalid="ignore"):
        rh = 100.0 * e / es
    return np.clip(rh, 0.0, 100.0)


def dewpoint_from_rh_k(temperature_k: np.ndarray, rh_percent: np.ndarray) -> np.ndarray:
    """Retained for backward compatibility/tests and diagnostic calculations."""
    temperature_c = np.asarray(temperature_k, dtype=float) - 273.15
    rh = np.clip(np.asarray(rh_percent, dtype=float), 0.01, 100.0)
    alpha = np.log(rh / 100.0) + (17.625 * temperature_c) / (243.04 + temperature_c)
    dewpoint_c = 243.04 * alpha / (17.625 - alpha)
    return dewpoint_c + 273.15


def _common_pressure_profile(
    fields: list[Field],
    value_names: set[str],
    height_names: set[str] = {"gh", "hgt"},
) -> tuple[np.ndarray, np.ndarray] | None:
    value_by_level = {
        float(field.level): field
        for field in fields
        if _name(field) in value_names
        and "isobaric" in field.type_of_level.lower()
        and field.level is not None
    }
    height_by_level = {
        float(field.level): field
        for field in fields
        if _name(field) in height_names
        and "isobaric" in field.type_of_level.lower()
        and field.level is not None
    }
    levels = sorted(set(value_by_level).intersection(height_by_level), reverse=True)
    if len(levels) < 2:
        return None
    shape = value_by_level[levels[0]].values.shape
    levels = [
        level
        for level in levels
        if value_by_level[level].values.shape == shape
        and height_by_level[level].values.shape == shape
    ]
    if len(levels) < 2:
        return None
    heights = np.stack([np.asarray(height_by_level[level].values, dtype=float) for level in levels])
    values = np.stack([np.asarray(value_by_level[level].values, dtype=float) for level in levels])
    return heights, values


def _layer_depth_m(field: Field) -> float | None:
    if field.top_level is not None and field.bottom_level is not None:
        depth = abs(field.top_level - field.bottom_level)
        if depth > 0:
            return depth
    if field.level is not None and field.level in {1000.0, 3000.0, 6000.0}:
        return float(field.level)
    return None


def _find_shear_component(fields: list[Field], name: str, depth_m: float) -> Field | None:
    candidates = [
        field
        for field in fields
        if _name(field) == name and "heightabovegroundlayer" in field.type_of_level.lower()
    ]
    for field in candidates:
        depth = _layer_depth_m(field)
        if depth is not None and abs(depth - depth_m) < 1.0:
            return field
    return None


def derive_diagnostics(
    surface_fields: list[Field],
    pressure_fields: list[Field],
    bounds: Bounds | None = None,
) -> dict[str, object]:
    """Derive only diagnostics supportable from the filtered HRRR 2D fields.

    V2 intentionally does not calculate DCAPE from the sparse 2D pressure-level
    profile. The direct HRRR MAXDVV field is carried separately as the primary
    model downdraft diagnostic.
    """
    output: dict[str, object] = {}

    # 700-500-mb lapse rate from the two explicit pressure levels.
    t700 = _pressure_field(pressure_fields, {"t", "tmp"}, 700)
    t500 = _pressure_field(pressure_fields, {"t", "tmp"}, 500)
    z700 = _pressure_field(pressure_fields, {"gh", "hgt"}, 700)
    z500 = _pressure_field(pressure_fields, {"gh", "hgt"}, 500)
    if all(field is not None for field in (t700, t500, z700, z500)):
        lapse = layer_lapse_rate_k_per_km(
            t700.values,  # type: ignore[union-attr]
            t500.values,  # type: ignore[union-attr]
            z700.values,  # type: ignore[union-attr]
            z500.values,  # type: ignore[union-attr]
        )
        output["lapse_rate_700_500mb"] = _metric(
            lapse,
            t700,  # type: ignore[arg-type]
            "K/km",
            "HRRR-derived from 700/500-mb temperature and geopotential height",
            bounds,
        )

    # 700-mb dryness diagnostics use the directly available 700-mb dewpoint.
    td700 = _pressure_field(pressure_fields, {"dpt", "td"}, 700)
    if t700 is not None and td700 is not None:
        depression = np.asarray(t700.values, dtype=float) - np.asarray(td700.values, dtype=float)
        output["dewpoint_depression_700mb"] = _metric(
            depression,
            t700,
            "K",
            "HRRR-derived 700-mb temperature minus dewpoint",
            bounds,
        )
        rh700 = relative_humidity_from_t_td_percent(t700.values, td700.values)
        output["relative_humidity_700mb"] = _metric(
            rh700,
            t700,
            "percent",
            "HRRR-derived from 700-mb temperature and dewpoint",
            bounds,
        )

    # 0-3-km lapse rate from 2-m temperature and common T/HGT pressure levels.
    terrain = _terrain_height(surface_fields)
    t2m = _surface_field(surface_fields, {"2t", "t", "tmp"}, 2)
    profile = _common_pressure_profile(pressure_fields, {"t", "tmp"})
    if terrain is not None and t2m is not None and profile is not None:
        heights, temperatures = profile
        target = np.asarray(terrain.values, dtype=float) + 3000.0
        t3km = interpolate_at_height(heights, temperatures, target)
        output["lapse_rate_0_3km_agl"] = _metric(
            (np.asarray(t2m.values, dtype=float) - t3km) / 3.0,
            t2m,
            "K/km",
            "HRRR-derived using 2-m temperature and pressure-level interpolation to 3 km AGL",
            bounds,
        )

    # Direct HRRR vertical shear components are in s^-1. Multiplying by the
    # layer depth produces the vector wind difference across the layer.
    for depth_m, label in ((1000.0, "0_1km"), (6000.0, "0_6km")):
        u_shear = _find_shear_component(surface_fields, "vucsh", depth_m)
        v_shear = _find_shear_component(surface_fields, "vvcsh", depth_m)
        if u_shear is None or v_shear is None:
            continue
        shear_ms = np.hypot(
            np.asarray(u_shear.values, dtype=float) * depth_m,
            np.asarray(v_shear.values, dtype=float) * depth_m,
        )
        output[f"bulk_shear_{label}"] = _metric(
            shear_ms * 1.943844,
            u_shear,
            "kt",
            f"HRRR-derived from VUCSH/VVCSH over the {int(depth_m / 1000)}-km layer",
            bounds,
        )

    return output
