from __future__ import annotations

import datetime as dt
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .config import Bounds, PRESSURE_LEVELS_HPA, PRESSURE_VARIABLES, SURFACE_LEVELS, SURFACE_VARIABLES

NOMADS_BASE = "https://nomads.ncep.noaa.gov/cgi-bin"
NODD_BASE = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com"


@dataclass(frozen=True, order=True)
class Cycle:
    initialized: dt.datetime

    def __post_init__(self) -> None:
        if self.initialized.tzinfo is None:
            raise ValueError("cycle time must be timezone-aware")

    @property
    def date(self) -> str:
        return self.initialized.astimezone(dt.UTC).strftime("%Y%m%d")

    @property
    def hour(self) -> str:
        return self.initialized.astimezone(dt.UTC).strftime("%H")

    def valid_time(self, forecast_hour: int) -> dt.datetime:
        return self.initialized + dt.timedelta(hours=forecast_hour)


def _request(url: str, *, method: str = "GET", timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(
        url,
        method=method,
        headers={"User-Agent": "burke-hrrr/0.1 operational-decision-support"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def nodd_index_url(cycle: Cycle, forecast_hour: int, product: str = "wrfsfc") -> str:
    filename = f"hrrr.t{cycle.hour}z.{product}f{forecast_hour:02d}.grib2.idx"
    return f"{NODD_BASE}/hrrr.{cycle.date}/conus/{filename}"


def cycle_has_forecast(cycle: Cycle, forecast_hour: int, *, timeout: float = 10.0) -> bool:
    try:
        data = _request(nodd_index_url(cycle, forecast_hour), timeout=timeout)
    except (urllib.error.URLError, TimeoutError):
        return False
    return b":d=" in data and b":TMP:" in data


def discover_latest_complete_cycle(
    *,
    now: dt.datetime,
    required_forecast_hour: int,
    lookback_hours: int = 18,
    timeout: float = 10.0,
) -> Cycle:
    """Find the newest cycle with the requested terminal forecast hour present."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if required_forecast_hour < 0:
        raise ValueError("required_forecast_hour must be nonnegative")
    latest_hour = now.astimezone(dt.UTC).replace(minute=0, second=0, microsecond=0)
    for offset in range(lookback_hours + 1):
        candidate = Cycle(latest_hour - dt.timedelta(hours=offset))
        if cycle_has_forecast(candidate, required_forecast_hour, timeout=timeout):
            return candidate
    raise RuntimeError(
        f"no complete HRRR cycle found in {lookback_hours} hours "
        f"for forecast hour {required_forecast_hour}"
    )


def forecast_hour_through(cycle_time: dt.datetime, through: dt.datetime) -> int:
    if cycle_time.tzinfo is None or through.tzinfo is None:
        raise ValueError("times must be timezone-aware")
    seconds = (through.astimezone(dt.UTC) - cycle_time.astimezone(dt.UTC)).total_seconds()
    if seconds < 0:
        return 0
    return int((seconds + 3599) // 3600)


def _query_flags(prefix: str, values: Iterable[str]) -> dict[str, str]:
    # NOMADS replaces spaces with underscores but preserves punctuation such as
    # the hyphen in "90-0 mb above ground".
    return {f"{prefix}_{value.replace(' ', '_')}": "on" for value in values}


def build_nomads_url(
    cycle: Cycle,
    forecast_hour: int,
    bounds: Bounds,
    *,
    product: str,
) -> str:
    bounds.validate()
    if product == "surface":
        script = "filter_hrrr_2d.pl"
        filename = f"hrrr.t{cycle.hour}z.wrfsfcf{forecast_hour:02d}.grib2"
        variables = SURFACE_VARIABLES
        levels = SURFACE_LEVELS
    elif product == "pressure":
        script = "filter_hrrr_3d.pl"
        filename = f"hrrr.t{cycle.hour}z.wrfprsf{forecast_hour:02d}.grib2"
        variables = PRESSURE_VARIABLES
        levels = tuple(f"{level} mb" for level in PRESSURE_LEVELS_HPA)
    else:
        raise ValueError(f"unknown product: {product}")

    params: dict[str, str] = {
        "dir": f"/hrrr.{cycle.date}/conus",
        "file": filename,
        "subregion": "",
        "leftlon": str(bounds.west),
        "rightlon": str(bounds.east),
        "toplat": str(bounds.north),
        "bottomlat": str(bounds.south),
    }
    params.update(_query_flags("var", variables))
    params.update(_query_flags("lev", levels))
    return f"{NOMADS_BASE}/{script}?{urllib.parse.urlencode(params)}"


def download_subset(
    cycle: Cycle,
    forecast_hour: int,
    bounds: Bounds,
    destination: Path,
    *,
    product: str,
    timeout: float = 120.0,
) -> str:
    url = build_nomads_url(cycle, forecast_hour, bounds, product=product)
    payload = _request(url, timeout=timeout)
    if not payload.startswith(b"GRIB"):
        preview = payload[:200].decode("utf-8", errors="replace")
        raise RuntimeError(f"NOAA response was not GRIB2: {preview}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    return url
