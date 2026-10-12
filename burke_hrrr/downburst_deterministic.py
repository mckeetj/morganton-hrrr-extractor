"""Deterministic Daily Downburst Model for Morganton/Burke County.

This module produces the machine handoff consumed by the Operational Readiness
Hub without relying on an LLM or paid API. It combines:
- official SPC Day 1 categorical and wind-probability outlooks,
- NWS hourly thunderstorm wording for the Morganton point, and
- validated 12Z HRRR decision-support diagnostics for the late-morning refresh.

Antecedent rainfall and soil moisture are intentionally excluded from the
meteorological Downburst Index. They remain separate impact modifiers in the
Hub.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


EASTERN = ZoneInfo("America/New_York")
LAT = 35.745
LON = -81.684
USER_AGENT = "MorgantonElectricDownburst/1.0 (tmckee@morgantonnc.gov)"

MODEL_TYPE = "Deterministic"
MODEL_VERSION = "1.0"

SPC_SERVICE = (
    "https://mapservices.weather.noaa.gov/vector/rest/services/"
    "outlooks/SPC_wx_outlks/FeatureServer"
)
SPC_CATEGORY_LAYER = 1
SPC_WIND_LAYER = 7
NWS_POINT_URL = f"https://api.weather.gov/points/{LAT},{LON}"

CATEGORY_BY_DN = {
    2: "General",
    3: "Marginal",
    4: "Slight",
    5: "Enhanced",
    6: "Moderate",
    8: "High",
}

# Categorical risk is intentionally a small floor because a categorical outlook
# can be driven by hail/tornado risk rather than wind.
CATEGORY_BASE = {
    0: 0,
    2: 5,
    3: 7,
    4: 9,
    5: 11,
    6: 13,
    8: 15,
}

# SPC Day 1 wind probability is the primary official severe-wind baseline.
WIND_PROB_BASE = {
    0: 0,
    5: 18,
    15: 32,
    30: 46,
    45: 58,
    60: 68,
    75: 78,
    90: 88,
}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).astimezone(EASTERN)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _parse_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, application/geo+json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON response was not an object")
    return value


def _spc_query(layer: int) -> list[dict[str, Any]]:
    params = {
        "geometry": f"{LON},{LAT}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "dn,valid,expire,issue,label,idp_filedate",
        "returnGeometry": "false",
        "f": "json",
    }
    url = f"{SPC_SERVICE}/{layer}/query?" + urllib.parse.urlencode(params)
    data = _fetch_json(url)
    if data.get("error"):
        raise RuntimeError(f"SPC query error: {data['error']}")
    rows: list[dict[str, Any]] = []
    for feature in data.get("features", []):
        attrs = feature.get("attributes")
        if isinstance(attrs, dict):
            rows.append(attrs)
    return rows


def _latest_spc_time(*collections: list[dict[str, Any]]) -> dt.datetime | None:
    candidates: list[dt.datetime] = []
    for rows in collections:
        for row in rows:
            filedate = _finite(row.get("idp_filedate"))
            if filedate is not None:
                try:
                    candidates.append(
                        dt.datetime.fromtimestamp(filedate / 1000.0, tz=dt.UTC)
                    )
                except (OSError, OverflowError, ValueError):
                    pass
            issue = row.get("issue")
            if isinstance(issue, str):
                parsed = _parse_time(issue)
                if parsed is not None:
                    candidates.append(parsed)
                    continue
                digits = re.sub(r"[^0-9]", "", issue)
                for fmt, length in (("%Y%m%d%H%M", 12), ("%Y%m%d%H", 10)):
                    if len(digits) >= length:
                        try:
                            parsed_naive = dt.datetime.strptime(digits[:length], fmt)
                        except ValueError:
                            continue
                        candidates.append(parsed_naive.replace(tzinfo=dt.UTC))
                        break
    return max(candidates) if candidates else None


def fetch_spc() -> dict[str, Any]:
    category_rows = _spc_query(SPC_CATEGORY_LAYER)
    wind_rows = _spc_query(SPC_WIND_LAYER)

    category_dn = 0
    for row in category_rows:
        number = _finite(row.get("dn"))
        if number is not None:
            category_dn = max(category_dn, int(number))

    wind_probability = 0
    for row in wind_rows:
        number = _finite(row.get("dn"))
        if number is not None:
            wind_probability = max(wind_probability, int(number))

    category = CATEGORY_BY_DN.get(category_dn, "General")
    issue_time = _latest_spc_time(category_rows, wind_rows)
    return {
        "category_dn": category_dn,
        "category": category,
        "wind_probability_percent": wind_probability,
        "issued_at": issue_time,
    }


def _nws_hourly_url() -> str:
    point = _fetch_json(NWS_POINT_URL)
    url = point.get("properties", {}).get("forecastHourly")
    if not isinstance(url, str) or not url:
        raise ValueError("NWS point response did not provide forecastHourly")
    return url


def fetch_nws_thunder(now: dt.datetime) -> dict[str, Any]:
    data = _fetch_json(_nws_hourly_url())
    periods = data.get("properties", {}).get("periods", [])
    horizon = now + dt.timedelta(hours=18)
    thunder_times: list[dt.datetime] = []

    for period in periods:
        start = _parse_time(period.get("startTime"))
        if start is None or start < now - dt.timedelta(hours=1) or start > horizon:
            continue
        text = " ".join(
            str(period.get(key) or "")
            for key in ("shortForecast", "detailedForecast")
        ).lower()
        if "thunder" in text or "t-storm" in text or "tstorm" in text:
            thunder_times.append(start.astimezone(EASTERN))

    return {
        "available": True,
        "thunder_hours": len(thunder_times),
        "thunder_times": thunder_times,
        "update_time": _parse_time(data.get("properties", {}).get("updateTime")),
    }


def _version_tuple(value: Any) -> tuple[int, ...]:
    if not isinstance(value, str):
        return ()
    output: list[int] = []
    for token in value.split("."):
        try:
            output.append(int(token))
        except ValueError:
            break
    return tuple(output)


def load_hrrr(repo_root: Path, now: dt.datetime) -> dict[str, Any]:
    path = repo_root / "current" / "burke-hrrr-12z.json"
    value = _read_json(path)
    if value is None:
        return {"usable": False, "reason": "12Z HRRR file is missing or malformed"}

    today = now.astimezone(EASTERN).date()
    cycle = _parse_time(value.get("cycle_initialized"))
    generated = _parse_time(value.get("generated_at"))
    published = _parse_time(value.get("snapshot_published_at"))
    through = _parse_time(value.get("through"))

    reasons: list[str] = []
    if _version_tuple(value.get("extractor_version")) < (0, 3, 2):
        reasons.append("extractor_version < 0.3.2")
    if cycle is None or cycle.astimezone(EASTERN).date() != today:
        reasons.append("cycle is not current")
    elif cycle.astimezone(dt.UTC).hour != 12:
        reasons.append("cycle is not 12Z")
    if generated is None or generated.astimezone(EASTERN).date() != today:
        reasons.append("generated_at is not current")
    if published is None or published.astimezone(EASTERN).date() != today:
        reasons.append("snapshot_published_at is not current")
    if through is None or through.astimezone(EASTERN).date() < today:
        reasons.append("forecast does not cover the current local date")
    if not isinstance(value.get("operational_summary"), dict):
        reasons.append("operational_summary is missing")

    return {
        "usable": not reasons,
        "reason": "; ".join(reasons) if reasons else "validated",
        "data": value,
    }


def _metric(summary: dict[str, Any], key: str, which: str) -> float | None:
    metrics = summary.get("metrics")
    if not isinstance(metrics, dict):
        return None
    record = metrics.get(key)
    if not isinstance(record, dict):
        return None
    peak = record.get(which)
    if not isinstance(peak, dict):
        return None
    return _finite(peak.get("value"))


def _metric_time(
    summary: dict[str, Any],
    key: str,
    which: str,
) -> dt.datetime | None:
    metrics = summary.get("metrics")
    if not isinstance(metrics, dict):
        return None
    record = metrics.get(key)
    if not isinstance(record, dict):
        return None
    peak = record.get(which)
    if not isinstance(peak, dict):
        return None
    return _parse_time(peak.get("valid_time"))


def _threshold_points(
    value: float | None,
    thresholds: tuple[tuple[float, int], ...],
) -> int:
    if value is None:
        return 0
    result = 0
    for threshold, points in thresholds:
        if value >= threshold:
            result = points
    return result


def hrrr_components(hrrr: dict[str, Any]) -> dict[str, Any]:
    if not hrrr.get("usable"):
        return {
            "usable": False,
            "instability": 0,
            "convection": 0,
            "downdraft": 0,
            "organization": 0,
            "gust": 0,
            "synergy": 0,
            "total": 0,
            "reason": hrrr.get("reason", "HRRR unavailable"),
            "peak_time": None,
        }

    data = hrrr["data"]
    summary = data["operational_summary"]

    cape_p90 = _metric(summary, "cape_surface_jkg", "peak_county_p90")
    cape_max = _metric(summary, "cape_surface_jkg", "peak_county_max")
    instability = _threshold_points(
        cape_p90,
        ((250, 2), (500, 4), (1000, 6), (2000, 8)),
    )
    if instability == 0:
        instability = _threshold_points(
            cape_max,
            ((200, 1), (750, 2), (1500, 3)),
        )

    refl_p90_values = [
        _metric(summary, "composite_reflectivity_dbz", "peak_county_p90"),
        _metric(summary, "max_reflectivity_1km_dbz", "peak_county_p90"),
    ]
    refl_max_values = [
        _metric(summary, "composite_reflectivity_dbz", "peak_county_max"),
        _metric(summary, "max_reflectivity_1km_dbz", "peak_county_max"),
    ]
    refl_p90 = max((v for v in refl_p90_values if v is not None), default=None)
    refl_max = max((v for v in refl_max_values if v is not None), default=None)
    updraft_p90 = _metric(summary, "max_updraft_velocity_ms", "peak_county_p90")
    vil_p90 = _metric(summary, "vil_kg_m2", "peak_county_p90")

    convection = _threshold_points(
        refl_p90,
        ((30, 2), (40, 4), (50, 6)),
    )
    convection += _threshold_points(
        refl_max,
        ((35, 1), (45, 2), (55, 3)),
    )
    convection += _threshold_points(updraft_p90, ((3, 1), (5, 2)))
    convection += _threshold_points(vil_p90, ((5, 1), (15, 2)))
    convection = min(10, convection)

    downdraft_p90 = _metric(
        summary, "max_downdraft_magnitude_ms", "peak_county_p90"
    )
    pwat_p90 = _metric(summary, "pwat_inches", "peak_county_p90")
    dpd700_p90 = _metric(
        summary, "dewpoint_depression_700mb_k", "peak_county_p90"
    )
    lapse03_p90 = _metric(
        summary, "lapse_rate_0_3km_agl_k_per_km", "peak_county_p90"
    )

    downdraft = _threshold_points(
        downdraft_p90,
        ((2, 2), (4, 4), (6, 6)),
    )
    downdraft += _threshold_points(
        pwat_p90,
        ((1.25, 1), (1.5, 2), (2.0, 3)),
    )
    convective_gate = instability >= 2 or convection >= 2
    if convective_gate and dpd700_p90 is not None and lapse03_p90 is not None:
        if dpd700_p90 >= 25 and lapse03_p90 >= 7.0:
            downdraft += 3
        elif dpd700_p90 >= 15 and lapse03_p90 >= 6.5:
            downdraft += 2
    downdraft = min(10, downdraft)

    organization = 0
    shear_p90 = _metric(summary, "bulk_shear_sfc_500mb_kt", "peak_county_p90")
    if convective_gate:
        organization = _threshold_points(
            shear_p90,
            ((25, 1), (35, 2), (45, 3)),
        )

    gust = 0
    gust_p90 = _metric(summary, "surface_gust_mph", "peak_county_p90")
    gust_max = _metric(summary, "surface_gust_mph", "peak_county_max")
    if convective_gate:
        gust = _threshold_points(
            gust_p90,
            ((35, 2), (45, 4), (55, 6)),
        )
        if gust_p90 is not None and gust_p90 >= 35 and gust_max is not None:
            if gust_max >= 65 and gust_p90 >= 45:
                gust += 2
            elif gust_max >= 58:
                gust += 1
        gust = min(8, gust)

    synergy = 0
    if instability >= 4 and convection >= 4 and downdraft >= 4:
        synergy += 4
    if gust >= 4 and organization >= 2:
        synergy += 4

    total = instability + convection + downdraft + organization + gust + synergy

    peak_candidates: list[tuple[float, dt.datetime]] = []
    for key, which in (
        ("composite_reflectivity_dbz", "peak_county_p90"),
        ("max_reflectivity_1km_dbz", "peak_county_p90"),
        ("max_updraft_velocity_ms", "peak_county_p90"),
    ):
        value = _metric(summary, key, which)
        valid = _metric_time(summary, key, which)
        if value is not None and valid is not None:
            peak_candidates.append((value, valid))
    peak_time = max(peak_candidates, key=lambda item: item[0])[1] if peak_candidates else None

    return {
        "usable": True,
        "instability": instability,
        "convection": convection,
        "downdraft": downdraft,
        "organization": organization,
        "gust": gust,
        "synergy": synergy,
        "total": total,
        "reason": "validated 12Z HRRR",
        "peak_time": peak_time,
        "diagnostics": {
            "cape_surface_p90_jkg": cape_p90,
            "cape_surface_max_jkg": cape_max,
            "reflectivity_p90_dbz": refl_p90,
            "reflectivity_max_dbz": refl_max,
            "updraft_p90_ms": updraft_p90,
            "downdraft_p90_ms": downdraft_p90,
            "pwat_p90_in": pwat_p90,
            "dewpoint_depression_700mb_p90_k": dpd700_p90,
            "lapse_rate_0_3km_p90_k_per_km": lapse03_p90,
            "shear_10m_500mb_p90_kt": shear_p90,
            "surface_gust_p90_mph": gust_p90,
            "surface_gust_max_mph": gust_max,
        },
    }


def calculate_score(
    spc_category_dn: int,
    spc_wind_probability: int,
    thunder_hours: int,
    hrrr: dict[str, Any] | None = None,
) -> tuple[int, dict[str, int]]:
    official = max(
        CATEGORY_BASE.get(spc_category_dn, 0),
        WIND_PROB_BASE.get(spc_wind_probability, 0),
    )
    nws = 3 if thunder_hours >= 4 else 2 if thunder_hours > 0 else 0

    components = {
        "official_spc": official,
        "nws_thunder": nws,
        "hrrr_instability": 0,
        "hrrr_convection": 0,
        "hrrr_downdraft": 0,
        "hrrr_organization": 0,
        "hrrr_gust": 0,
        "hrrr_synergy": 0,
    }

    if hrrr is not None:
        model = hrrr_components(hrrr)
        components.update(
            {
                "hrrr_instability": int(model["instability"]),
                "hrrr_convection": int(model["convection"]),
                "hrrr_downdraft": int(model["downdraft"]),
                "hrrr_organization": int(model["organization"]),
                "hrrr_gust": int(model["gust"]),
                "hrrr_synergy": int(model["synergy"]),
            }
        )

    score = min(100, sum(components.values()))
    return score, components


def verdict_for_index(index: int) -> str:
    if index <= 24:
        return "LOW"
    if index <= 44:
        return "CONDITIONAL"
    if index <= 64:
        return "ELEVATED"
    return "HIGH"


def _format_clock(value: dt.datetime) -> str:
    local = value.astimezone(EASTERN)
    return local.strftime("%-I:%M %p %Z")


def likely_window(
    nws: dict[str, Any] | None,
    model: dict[str, Any] | None,
) -> str:
    if model and model.get("usable"):
        peak = model.get("peak_time")
        if isinstance(peak, dt.datetime) and (
            model.get("convection", 0) >= 2 or model.get("instability", 0) >= 2
        ):
            start = peak.astimezone(EASTERN) - dt.timedelta(hours=1)
            end = peak.astimezone(EASTERN) + dt.timedelta(hours=1)
            return f"{_format_clock(start)}–{_format_clock(end)}"

    if nws:
        times = nws.get("thunder_times")
        if isinstance(times, list) and times:
            start = min(times)
            end = max(times) + dt.timedelta(hours=1)
            return f"{_format_clock(start)}–{_format_clock(end)}"

    return "No meaningful convective window identified"


def _prior_handoff(repo_root: Path, today: dt.date) -> dict[str, Any] | None:
    value = _read_json(repo_root / "current" / "downburst-outlook.json")
    if value is None or value.get("valid_date") != today.isoformat():
        return None
    return value


def _run_satisfied(prior: dict[str, Any] | None, run: str) -> bool:
    if prior is None:
        return False
    source = prior.get("source_run")
    if run == "initial":
        return source in {"8:30 AM Initial", "11:00 AM Model Refresh"}
    return source == "11:00 AM Model Refresh"


def _auto_run(now: dt.datetime) -> str | None:
    local = now.astimezone(EASTERN)
    minutes = local.hour * 60 + local.minute
    if minutes >= 11 * 60 + 5:
        return "refresh"
    if minutes >= 8 * 60 + 35:
        return "initial"
    return None


def _trend(index: int, prior: dict[str, Any] | None, run: str) -> str | None:
    if run == "initial":
        return None
    prior_index = _finite(prior.get("downburst_index")) if prior else None
    if prior_index is None:
        return "Similar"
    difference = index - prior_index
    if difference >= 5:
        return "Higher"
    if difference <= -5:
        return "Lower"
    return "Similar"


def build_handoff(
    repo_root: Path,
    requested_run: str,
    now: dt.datetime | None = None,
    force: bool = False,
) -> tuple[dict[str, Any] | None, str]:
    current = now or _now()
    run = _auto_run(current) if requested_run == "auto" else requested_run
    if run is None:
        return None, "before the 08:35 Initial window"

    today = current.astimezone(EASTERN).date()
    prior = _prior_handoff(repo_root, today)
    if not force and _run_satisfied(prior, run):
        return None, f"same-day {run} handoff already satisfied"

    spc = fetch_spc()

    nws: dict[str, Any] | None
    try:
        nws = fetch_nws_thunder(current)
    except Exception as exc:
        nws = {
            "available": False,
            "thunder_hours": 0,
            "thunder_times": [],
            "error": str(exc),
        }

    hrrr: dict[str, Any] | None = None
    model: dict[str, Any] | None = None
    if run == "refresh":
        hrrr = load_hrrr(repo_root, current)
        model = hrrr_components(hrrr)

    index, components = calculate_score(
        int(spc["category_dn"]),
        int(spc["wind_probability_percent"]),
        int(nws.get("thunder_hours", 0)) if nws else 0,
        hrrr,
    )
    verdict = verdict_for_index(index)

    confidence = "Moderate"
    if not nws or not nws.get("available"):
        confidence = "Low"
    if run == "refresh" and (not hrrr or not hrrr.get("usable")):
        confidence = "Low"
    if (
        run == "refresh"
        and confidence == "Moderate"
        and spc["wind_probability_percent"] >= 15
        and model
        and model.get("total", 0) >= 10
    ):
        confidence = "High"

    source_run = "8:30 AM Initial" if run == "initial" else "11:00 AM Model Refresh"
    trend = _trend(index, prior, run)
    model_total = model.get("total", 0) if model else 0
    basis = (
        f"Deterministic v{MODEL_VERSION}: SPC {components['official_spc']} + "
        f"NWS thunder {components['nws_thunder']} + HRRR {model_total} = {index}; "
        f"SPC wind probability {spc['wind_probability_percent']}%."
    )

    handoff: dict[str, Any] = {
        "schema_version": 1,
        "model_type": MODEL_TYPE,
        "deterministic_model_version": MODEL_VERSION,
        "downburst_index": index,
        "verdict": verdict,
        "confidence": confidence,
        "source_run": source_run,
        "trend": trend,
        "valid_date": today.isoformat(),
        "issued_at": current.astimezone(EASTERN).isoformat(),
        "most_likely_window": likely_window(nws, model),
        "spc_category": spc["category"],
        "spc_issued_at": (
            spc["issued_at"].astimezone(EASTERN).isoformat()
            if isinstance(spc.get("issued_at"), dt.datetime)
            else None
        ),
        "index_basis": basis,
        "handoff_status": "Automated deterministic handoff",
        "notes": (
            "Deterministic Daily Downburst Model v1.0 machine handoff for the "
            "Operational Readiness Hub. Independent of the ChatGPT briefing."
        ),
        "score_components": components,
        "spc_wind_probability_percent": spc["wind_probability_percent"],
        "spc_categorical_dn": spc["category_dn"],
        "nws_thunder_hours_next_18h": (
            int(nws.get("thunder_hours", 0)) if nws else 0
        ),
        "hrrr_used": bool(hrrr and hrrr.get("usable")),
    }
    if model and model.get("diagnostics"):
        handoff["hrrr_diagnostics"] = model["diagnostics"]
    if hrrr and not hrrr.get("usable"):
        handoff["hrrr_unavailable_reason"] = hrrr.get("reason")

    return handoff, "generated"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--run",
        choices=("initial", "refresh", "auto"),
        default="auto",
    )
    parser.add_argument(
        "--output",
        default="current/downburst-outlook.json",
    )
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    try:
        handoff, status = build_handoff(
            repo_root,
            requested_run=args.run,
            force=args.force,
        )
    except Exception as exc:
        print(f"::error::Deterministic Downburst generation failed: {exc}", file=sys.stderr)
        return 1

    if handoff is None:
        print(f"No change: {status}")
        return 0

    output = repo_root / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(handoff, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "source_run": handoff["source_run"],
                "downburst_index": handoff["downburst_index"],
                "verdict": handoff["verdict"],
                "confidence": handoff["confidence"],
                "score_components": handoff["score_components"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
