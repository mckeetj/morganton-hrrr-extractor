from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import numpy as np

from .config import Bounds
from .decode import Field, summarize_field

MPS_TO_MPH = 2.2369362920544
M_TO_FT = 3.2808398950131
MM_TO_IN = 1.0 / 25.4


def _name(field: Field) -> str:
    return field.short_name.lower()


def _find(
    fields: Iterable[Field],
    names: set[str],
    *,
    type_contains: str | None = None,
    level: float | None = None,
    step_type: str | None = None,
    long_name_contains: str | None = None,
) -> Field | None:
    for field in fields:
        if names and _name(field) not in names:
            continue
        if type_contains and type_contains.lower() not in field.type_of_level.lower():
            continue
        if level is not None and (field.level is None or abs(field.level - level) > 0.1):
            continue
        if step_type and step_type.lower() != field.step_type.lower():
            continue
        if long_name_contains and long_name_contains.lower() not in field.long_name.lower():
            continue
        return field
    return None


def _layer_depth_m(field: Field) -> float | None:
    if field.top_level is not None and field.bottom_level is not None:
        depth = abs(field.top_level - field.bottom_level)
        if depth > 0:
            return depth
    if field.level is not None and field.level in {1000.0, 3000.0, 6000.0}:
        return float(field.level)
    return None


def _find_layer(fields: list[Field], names: set[str], depth_m: float) -> Field | None:
    candidates = [
        field
        for field in fields
        if _name(field) in names and "heightabovegroundlayer" in field.type_of_level.lower()
    ]
    for field in candidates:
        depth = _layer_depth_m(field)
        if depth is not None and abs(depth - depth_m) < 1.0:
            return field
    return None


def _convert_summary(
    summary: dict[str, float | int | None],
    *,
    factor: float = 1.0,
    offset: float = 0.0,
) -> dict[str, float | int | None]:
    converted: dict[str, float | int | None] = {}
    for key, value in summary.items():
        if key == "count" or value is None:
            converted[key] = value
        else:
            converted[key] = float(value) * factor + offset
    return converted


def _field_metric(
    field: Field,
    bounds: Bounds,
    *,
    units: str | None = None,
    factor: float = 1.0,
    offset: float = 0.0,
    source_label: str | None = None,
) -> dict[str, object]:
    summary = _convert_summary(summarize_field(field, bounds), factor=factor, offset=offset)
    return {
        "units": units or field.units,
        "source_field": source_label or field.short_name,
        "source_name": field.long_name,
        "source_type_of_level": field.type_of_level,
        "source_level": field.level,
        "source_step_type": field.step_type,
        "summary": summary,
    }


def _first_metric(
    fields: list[Field],
    bounds: Bounds,
    *,
    units: str,
    source_label: str,
) -> dict[str, object] | None:
    if not fields:
        return None
    return _field_metric(fields[0], bounds, units=units, source_label=source_label)


def _pressure_layer_field(fields: list[Field], name: str, depth_hpa: int) -> Field | None:
    # cfgrib represents pressureFromGroundLayer GRIB levels in Pa, e.g. 90 hPa
    # becomes 9000.0, as seen in the live HRRR output.
    return _find(
        fields,
        {name},
        type_contains="pressureFromGroundLayer",
        level=float(depth_hpa * 100),
    )


