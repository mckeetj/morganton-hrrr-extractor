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



def _common_wind_profile(
    fields: list[Field],
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """Return common pressure-level height/u/v arrays with vertical dim first.

    This provides a robust fallback for bulk shear when cfgrib does not expose
    HRRR VUCSH/VVCSH layer coordinates consistently.
    """
    u_by_level = {
        float(field.level): field
        for field in fields
        if _name(field) in {"u", "ugrd"}
        and "isobaric" in field.type_of_level.lower()
        and field.level is not None
    }
    v_by_level = {
        float(field.level): field
        for field in fields
        if _name(field) in {"v", "vgrd"}
        and "isobaric" in field.type_of_level.lower()
        and field.level is not None
    }
    z_by_level = {
        float(field.level): field
        for field in fields
        if _name(field) in {"gh", "hgt"}
        and "isobaric" in field.type_of_level.lower()
        and field.level is not None
    }
    levels = sorted(
        set(u_by_level).intersection(v_by_level).intersection(z_by_level),
        reverse=True,
    )
    if len(levels) < 2:
        return None
    shape = u_by_level[levels[0]].values.shape
    levels = [
        level
        for level in levels
        if u_by_level[level].values.shape == shape
        and v_by_level[level].values.shape == shape
        and z_by_level[level].values.shape == shape
    ]
    if len(levels) < 2:
        return None
    heights = np.stack([np.asarray(z_by_level[level].values, dtype=float) for level in levels])
    u_values = np.stack([np.asarray(u_by_level[level].values, dtype=float) for level in levels])
    v_values = np.stack([np.asarray(v_by_level[level].values, dtype=float) for level in levels])
    return heights, u_values, v_values

def _layer_depth_m(field: Field) -> float | None:
    # cfgrib can combine several heightAboveGroundLayer messages into one
    # variable and expose the individual layer tops through the vertical
    # coordinate (Field.level). The GRIB topLevel/bottomLevel attributes can
    # then be inherited from the first message in that group and are not
    # reliable for each split Field. For the HRRR 0-based layers used here,
    # prefer the decoded coordinate value.
    if (
        "heightabovegroundlayer" in field.type_of_level.lower()
        and field.level is not None
        and field.level in {1000.0, 3000.0, 6000.0}
    ):
        return float(field.level)
    if field.top_level is not None and field.bottom_level is not None:
        depth = abs(field.top_level - field.bottom_level)
        if depth > 0:
            return depth
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



def _log_pressure_interpolate(
    lower_pressure_hpa: float,
    lower_values: np.ndarray,
    upper_pressure_hpa: float,
    upper_values: np.ndarray,
    target_pressure_hpa: float,
) -> np.ndarray:
    """Interpolate a field linearly in log-pressure coordinates."""
    if not (
        lower_pressure_hpa > target_pressure_hpa > upper_pressure_hpa
    ):
        raise ValueError("target pressure must lie between bounding pressure levels")
    weight = (
        np.log(target_pressure_hpa) - np.log(lower_pressure_hpa)
    ) / (
        np.log(upper_pressure_hpa) - np.log(lower_pressure_hpa)
    )
    return (
        np.asarray(lower_values, dtype=float)
        + weight
        * (
            np.asarray(upper_values, dtype=float)
            - np.asarray(lower_values, dtype=float)
        )
    )


def _mwpi_candidate(
    cape_jkg: np.ndarray,
    t850_k: np.ndarray,
    td850_k: np.ndarray,
    z850_m: np.ndarray,
    t700_k: np.ndarray,
    td700_k: np.ndarray,
    z700_m: np.ndarray,
    t500_k: np.ndarray,
    td500_k: np.ndarray,
    z500_m: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Return Pryor-style MWPI and a provisional 0-100 scaled candidate.

    The published 2015 MWPI formulation uses surface-based CAPE plus the
    850-to-670-hPa lapse rate and dewpoint-depression difference. HRRR's
    filtered 2D pressure levels do not include 670 hPa, so the 670-hPa
    temperature, dewpoint, and geopotential height are interpolated between
    the available 700- and 500-hPa fields in log-pressure coordinates.

    The 0-100 value is only a candidate integration field for local validation:
    it is 20 times the dimensionless MWPI, clipped to 0-100. It is not a
    standalone thunderstorm forecast and must not become operationally
    controlling until validated against Morganton/Burke events.
    """
    t670 = _log_pressure_interpolate(700.0, t700_k, 500.0, t500_k, 670.0)
    td670 = _log_pressure_interpolate(700.0, td700_k, 500.0, td500_k, 670.0)
    z670 = _log_pressure_interpolate(700.0, z700_m, 500.0, z500_m, 670.0)

    depth_km = (np.asarray(z670, dtype=float) - np.asarray(z850_m, dtype=float)) / 1000.0
    with np.errstate(divide="ignore", invalid="ignore"):
        lapse_rate = (
            np.asarray(t850_k, dtype=float) - np.asarray(t670, dtype=float)
        ) / depth_km

    dd850 = np.asarray(t850_k, dtype=float) - np.asarray(td850_k, dtype=float)
    dd670 = np.asarray(t670, dtype=float) - np.asarray(td670, dtype=float)

    mwpi = (
        np.asarray(cape_jkg, dtype=float) / 1000.0
        + lapse_rate / 5.0
        + (dd850 - dd670) / 5.0
    )
    mwpi = np.where(depth_km > 0.0, mwpi, np.nan)
    candidate_0_100 = np.clip(mwpi * 20.0, 0.0, 100.0)
    return mwpi, candidate_0_100


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

    # Deep-layer shear proxy.  The filtered CONUS HRRR 2D file provides
    # 10-m winds plus 500-mb U/V, but not enough geopotential-height levels
    # above 500 mb to interpolate a defensible exact 6-km-AGL wind everywhere
    # in Burke County.  Use the direct vector difference to 500 mb instead and
    # label it explicitly as a proxy rather than mislabeling it as 0-6 km shear.
    u10 = _surface_field(surface_fields, {"10u", "u10", "u", "ugrd"}, 10)
    v10 = _surface_field(surface_fields, {"10v", "v10", "v", "vgrd"}, 10)
    u500 = _pressure_field(pressure_fields, {"u", "ugrd"}, 500)
    v500 = _pressure_field(pressure_fields, {"v", "vgrd"}, 500)
    if all(field is not None for field in (u10, v10, u500, v500)):
        shear_ms = np.hypot(
            np.asarray(u500.values, dtype=float) - np.asarray(u10.values, dtype=float),  # type: ignore[union-attr]
            np.asarray(v500.values, dtype=float) - np.asarray(v10.values, dtype=float),  # type: ignore[union-attr]
        )
        output["bulk_shear_sfc_500mb"] = _metric(
            shear_ms * 1.943844,
            u10,  # type: ignore[arg-type]
            "kt",
            (
                "HRRR-derived vector wind difference from 10 m AGL to 500 mb; "
                "deep-layer shear proxy, not an exact 0-6 km AGL calculation"
            ),
            bounds,
        )


    # Experimental MWPI candidate for eventual Operational Readiness Hub
    # integration. This is environmental downburst potential conditional on
    # convection; it is deliberately not treated as an operational verdict.
    cape_surface = _find(surface_fields, {"cape"}, type_contains="surface")
    t850 = _pressure_field(pressure_fields, {"t", "tmp"}, 850)
    td850 = _pressure_field(pressure_fields, {"dpt", "td"}, 850)
    z850 = _pressure_field(pressure_fields, {"gh", "hgt"}, 850)
    t700_mwpi = _pressure_field(pressure_fields, {"t", "tmp"}, 700)
    td700_mwpi = _pressure_field(pressure_fields, {"dpt", "td"}, 700)
    z700_mwpi = _pressure_field(pressure_fields, {"gh", "hgt"}, 700)
    t500_mwpi = _pressure_field(pressure_fields, {"t", "tmp"}, 500)
    td500_mwpi = _pressure_field(pressure_fields, {"dpt", "td"}, 500)
    z500_mwpi = _pressure_field(pressure_fields, {"gh", "hgt"}, 500)

    mwpi_required = (
        cape_surface,
        t850,
        td850,
        z850,
        t700_mwpi,
        td700_mwpi,
        z700_mwpi,
        t500_mwpi,
        td500_mwpi,
        z500_mwpi,
    )
    if all(field is not None for field in mwpi_required):
        mwpi, candidate = _mwpi_candidate(
            cape_surface.values,  # type: ignore[union-attr]
            t850.values,  # type: ignore[union-attr]
            td850.values,  # type: ignore[union-attr]
            z850.values,  # type: ignore[union-attr]
            t700_mwpi.values,  # type: ignore[union-attr]
            td700_mwpi.values,  # type: ignore[union-attr]
            z700_mwpi.values,  # type: ignore[union-attr]
            t500_mwpi.values,  # type: ignore[union-attr]
            td500_mwpi.values,  # type: ignore[union-attr]
            z500_mwpi.values,  # type: ignore[union-attr]
        )
        output["mwpi_environment"] = _metric(
            mwpi,
            cape_surface,  # type: ignore[arg-type]
            "dimensionless",
            (
                "Pryor-style 2015 MWPI environmental potential using surface CAPE "
                "and 850-to-670-hPa lapse/dryness terms; 670-hPa fields are "
                "log-pressure interpolated from HRRR 700/500-hPa fields. "
                "Conditional on convection and not a standalone forecast."
            ),
            bounds,
        )
        output["downburst_index_candidate_0_100"] = _metric(
            candidate,
            cape_surface,  # type: ignore[arg-type]
            "0-100 candidate",
            (
                "Experimental Morganton integration candidate: 20 x MWPI, clipped "
                "to 0-100. Not locally calibrated and not approved to control the "
                "Operational Readiness Hub until validated against Morganton/Burke events."
            ),
            bounds,
        )


    return output
