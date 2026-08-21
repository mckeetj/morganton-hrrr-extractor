from __future__ import annotations

from collections.abc import Iterable

import numpy as np

from .decode import Field
from .diagnostics import bulk_shear_ms, interpolate_at_height, layer_lapse_rate_k_per_km, midlevel_rh, summarize


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


def _pressure_cube(fields: Iterable[Field], names: set[str]) -> tuple[np.ndarray, np.ndarray] | None:
    selected = [
        field for field in fields
        if _name(field) in names
        and "isobaric" in field.type_of_level.lower()
        and field.level is not None
    ]
    if not selected:
        return None
    selected.sort(key=lambda field: float(field.level), reverse=True)
    shape = selected[0].values.shape
    selected = [field for field in selected if field.values.shape == shape]
    return (
        np.asarray([float(field.level) for field in selected]),
        np.stack([np.asarray(field.values, dtype=float) for field in selected]),
    )


def _at_pressure(levels: np.ndarray, cube: np.ndarray, pressure_hpa: float) -> np.ndarray | None:
    matches = np.where(np.isclose(levels, pressure_hpa))[0]
    return cube[matches[0]] if matches.size else None


def _surface_field(fields: list[Field], names: set[str], level: float) -> Field | None:
    return _find(fields, names, type_contains="heightAboveGround", level=level)


def _terrain_height(fields: list[Field]) -> np.ndarray | None:
    field = _find(fields, {"orog", "gh", "hgt"}, type_contains="surface")
    return np.asarray(field.values, dtype=float) if field else None


def _metric(values: np.ndarray, units: str, method: str) -> dict[str, object]:
    return {"units": units, "method": method, "summary": summarize(values)}


def dewpoint_from_rh_k(temperature_k: np.ndarray, rh_percent: np.ndarray) -> np.ndarray:
    """Magnus-formula dewpoint for diagnostic dryness calculations."""
    temperature_c = np.asarray(temperature_k, dtype=float) - 273.15
    rh = np.clip(np.asarray(rh_percent, dtype=float), 0.01, 100.0)
    alpha = np.log(rh / 100.0) + (17.625 * temperature_c) / (243.04 + temperature_c)
    dewpoint_c = 243.04 * alpha / (17.625 - alpha)
    return dewpoint_c + 273.15


def _dcape(levels_hpa: np.ndarray, temperature_k: np.ndarray, rh_percent: np.ndarray) -> np.ndarray:
    try:
        import metpy.calc as mpcalc
        from metpy.units import units
    except ImportError:
        return np.full(temperature_k.shape[1:], np.nan)

    result = np.full(temperature_k.shape[1:], np.nan)
    dewpoint_k = dewpoint_from_rh_k(temperature_k, rh_percent)
    for index in np.ndindex(result.shape):
        column = (slice(None),) + index
        p = levels_hpa
        t = temperature_k[column]
        td = dewpoint_k[column]
        valid = np.isfinite(p) & np.isfinite(t) & np.isfinite(td)
        # MetPy needs surface through at least 500 hPa and a monotonic profile.
        if valid.sum() < 6 or np.nanmax(p[valid]) < 850 or np.nanmin(p[valid]) > 500:
            continue
        try:
            dcape, _, _ = mpcalc.downdraft_cape(
                p[valid] * units.hPa,
                t[valid] * units.kelvin,
                td[valid] * units.kelvin,
            )
            result[index] = float(dcape.to("joule / kilogram").magnitude)
        except (ValueError, IndexError):
            continue
    return result


def derive_diagnostics(surface_fields: list[Field], pressure_fields: list[Field]) -> dict[str, object]:
    output: dict[str, object] = {}
    t_data = _pressure_cube(pressure_fields, {"t", "tmp"})
    rh_data = _pressure_cube(pressure_fields, {"r", "rh"})
    z_data = _pressure_cube(pressure_fields, {"gh", "hgt"})
    u_data = _pressure_cube(pressure_fields, {"u", "ugrd"})
    v_data = _pressure_cube(pressure_fields, {"v", "vgrd"})

    if t_data and z_data:
        t_levels, temperature = t_data
        z_levels, height = z_data
        if np.array_equal(t_levels, z_levels):
            t700, t500 = _at_pressure(t_levels, temperature, 700), _at_pressure(t_levels, temperature, 500)
            z700, z500 = _at_pressure(z_levels, height, 700), _at_pressure(z_levels, height, 500)
            if all(value is not None for value in (t700, t500, z700, z500)):
                lapse = layer_lapse_rate_k_per_km(t700, t500, z700, z500)  # type: ignore[arg-type]
                output["lapse_rate_700_500mb"] = _metric(
                    lapse, "K/km", "HRRR-derived from pressure-level temperature and geopotential height"
                )

            terrain = _terrain_height(surface_fields)
            t2m = _surface_field(surface_fields, {"t", "tmp"}, 2)
            if terrain is not None and t2m is not None:
                t3km = interpolate_at_height(height, temperature, terrain + 3000.0)
                output["lapse_rate_0_3km_agl"] = _metric(
                    (np.asarray(t2m.values) - t3km) / 3.0,
                    "K/km",
                    "HRRR-derived using 2-m temperature and pressure-level interpolation to 3 km AGL",
                )

    if rh_data:
        rh_levels, rh = rh_data
        layer_mask = (rh_levels <= 700) & (rh_levels >= 500)
        if layer_mask.any():
            mean_rh, min_rh = midlevel_rh(rh[layer_mask])
            output["mean_rh_700_500mb"] = _metric(
                mean_rh, "percent", "HRRR-derived arithmetic mean of pressure-level RH"
            )
            output["minimum_rh_700_500mb"] = _metric(
                min_rh, "percent", "HRRR-derived minimum pressure-level RH; midlevel dry-air proxy"
            )
        if t_data and np.array_equal(t_data[0], rh_levels):
            t700 = _at_pressure(t_data[0], t_data[1], 700)
            rh700 = _at_pressure(rh_levels, rh, 700)
            if t700 is not None and rh700 is not None:
                depression = t700 - dewpoint_from_rh_k(t700, rh700)
                output["dewpoint_depression_700mb"] = _metric(
                    depression, "K", "HRRR-derived from 700-mb temperature and RH"
                )
            output["dcape"] = _metric(
                _dcape(t_data[0], t_data[1], rh),
                "J/kg",
                "HRRR-derived with MetPy downdraft_cape; saturated descent from minimum theta-e in 700-500 mb",
            )

    if z_data and u_data and v_data:
        z_levels, height = z_data
        if np.array_equal(z_levels, u_data[0]) and np.array_equal(z_levels, v_data[0]):
            terrain = _terrain_height(surface_fields)
            u10 = _surface_field(surface_fields, {"u", "ugrd"}, 10)
            v10 = _surface_field(surface_fields, {"v", "vgrd"}, 10)
            if terrain is not None and u10 is not None and v10 is not None:
                u6km = interpolate_at_height(height, u_data[1], terrain + 6000.0)
                v6km = interpolate_at_height(height, v_data[1], terrain + 6000.0)
                shear = bulk_shear_ms(u10.values, v10.values, u6km, v6km)
                output["bulk_shear_0_6km"] = _metric(
                    shear * 1.943844, "kt", "HRRR-derived from 10-m wind and interpolation to 6 km AGL"
                )

    return output