def build_key_diagnostics(
    surface_fields: list[Field],
    pressure_fields: list[Field],
    special_fields: dict[str, list[Field]],
    derived: dict[str, object],
    bounds: Bounds,
) -> dict[str, object]:
    """Produce a compact, stable severe-weather view for the scheduled agent."""
    output: dict[str, object] = {}

    def add_field(
        key: str,
        field: Field | None,
        *,
        units: str | None = None,
        factor: float = 1.0,
        source_label: str | None = None,
    ) -> None:
        if field is not None:
            output[key] = _field_metric(
                field,
                bounds,
                units=units,
                factor=factor,
                source_label=source_label,
            )

    add_field(
        "surface_gust_mph",
        _find(surface_fields, {"gust"}, type_contains="surface"),
        units="mph",
        factor=MPS_TO_MPH,
        source_label="GUST surface",
    )
    add_field(
        "max_10m_wind_mph",
        _find(surface_fields, {"max_10si", "wind", "si10"}, type_contains="heightAboveGround", level=10),
        units="mph",
        factor=MPS_TO_MPH,
        source_label="WIND hourly maximum at 10 m AGL",
    )

    # MAXREF is an HRRR local-table parameter that may decode as "unknown" in
    # ecCodes. Within this request the only hourly-max field at 1000 m AGL is
    # MAXREF, so that metadata combination is an unambiguous fallback.
    max_ref = _find(surface_fields, {"maxref"}, type_contains="heightAboveGround", level=1000)
    if max_ref is None:
        max_ref = _find(
            surface_fields,
            {"unknown"},
            type_contains="heightAboveGround",
            level=1000,
            step_type="max",
        )
    add_field(
        "max_reflectivity_1km_dbz",
        max_ref,
        units="dBZ",
        source_label="MAXREF hourly maximum simulated reflectivity at 1 km AGL",
    )

    add_field(
        "composite_reflectivity_dbz",
        _find(surface_fields, {"refc"}),
        units="dBZ",
        source_label="REFC composite reflectivity",
    )
    add_field(
        "echo_top_ft",
        _find(surface_fields, {"retop"}),
        units="ft",
        factor=M_TO_FT,
        source_label="RETOP echo top",
    )
    add_field(
        "vil_kg_m2",
        _find(surface_fields, {"vil", "veril"}),
        units="kg/m^2",
        source_label="VIL radar-simulated vertically integrated liquid",
    )
    add_field(
        "lightning_model_field",
        _find(surface_fields, {"ltng"}),
        source_label="LTNG HRRR lightning field",
    )
    add_field(
        "pwat_inches",
        _find(surface_fields, {"pwat"}),
        units="in",
        factor=MM_TO_IN,
        source_label="PWAT precipitable water",
    )

    # CAPE/CIN are kept by their actual HRRR layer label rather than imposing a
    # parcel-method name that the GRIB metadata itself does not explicitly use.
    add_field(
        "cape_surface_jkg",
        _find(surface_fields, {"cape"}, type_contains="surface"),
        units="J/kg",
        source_label="CAPE surface",
    )
    add_field(
        "cin_surface_jkg",
        _find(surface_fields, {"cin"}, type_contains="surface"),
        units="J/kg",
        source_label="CIN surface",
    )
    for depth in (90, 180, 255):
        add_field(
            f"cape_{depth}_0mb_jkg",
            _pressure_layer_field(surface_fields, "cape", depth),
            units="J/kg",
            source_label=f"CAPE {depth}-0 mb above ground",
        )
        add_field(
            f"cin_{depth}_0mb_jkg",
            _pressure_layer_field(surface_fields, "cin", depth),
            units="J/kg",
            source_label=f"CIN {depth}-0 mb above ground",
        )

    add_field(
        "srh_0_1km_m2s2",
        _find_layer(surface_fields, {"hlcy"}, 1000.0),
        units="m^2/s^2",
        source_label="HLCY storm-relative helicity 0-1 km AGL",
    )
    add_field(
        "srh_0_3km_m2s2",
        _find_layer(surface_fields, {"hlcy"}, 3000.0),
        units="m^2/s^2",
        source_label="HLCY storm-relative helicity 0-3 km AGL",
    )

    # MAXDVV is encoded as negative vertical velocity. Convert it to a
    # positive downward-speed magnitude before spatial/temporal maximization so
    # stronger downdrafts correspond to larger values in the operational JSON.
    downdraft_fields = special_fields.get("max_downdraft", [])
    if downdraft_fields:
        field = downdraft_fields[0]
        magnitude = np.maximum(-np.asarray(field.values, dtype=float), 0.0)
        proxy = Field(
            short_name=field.short_name,
            type_of_level=field.type_of_level,
            level=field.level,
            step_type=field.step_type,
            units=field.units,
            values=magnitude,
            latitude=field.latitude,
            longitude=field.longitude,
            long_name=field.long_name,
            parameter_number=field.parameter_number,
            parameter_category=field.parameter_category,
            top_level=field.top_level,
            bottom_level=field.bottom_level,
        )
        output["max_downdraft_magnitude_ms"] = _field_metric(
            proxy,
            bounds,
            units="m/s",
            source_label=(
                "MAXDVV downward-speed magnitude (positive magnitude derived "
                "from the negative HRRR vertical-velocity field)"
            ),
        )

    special_specs = {
        "max_updraft_velocity_ms": (
            "max_updraft",
            "m/s",
            "MAXUVV hourly maximum upward vertical velocity in the lowest 400 hPa",
        ),
        "updraft_helicity_2_5km_m2s2": (
            "updraft_helicity",
            "m^2/s^2",
            "MXUPHL hourly maximum updraft helicity 2-5 km AGL",
        ),
    }
    for key, (product, units, source_label) in special_specs.items():
        metric = _first_metric(
            special_fields.get(product, []),
            bounds,
            units=units,
            source_label=source_label,
        )
        if metric is not None:
            output[key] = metric

    derived_key_map = {
        "lapse_rate_700_500mb": "lapse_rate_700_500mb_k_per_km",
        "lapse_rate_0_3km_agl": "lapse_rate_0_3km_agl_k_per_km",
        "dewpoint_depression_700mb": "dewpoint_depression_700mb_k",
        "relative_humidity_700mb": "relative_humidity_700mb_percent",
        "bulk_shear_0_1km": "bulk_shear_0_1km_kt",
        "bulk_shear_0_6km": "bulk_shear_0_6km_kt",
    }
    for source_key, target_key in derived_key_map.items():
        if source_key in derived:
            output[target_key] = derived[source_key]

    return output


