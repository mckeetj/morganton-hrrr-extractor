from __future__ import annotations

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


def _import_cfgrib() -> Any:
    try:
        import cfgrib
    except ImportError as exc:
        raise RuntimeError(
            "GRIB decoding requires the project dependencies; install with "
            "`uv pip install -e .` in a network-enabled environment"
        ) from exc
    return cfgrib


def read_fields(path: Path) -> list[Field]:
    cfgrib = _import_cfgrib()
    datasets = cfgrib.open_datasets(str(path), backend_kwargs={"indexpath": ""})
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
                    fields.append(Field(
                        level=_as_float(level), values=values[index], **common
                    ))
    return fields


def _as_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
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


def summarize_fields(fields: list[Field], bounds: Bounds) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for field in fields:
        mask = (
            (field.latitude >= bounds.south)
            & (field.latitude <= bounds.north)
            & (field.longitude >= bounds.west)
            & (field.longitude <= bounds.east)
        )
        values = np.asarray(field.values)
        if values.shape != mask.shape:
            continue
        output.append({
            "field": field.short_name,
            "type_of_level": field.type_of_level,
            "level": field.level,
            "step_type": field.step_type,
            "units": field.units,
            "summary": summarize(values[mask]),
        })
    return output
