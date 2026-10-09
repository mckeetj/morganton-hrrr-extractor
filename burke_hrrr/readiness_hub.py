"""Build the current Morganton Operational Readiness Hub snapshot.

The builder intentionally separates source acquisition from Hub methodology:
- NWS digital/grid data supply objective weather values and active alerts.
- The Daily Downburst Model publishes the reviewed downburst handoff.
- MORG ECONet supplies antecedent rainfall context.
- Load Stress remains a manual Morganton Electric assessment.

The generated snapshot always defaults to REVIEWED. It never designates itself
OPERATIONAL.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
LAT = 35.745
LON = -81.684
USER_AGENT = "MorgantonElectricOperationalReadiness/1.0 (tmckee@morgantonnc.gov)"
NWS_POINT_URL = f"https://api.weather.gov/points/{LAT},{LON}"
NWS_ALERT_URL = (
    "https://api.weather.gov/alerts/active?"
    + urllib.parse.urlencode({"point": f"{LAT},{LON}"})
)
NWS_DWML_URL = (
    "https://forecast.weather.gov/MapClick.php"
    f"?lat={LAT}&lon={LON}&FcstType=digitalDWML"
)

ALERT_PRIORITY = {
    "Tornado Warning": 100,
    "Severe Thunderstorm Warning": 90,
    "Tornado Watch": 80,
    "Severe Thunderstorm Watch": 70,
    "Flash Flood Warning": 65,
    "Flood Warning": 60,
    "Ice Storm Warning": 58,
    "Winter Storm Warning": 56,
    "High Wind Warning": 54,
    "Flood Watch": 50,
    "Winter Storm Watch": 48,
    "Winter Weather Advisory": 46,
    "Wind Advisory": 44,
}


def _parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=EASTERN)
    return parsed


def _parse_duration(value: str) -> dt.timedelta:
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?)?",
        value,
    )
    if not match:
        raise ValueError(f"unsupported ISO-8601 duration: {value}")
    return dt.timedelta(
        days=int(match.group("days") or 0),
        hours=int(match.group("hours") or 0),
        minutes=int(match.group("minutes") or 0),
    )


def _parse_valid_time(value: str) -> tuple[dt.datetime, dt.datetime]:
    start_text, duration_text = value.split("/", 1)
    start = _parse_time(start_text)
    if start is None:
        raise ValueError(f"invalid validTime: {value}")
    return start, start + _parse_duration(duration_text)


def _overlaps(
    start: dt.datetime,
    end: dt.datetime,
    window_start: dt.datetime,
    window_end: dt.datetime,
) -> bool:
    return start < window_end and end > window_start


def _fully_inside(
    start: dt.datetime,
    end: dt.datetime,
    window_start: dt.datetime,
    window_end: dt.datetime,
) -> bool:
    return start >= window_start and end <= window_end


def _fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/geo+json, application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _dwml_layouts(root: ET.Element) -> dict[str, list[tuple[dt.datetime, dt.datetime]]]:
    layouts: dict[str, list[tuple[dt.datetime, dt.datetime]]] = {}
    for layout in root.findall(".//time-layout"):
        key = layout.findtext("layout-key")
        if not key:
            continue
        starts = layout.findall("start-valid-time")
        ends = layout.findall("end-valid-time")
        rows: list[tuple[dt.datetime, dt.datetime]] = []
        for index, start_el in enumerate(starts):
            start = _parse_time(start_el.text)
            if start is None:
                continue
            end = _parse_time(ends[index].text) if index < len(ends) else None
            if end is None:
                end = start + dt.timedelta(hours=1)
            rows.append((start, end))
        layouts[key] = rows
    return layouts


def source_aligned_qpf_24(
    dwml_text: str,
    now: dt.datetime,
) -> dict[str, Any]:
    """Return the NWS-provided 24 consecutive one-hour QPF slots.

    The window starts with the one-hour source interval containing now.
    If no containing interval exists, it starts with the first future interval.
    Values are never minute-prorated.
    """
    root = ET.fromstring(dwml_text)
    creation_time = _parse_time(root.findtext(".//creation-date"))
    layouts = _dwml_layouts(root)
    qpf = root.find(".//hourly-qpf")
    if qpf is None:
        raise ValueError("NWS digital forecast did not include hourly-qpf")

    layout_key = qpf.attrib.get("time-layout")
    times = layouts.get(layout_key or "", [])
    values = qpf.findall("value")
    rows: list[dict[str, Any]] = []
    for index, value_el in enumerate(values):
        if index >= len(times):
            break
        start, end = times[index]
        nil = value_el.attrib.get(
            "{http://www.w3.org/2001/XMLSchema-instance}nil"
        )
        value = (
            None
            if nil == "true" or value_el.text is None
            else _finite_number(float(value_el.text))
        )
        rows.append({"start": start, "end": end, "value": value})

    now = now.astimezone(EASTERN)
    first_index = None
    for index, row in enumerate(rows):
        if row["start"] <= now < row["end"]:
            first_index = index
            break
        if row["start"] > now:
            first_index = index
            break
    if first_index is None:
        raise ValueError("NWS hourly-qpf has no current/future interval")

    selected = rows[first_index : first_index + 24]
    if len(selected) != 24:
        raise ValueError("NWS hourly-qpf does not cover 24 source hours")

    for index, row in enumerate(selected):
        if row["value"] is None:
            raise ValueError("NWS hourly-qpf contains a missing value")
        if row["end"] - row["start"] != dt.timedelta(hours=1):
            raise ValueError("NWS hourly-qpf interval is not one hour")
        if index and selected[index - 1]["end"] != row["start"]:
            raise ValueError("NWS hourly-qpf intervals are not contiguous")

    return {
        "source_creation_time": creation_time,
        "window_start": selected[0]["start"],
        "window_end": selected[-1]["end"],
        "qpf_24_in": sum(float(row["value"]) for row in selected),
        "interval_count": 24,
    }


def _grid_values(
    grid: dict[str, Any],
    key: str,
) -> tuple[str | None, list[dict[str, Any]]]:
    prop = grid.get("properties", {}).get(key)
    if not isinstance(prop, dict):
        return None, []
    values = prop.get("values")
    return prop.get("uom"), values if isinstance(values, list) else []


def _max_grid_value(
    values: list[dict[str, Any]],
    window_start: dt.datetime,
    window_end: dt.datetime,
) -> float | None:
    result: list[float] = []
    for row in values:
        valid_time = row.get("validTime")
        number = _finite_number(row.get("value"))
        if not isinstance(valid_time, str) or number is None:
            continue
        start, end = _parse_valid_time(valid_time)
        if _overlaps(start, end, window_start, window_end):
            result.append(number)
    return max(result) if result else None


def _min_grid_value(
    values: list[dict[str, Any]],
    window_start: dt.datetime,
    window_end: dt.datetime,
) -> float | None:
    result: list[float] = []
    for row in values:
        valid_time = row.get("validTime")
        number = _finite_number(row.get("value"))
        if not isinstance(valid_time, str) or number is None:
            continue
        start, end = _parse_valid_time(valid_time)
        if _overlaps(start, end, window_start, window_end):
            result.append(number)
    return min(result) if result else None


def conservative_accumulation(
    values: list[dict[str, Any]],
    window_start: dt.datetime,
    window_end: dt.datetime,
) -> float | None:
    """Sum gridded accumulation values without prorating partial intervals.

    A partially overlapping source interval is safe only when its accumulation
    is exactly zero. A non-zero partial interval makes the requested total
    unavailable rather than inventing a fraction.
    """
    total = 0.0
    saw_coverage = False
    for row in values:
        valid_time = row.get("validTime")
        number = _finite_number(row.get("value"))
        if not isinstance(valid_time, str) or number is None:
            continue
        start, end = _parse_valid_time(valid_time)
        if not _overlaps(start, end, window_start, window_end):
            continue
        saw_coverage = True
        if _fully_inside(start, end, window_start, window_end):
            total += number
        elif abs(number) > 1e-12:
            return None
    return total if saw_coverage else None


def _kmh_to_mph(value: float) -> float:
    return value / 1.609344


def _c_to_f(value: float) -> float:
    return value * 9.0 / 5.0 + 32.0


def _mm_to_in(value: float) -> float:
    return value / 25.4


def fetch_nws_weather(now: dt.datetime) -> dict[str, Any]:
    point = _fetch_json(NWS_POINT_URL)
    grid_url = point.get("properties", {}).get("forecastGridData")
    if not isinstance(grid_url, str):
        raise ValueError("NWS point metadata did not provide forecastGridData")
    grid = _fetch_json(grid_url)
    dwml = _fetch_text(NWS_DWML_URL)
    qpf = source_aligned_qpf_24(dwml, now)

    qpf_start = qpf["window_start"]
    qpf_end = qpf["window_end"]
    forecast_end = qpf_start + dt.timedelta(hours=72)

    gust_uom, gust_values = _grid_values(grid, "windGust")
    gust = _max_grid_value(gust_values, qpf_start, qpf_end)
    if gust is not None and gust_uom == "wmoUnit:km_h-1":
        gust = _kmh_to_mph(gust)

    temp_uom, temp_values = _grid_values(grid, "temperature")
    min_temp = _min_grid_value(temp_values, qpf_start, forecast_end)
    if min_temp is not None and temp_uom == "wmoUnit:degC":
        min_temp = _c_to_f(min_temp)

    snow_uom, snow_values = _grid_values(grid, "snowfallAmount")
    snow = conservative_accumulation(snow_values, qpf_start, forecast_end)
    if snow is not None and snow_uom == "wmoUnit:mm":
        snow = _mm_to_in(snow)

    ice_uom, ice_values = _grid_values(grid, "iceAccumulation")
    ice = conservative_accumulation(ice_values, qpf_start, forecast_end)
    if ice is not None and ice_uom == "wmoUnit:mm":
        ice = _mm_to_in(ice)

    update_time = _parse_time(grid.get("properties", {}).get("updateTime"))

    return {
        "grid_url": grid_url,
        "source_update_time": update_time or qpf["source_creation_time"],
        "qpf_window_start": qpf_start,
        "qpf_window_end": qpf_end,
        "qpf_24_in": qpf["qpf_24_in"],
        "wind_gust_mph": gust,
        "min_temp_f": min_temp,
        "snowfall_in": snow,
        "ice_accretion_in": ice,
    }


def fetch_nws_alert(now: dt.datetime) -> dict[str, Any]:
    data = _fetch_json(NWS_ALERT_URL)
    candidates: list[dict[str, Any]] = []
    for feature in data.get("features", []):
        properties = feature.get("properties", {})
        event = properties.get("event")
        if not isinstance(event, str):
            continue
        expires = _parse_time(properties.get("expires"))
        ends = _parse_time(properties.get("ends"))
        effective = _parse_time(properties.get("effective"))
        onset = _parse_time(properties.get("onset"))
        end = ends or expires
        start = onset or effective
        if start and now < start:
            continue
        if end and now >= end:
            continue
        candidates.append(properties)

    if not candidates:
        return {
            "event": "None",
            "source_time": None,
            "notes": "No active NWS alert returned for the Morganton point.",
        }

    def key(properties: dict[str, Any]) -> tuple[int, dt.datetime]:
        event = str(properties.get("event") or "")
        sent = _parse_time(properties.get("sent")) or dt.datetime.min.replace(
            tzinfo=dt.UTC
        )
        return ALERT_PRIORITY.get(event, 1), sent

    chosen = max(candidates, key=key)
    event = str(chosen.get("event") or "None")
    source_time = (
        _parse_time(chosen.get("sent"))
        or _parse_time(chosen.get("effective"))
        or _parse_time(chosen.get("onset"))
    )
    area = chosen.get("areaDesc")
    headline = chosen.get("headline")
    notes_parts = [f"Active NWS alert selected for Hub display: {event}."]
    if area:
        notes_parts.append(f"Area: {area}.")
    if headline:
        notes_parts.append(f"Headline: {headline}")
    return {
        "event": event,
        "source_time": source_time,
        "notes": " ".join(notes_parts),
    }


def _choose_econet(repo_root: Path) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for filename in ("morg-econet-0830.json", "morg-econet-1030.json"):
        value = _read_json(repo_root / "current" / filename)
        if value is None:
            continue
        generated = _parse_time(value.get("generated_at"))
        if generated is None:
            continue
        value = dict(value)
        value["_filename"] = filename
        value["_generated"] = generated
        candidates.append(value)
    return max(candidates, key=lambda item: item["_generated"]) if candidates else None


def _econet_context(
    repo_root: Path,
    now: dt.datetime,
) -> tuple[float | None, dict[str, Any]]:
    econet = _choose_econet(repo_root)
    if econet is None:
        return None, {
            "status": "Unavailable",
            "notes": "No parseable current MORG ECONet snapshot was available.",
        }

    rainfall = econet.get("rainfall", {}).get("72h", {}).get("total_inches")
    rainfall_value = _finite_number(rainfall)
    latest = _parse_time(
        econet.get("freshness", {}).get("latest_accepted_observation_time")
    )
    threshold_minutes = _finite_number(
        econet.get("freshness", {}).get("maximum_current_age_minutes")
    )
    status = "Available"
    age_hours = None
    if latest is not None:
        age_hours = (now - latest).total_seconds() / 3600.0
    if (
        latest is None
        or threshold_minutes is None
        or age_hours is None
        or age_hours * 60.0 > threshold_minutes
    ):
        status = "Stale"

    coverage = econet.get("rainfall", {}).get("72h", {}).get("coverage_percent")
    notes = (
        f"MORG ECONet source file {econet['_filename']}. "
        f"Rolling 72-hour rainfall={rainfall_value if rainfall_value is not None else 'unavailable'} in"
    )
    if coverage is not None:
        notes += f" with {coverage}% coverage."
    else:
        notes += "."
    if latest is not None:
        notes += f" Latest accepted observation={latest.astimezone(EASTERN).isoformat()}."
    if threshold_minutes is not None:
        notes += f" Freshness threshold={threshold_minutes} minutes."

    source = {
        "status": status,
        "retrieved_at": econet["_generated"].isoformat(),
        "notes": notes,
    }
    if latest is not None:
        source["observation_time"] = latest.isoformat()
    if threshold_minutes is not None:
        source["freshness_threshold_hours"] = threshold_minutes / 60.0
    return rainfall_value, source


def _hrrr_source(repo_root: Path, now: dt.datetime) -> dict[str, Any]:
    hrrr = _read_json(repo_root / "current" / "burke-hrrr-12z.json")
    if hrrr is None:
        return {"status": "Unavailable", "notes": "HRRR repository snapshot unavailable."}

    generated = _parse_time(hrrr.get("generated_at"))
    published = _parse_time(hrrr.get("snapshot_published_at"))
    cycle = _parse_time(hrrr.get("cycle_initialized"))
    through = _parse_time(hrrr.get("through"))
    today = now.astimezone(EASTERN).date()
    status = "Available"
    if cycle is None or cycle.astimezone(EASTERN).date() != today:
        status = "Stale"
    if through is not None and through <= now:
        status = "Stale"

    source: dict[str, Any] = {
        "status": status,
        "notes": (
            "Burke County HRRR decision-support extraction. Used as an input to "
            "the late-morning Daily Downburst Model, not as the Hub Downburst Index."
        ),
    }
    if generated is not None:
        source["observation_time"] = generated.isoformat()
    if published is not None:
        source["retrieved_at"] = published.isoformat()
    elif generated is not None:
        source["retrieved_at"] = generated.isoformat()
    return source


def _downburst_handoff(
    repo_root: Path,
    now: dt.datetime,
) -> tuple[int | None, str | None, dict[str, Any], dict[str, Any]]:
    value = _read_json(repo_root / "current" / "downburst-outlook.json")
    unavailable = {
        "status": "Unavailable",
        "notes": "No valid current Daily Downburst Model handoff was available.",
    }
    if value is None:
        return None, None, unavailable, unavailable.copy()

    valid_date = value.get("valid_date")
    today = now.astimezone(EASTERN).date().isoformat()
    index = _finite_number(value.get("downburst_index"))
    issued = _parse_time(value.get("issued_at"))
    if (
        valid_date != today
        or index is None
        or not 0 <= index <= 100
        or issued is None
    ):
        return None, None, unavailable, unavailable.copy()

    spc_category = value.get("spc_category")
    allowed_spc = {
        "General",
        "Thunderstorms",
        "Marginal",
        "Slight",
        "Enhanced",
        "Moderate",
        "High",
    }
    if spc_category not in allowed_spc:
        spc_category = None

    basis = value.get("index_basis")
    verdict = value.get("verdict")
    confidence = value.get("confidence")
    source_run = value.get("source_run")

    downburst_source = {
        "status": "Available",
        "observation_time": issued.isoformat(),
        "retrieved_at": now.isoformat(),
        "notes": (
            f"Daily Downburst Model handoff from {source_run or 'current run'}; "
            f"verdict={verdict}; index={int(index)}; confidence={confidence}. "
            f"{basis or ''}"
        ).strip(),
    }

    if spc_category is None:
        spc_source: dict[str, Any] = {
            "status": "Unavailable",
            "notes": "Current Downburst handoff did not include a supported SPC category.",
        }
    else:
        spc_time = _parse_time(value.get("spc_issued_at")) or issued
        spc_source = {
            "status": "Available",
            "observation_time": spc_time.isoformat(),
            "retrieved_at": now.isoformat(),
            "notes": (
                "SPC category carried by the reviewed Daily Downburst Model handoff: "
                f"{spc_category}."
            ),
        }
    return int(index), spc_category, downburst_source, spc_source


def _load_stress(
    repo_root: Path,
    now: dt.datetime,
) -> tuple[int | None, dict[str, Any]]:
    value = _read_json(repo_root / "current" / "load-stress.json")
    if value is None:
        return None, {
            "status": "Not Assessed",
            "notes": "No current manual Morganton Electric Load Stress assessment file.",
        }
    level = value.get("level")
    if isinstance(level, bool) or not isinstance(level, int) or level not in range(1, 6):
        return None, {
            "status": "Not Assessed",
            "notes": "Load Stress assessment file did not contain a valid Level 1-5 value.",
        }
    assessed = _parse_time(value.get("assessed_at"))
    labels = {
        1: "Normal",
        2: "Awareness",
        3: "Elevated",
        4: "Storm Posture",
        5: "Major Event",
    }
    source: dict[str, Any] = {
        "status": "Manual",
        "retrieved_at": now.isoformat(),
        "notes": (
            f"Morganton Electric manual Load Stress assessment: Level {level} — "
            f"{labels[level]}. Not inferred from weather."
        ),
    }
    if assessed is not None:
        source["observation_time"] = assessed.isoformat()
    return level, source


def build_snapshot(
    repo_root: Path,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.UTC)
    now = now.astimezone(EASTERN)

    missing: list[str] = []
    sources: dict[str, Any] = {}
    severe: dict[str, Any] = {}
    winter: dict[str, Any] = {}

    try:
        nws = fetch_nws_weather(now)
        if nws["wind_gust_mph"] is not None:
            severe["wind_gust_mph"] = round(nws["wind_gust_mph"], 1)
        else:
            missing.append("severe.wind_gust_mph")
        severe["qpf_24_in"] = round(float(nws["qpf_24_in"]), 4)
        if nws["min_temp_f"] is not None:
            winter["min_temp_f"] = round(nws["min_temp_f"], 1)
        else:
            missing.append("winter.min_temp_f")
        if nws["snowfall_in"] is not None:
            winter["snowfall_in"] = round(nws["snowfall_in"], 4)
        else:
            missing.append("winter.snowfall_in")
        if nws["ice_accretion_in"] is not None:
            winter["ice_accretion_in"] = round(nws["ice_accretion_in"], 4)
        else:
            missing.append("winter.ice_accretion_in")

        source_time = nws["source_update_time"]
        sources["nws_nbm"] = {
            "status": "Available",
            "observation_time": source_time.isoformat() if source_time else now.isoformat(),
            "retrieved_at": now.isoformat(),
            "valid_time": nws["qpf_window_start"].isoformat(),
            "notes": (
                "Schema source bucket retained for backward compatibility. Actual source is "
                "NWS Digital Forecast / NDFD and api.weather.gov grid data for Morganton. "
                f"Source-aligned 24-hour QPF window {nws['qpf_window_start'].isoformat()} "
                f"through {nws['qpf_window_end'].isoformat()}, 24 consecutive NWS one-hour "
                "QPF slots, no minute-level prorating. Wind gust uses the maximum NWS grid "
                "windGust overlapping the same 24-hour window. Minimum temperature uses "
                "NWS hourly grid temperature through 72 hours. Snow and ice use NWS grid "
                "accumulation intervals conservatively; non-zero partial boundary intervals "
                "are unavailable rather than prorated."
            ),
        }
    except Exception as exc:
        missing.extend(
            [
                "severe.wind_gust_mph",
                "severe.qpf_24_in",
                "winter.min_temp_f",
                "winter.snowfall_in",
                "winter.ice_accretion_in",
            ]
        )
        sources["nws_nbm"] = {
            "status": "Unavailable",
            "retrieved_at": now.isoformat(),
            "notes": f"NWS Digital Forecast / grid retrieval failed: {exc}",
        }

    try:
        alert = fetch_nws_alert(now)
        severe["nws_alert"] = alert["event"]
        alert_source: dict[str, Any] = {
            "status": "Available",
            "retrieved_at": now.isoformat(),
            "notes": alert["notes"],
        }
        if alert["source_time"] is not None:
            alert_source["observation_time"] = alert["source_time"].isoformat()
        sources["nws_alerts"] = alert_source
    except Exception as exc:
        sources["nws_alerts"] = {
            "status": "Unavailable",
            "retrieved_at": now.isoformat(),
            "notes": f"NWS alert retrieval failed; alert applicability unknown: {exc}",
        }
        missing.append("severe.nws_alert_applicability")

    rainfall_72, econet_source = _econet_context(repo_root, now)
    sources["econet"] = econet_source
    if rainfall_72 is not None:
        severe["rainfall_72_in"] = rainfall_72
    else:
        missing.append("severe.rainfall_72_in")

    sources["hrrr"] = _hrrr_source(repo_root, now)

    downburst_index, spc_category, downburst_source, spc_source = _downburst_handoff(
        repo_root, now
    )
    sources["downburst_workflow"] = downburst_source
    sources["spc"] = spc_source
    if downburst_index is not None:
        severe["downburst_index"] = downburst_index
    else:
        missing.append("severe.downburst_index")
    if spc_category is not None:
        severe["spc_category"] = spc_category
    else:
        missing.append("severe.spc_category")

    load_level, load_source = _load_stress(repo_root, now)
    sources["load_stress"] = load_source
    if load_level is not None:
        winter["load_stress_level"] = load_level
    else:
        missing.append("winter.load_stress_level")

    missing = sorted(set(missing))
    completeness = "Complete controlling inputs" if not missing else "Partial / Degraded"
    notes = (
        "Automatically assembled controlled-pilot snapshot from the Morganton weather "
        "workflow. Defaults to REVIEWED and never self-designates OPERATIONAL. Daily "
        "Downburst Model supplies the reviewed Downburst Index/SPC handoff; NWS Digital "
        "Forecast/grid data supply wind, QPF, temperature, snow and ice; MORG ECONet "
        "supplies antecedent rainfall; Load Stress remains manual."
    )
    if missing:
        notes += " Unavailable or unresolved fields: " + ", ".join(missing) + "."

    return {
        "meta": {
            "mode": "REVIEWED",
            "generated_at": now.isoformat(),
            "forecast_window": "72 hours",
            "confidence": f"Automated pilot snapshot — {completeness}",
            "notes": notes,
        },
        "severe": severe,
        "winter": winter,
        "sources": sources,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--output",
        type=Path,
        default=Path("current/operational-readiness-hub.json"),
    )
    result.add_argument(
        "--now",
        help="Optional ISO-8601 time for deterministic validation.",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    now = _parse_time(args.now) if args.now else None
    snapshot = build_snapshot(Path("."), now=now)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "generated_at": snapshot["meta"]["generated_at"],
                "mode": snapshot["meta"]["mode"],
                "confidence": snapshot["meta"]["confidence"],
                "severe": snapshot["severe"],
                "winter": snapshot["winter"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
