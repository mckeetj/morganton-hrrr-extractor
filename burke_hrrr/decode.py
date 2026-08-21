from __future__ import annotations

import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .config import Bounds
from .diagnostics import summarize


@dataclass(frozen=True)
class Field:
    short_name: str
    type_of_level: str
    level: float | None
    step_type: str
    units: str
    values: np.ndarray
    latitude: np.ndarray
    longitude: np.ndarray
    long_name: str = ""
    parameter_number: int | None = None
    parameter_category: int | None = None
    top_level: float | None = None
    bottom_level: float | None = None


def _import_cfgrib() -> Any:
    try:
        import cfgrib
    except ImportError as exc:
        raise RuntimeError(
            "GRIB decoding requires the project dependencies; install with "
            "`python -m pip install -e .` in a network-enabled environment"
        ) from exc
    return cfgrib


def read_fields(path: Path) -> list[Field]:
    cfgrib = _import_cfgrib()
    # cfgrib currently emits repeated xarray FutureWarnings about a future merge
    # default. They are not operationally useful and can bury real NOAA errors
    # in GitHub Actions logs.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            category=FutureWarning,
            module=r"cfgrib\.xarray_store",
        )
        datasets = cfgrib.open_datasets(
            str(path),
            backend_kwargs={
                "indexpath": "",
                "read_keys": [
                    "parameterNumber",
                    "parameterCategory",
                    "topLevel",
                    "bottomLevel",
                ],
            },
        )

    fields: list[Field] = []
    for dataset in datasets:
        if "latitude" not in dataset or "longitude" not in dataset:
            continue
        latitude = np.asarray(dataset["latitude"].values)
        longitude = np.asarray(dataset["longitude"].values)
        longitude = np.where(longitude > 180, longitude - 360, longitude)

        for name, variable in dataset.data_vars.items():
            attrs = variable.attrs
            values = np.asarray(variable.values)
            common = {
                "short_name": str(attrs.get("GRIB_shortName", name)),
                "type_of_level": str(attrs.get("GRIB_typeOfLevel", "unknown")),
                "step_type": str(attrs.get("GRIB_stepType", "instant")),
                "units": str(attrs.get("GRIB_units", attrs.get("units", "unknown"))),
                "latitude": latitude,
                "longitude": longitude,
                "long_name": str(attrs.get("GRIB_name", attrs.get("long_name", ""))),
                "parameter_number": _as_int(attrs.get("GRIB_parameterNumber")),
                "parameter_category": _as_int(attrs.get("GRIB_parameterCategory")),
                "top_level": _as_float(attrs.get("GRIB_topLevel")),
                "bottom_level": _as_float(attrs.get("GRIB_bottomLevel")),
            }

            if values.ndim == latitude.ndim:
                level = _as_float(attrs.get("GRIB_level"))
                if level is None:
                    level = _scalar_level(dataset, variable.dims)
                fields.append(Field(level=level, values=values, **common))
            elif values.ndim == latitude.ndim + 1:
                vertical_dim = variable.dims[0]
                if vertical_dim not in dataset.coords:
                    continue
                levels = np.asarray(dataset.coords[vertical_dim].values).reshape(-1)
                for index, level in enumerate(levels):
                    fields.append(
                        Field(level=_as_float(level), values=values[index], **common)
                    )
    return fields


def _as_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _scalar_level(dataset: Any, dims: tuple[str, ...]) -> float | None:
    for coord_name, coord in dataset.coords.items():
        if coord_name in {"latitude", "longitude", "time", "step", "valid_time"}:
            continue
        if coord_name in dims:
            continue
        values = np.asarray(coord.values)
        if values.size == 1:
            level = _as_float(values.reshape(-1)[0])
            if level is not None:
                return level
    return None


def bounds_mask(field: Field, bounds: Bounds) -> np.ndarray:
    return (
        (field.latitude >= bounds.south)
        & (field.latitude <= bounds.north)
        & (field.longitude >= bounds.west)
        & (field.longitude <= bounds.east)
    )


def summarize_field(field: Field, bounds: Bounds) -> dict[str, float | int | None]:
    values = np.asarray(field.values)
    mask = bounds_mask(field, bounds)
    if values.shape != mask.shape:
        return summarize(np.asarray([], dtype=float))
    return summarize(values[mask])


def summarize_fields(fields: list[Field], bounds: Bounds) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for field in fields:
        summary = summarize_field(field, bounds)
        if summary["count"] == 0:
            continue
        output.append(
            {
                "field": field.short_name,
                "name": field.long_name,
                "type_of_level": field.type_of_level,
                "level": field.level,
                "top_level": field.top_level,
                "bottom_level": field.bottom_level,
                "step_type": field.step_type,
                "units": field.units,
                "parameter_category": field.parameter_category,
                "parameter_number": field.parameter_number,
                "summary": summary,
            }
        )
    return output