PEAK_METRICS = (
    "surface_gust_mph",
    "max_10m_wind_mph",
    "max_downdraft_magnitude_ms",
    "max_updraft_velocity_ms",
    "max_reflectivity_1km_dbz",
    "composite_reflectivity_dbz",
    "echo_top_ft",
    "vil_kg_m2",
    "pwat_inches",
    "cape_surface_jkg",
    "cin_surface_jkg",
    "cape_90_0mb_jkg",
    "cin_90_0mb_jkg",
    "cape_180_0mb_jkg",
    "cin_180_0mb_jkg",
    "cape_255_0mb_jkg",
    "cin_255_0mb_jkg",
    "srh_0_1km_m2s2",
    "srh_0_3km_m2s2",
    "updraft_helicity_2_5km_m2s2",
    "bulk_shear_0_1km_kt",
    "bulk_shear_0_6km_kt",
    "lapse_rate_0_3km_agl_k_per_km",
    "lapse_rate_700_500mb_k_per_km",
    "dewpoint_depression_700mb_k",
)


def _peak_for_stat(
    records: list[dict[str, object]],
    metric_key: str,
    stat: str,
) -> dict[str, object] | None:
    best: dict[str, object] | None = None
    best_value: float | None = None
    for record in records:
        diagnostics = record.get("key_diagnostics")
        if not isinstance(diagnostics, dict):
            continue
        metric = diagnostics.get(metric_key)
        if not isinstance(metric, dict):
            continue
        summary = metric.get("summary")
        if not isinstance(summary, dict):
            continue
        value = summary.get(stat)
        if not isinstance(value, (int, float)):
            continue
        numeric = float(value)
        if best_value is None or numeric > best_value:
            best_value = numeric
            best = {
                "value": numeric,
                "valid_time": record.get("valid_time"),
                "forecast_hour": record.get("forecast_hour"),
                "units": metric.get("units"),
            }
    return best


def build_operational_summary(records: list[dict[str, object]]) -> dict[str, object]:
    metrics: dict[str, object] = {}
    for key in PEAK_METRICS:
        county_max = _peak_for_stat(records, key, "max")
        county_p90 = _peak_for_stat(records, key, "p90")
        if county_max is None and county_p90 is None:
            continue
        metrics[key] = {
            "peak_county_max": county_max,
            "peak_county_p90": county_p90,
        }

    # For the 700-mb RH dryness proxy, minimum values are operationally more
    # informative than maxima.
    driest: dict[str, object] | None = None
    driest_value: float | None = None
    for record in records:
        diagnostics = record.get("key_diagnostics")
        if not isinstance(diagnostics, dict):
            continue
        metric = diagnostics.get("relative_humidity_700mb_percent")
        if not isinstance(metric, dict):
            continue
        summary = metric.get("summary")
        if not isinstance(summary, dict):
            continue
        value = summary.get("min")
        if not isinstance(value, (int, float)):
            continue
        numeric = float(value)
        if driest_value is None or numeric < driest_value:
            driest_value = numeric
            driest = {
                "value": numeric,
                "valid_time": record.get("valid_time"),
                "forecast_hour": record.get("forecast_hour"),
                "units": metric.get("units"),
            }
    if driest is not None:
        metrics["minimum_relative_humidity_700mb_percent"] = {
            "minimum_county_value": driest
        }

    return {
        "metrics": metrics,
        "interpretation_note": (
            "County maxima can be isolated grid cells. Pair peak_county_max with "
            "peak_county_p90, reflectivity/coverage, radar trends, and observed "
            "mesoanalysis before making an operational wind-risk verdict."
        ),
    }
