"""MORG ECONet rolling rainfall and soil-moisture context via NCSCO CLOUDS API.

The public output intentionally contains derived summaries only. It does not republish
raw hourly observations or the exact current soil-moisture reading, consistent with the
CLOUDS API data-service agreement's restriction on redistributing raw data.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator
from zoneinfo import ZoneInfo

from . import __version__

EASTERN = ZoneInfo("America/New_York")
API_ENDPOINT = "https://api.climate.ncsu.edu/data.php"
STATION = "MORG"
VARIABLES = ("precip1m", "soilmoist", "soilmoist20cm")
QC_ACCEPTED = {0, 1}
MAX_CURRENT_AGE_MINUTES = 90.0
SOIL_DEPTHS_CM = {"soilmoist": 10, "soilmoist20cm": 20}
CSV_ATTRS = ("location", "datetime", "var", "value", "unit", "score", "flag", "obtime")


@dataclass(frozen=True)
class Observation:
    variable: str
    observed_at: dt.datetime
    value: float
    unit: str
    qc_score: int
    qc_flag: str | None
    flag_present: bool


def _parse_time(value: Any) -> dt.datetime | None:
    if isinstance(value, (int, float)):
        # Accept Unix seconds or milliseconds.
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000.0
        try:
            return dt.datetime.fromtimestamp(seconds, tz=dt.UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=EASTERN)
        return parsed
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
    ):
        try:
            return dt.datetime.strptime(text, fmt).replace(tzinfo=EASTERN)
        except ValueError:
            continue
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return result if math.isfinite(result) else None
    if isinstance(value, str):
        text = value.strip()
        if not text or text.upper() in {"MV", "QCF", "NA", "NAN", "NULL", "NONE", "-"}:
            return None
        try:
            result = float(text)
            return result if math.isfinite(result) else None
        except ValueError:
            return None
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _first(mapping: dict[str, Any], keys: Iterable[str]) -> Any:
    lower = {str(key).lower(): value for key, value in mapping.items()}
    for key in keys:
        if key.lower() in lower:
            return lower[key.lower()]
    return None


def _looks_like_datetime_key(key: str) -> dt.datetime | None:
    if len(key) < 8:
        return None
    return _parse_time(key)


def _iter_candidate_records(
    node: Any,
    context: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Flatten several plausible CLOUDS JSON layouts into record-like dicts.

    CLOUDS documents JSON output but not a stable example schema on the help page.
    This walker accepts both long-record JSON and nested location/datetime/variable
    layouts while still requiring explicit QC score and flag metadata downstream.
    """

    ctx = dict(context or {})

    if isinstance(node, list):
        for item in node:
            yield from _iter_candidate_records(item, ctx)
        return

    if not isinstance(node, dict):
        return

    loc = _first(node, ("location", "loc", "station", "station_id", "id"))
    if isinstance(loc, str) and loc.strip():
        ctx["location"] = loc.strip()

    when = _first(node, ("datetime", "date_time", "valid_time", "obtime", "time", "timestamp"))
    parsed_when = _parse_time(when)
    if parsed_when is not None:
        ctx["datetime"] = parsed_when

    variable = _first(node, ("var", "variable", "parameter", "parameter_code"))
    if isinstance(variable, str) and variable in VARIABLES:
        ctx["var"] = variable

    # Long-record representation: one variable/value per object.
    if ctx.get("var") in VARIABLES:
        value = _first(node, ("value", "val", "observation"))
        numeric = _as_float(value)
        if numeric is not None and ctx.get("datetime") is not None:
            merged = dict(ctx)
            merged.update(node)
            merged["value"] = numeric
            yield merged

    # Wide or nested representation where the parameter name is itself a key.
    for key, child in node.items():
        key_text = str(key)
        child_ctx = dict(ctx)

        if key_text in VARIABLES:
            child_ctx["var"] = key_text
            if isinstance(child, dict):
                yield from _iter_candidate_records(child, child_ctx)
            else:
                numeric = _as_float(child)
                if numeric is not None and child_ctx.get("datetime") is not None:
                    record = dict(child_ctx)
                    record["value"] = numeric
                    # Some wide formats put attributes next to var-specific values.
                    for suffix, canonical in (
                        ("unit", "unit"),
                        ("score", "score"),
                        ("flag", "flag"),
                    ):
                        sibling = node.get(f"{key_text}_{suffix}")
                        if sibling is not None:
                            record[canonical] = sibling
                    yield record
            continue

        parsed_key_time = _looks_like_datetime_key(key_text)
        if parsed_key_time is not None:
            child_ctx["datetime"] = parsed_key_time
        elif key_text.upper() == STATION:
            child_ctx["location"] = STATION

        if isinstance(child, (dict, list)):
            yield from _iter_candidate_records(child, child_ctx)


def parse_clouds_json(payload: Any) -> list[Observation]:
    parsed: list[Observation] = []
    seen: set[tuple[str, dt.datetime, float, int]] = set()

    for record in _iter_candidate_records(payload):
        variable = str(record.get("var") or _first(record, ("var", "variable", "parameter")) or "")
        if variable not in VARIABLES:
            continue

        location = record.get("location") or _first(record, ("location", "loc", "station", "station_id"))
        if location is not None and str(location).upper() != STATION:
            continue

        when = record.get("datetime")
        if not isinstance(when, dt.datetime):
            when = _parse_time(_first(record, ("datetime", "date_time", "valid_time", "obtime", "time", "timestamp")))
        if when is None:
            continue

        numeric = _as_float(record.get("value") if "value" in record else _first(record, ("value", "val", "observation")))
        if numeric is None:
            continue

        score_raw = _first(record, ("score", "qc_score", "qcscore", "qc"))
        score = _as_int(score_raw)
        flag_keys = {str(key).lower() for key in record}
        flag_present = any(key in flag_keys for key in ("flag", "qc_flag", "qcflag", "flags"))
        flag_raw = _first(record, ("flag", "qc_flag", "qcflag", "flags"))
        flag = None if flag_raw is None else str(flag_raw)

        unit_raw = _first(record, ("unit", "units"))
        if unit_raw is None:
            unit = "in" if variable == "precip1m" else "m3/m3"
        else:
            unit = str(unit_raw)

        if score is None:
            # Fail closed later if JSON does not expose QC metadata.
            continue

        key = (variable, when.astimezone(dt.UTC), numeric, score)
        if key in seen:
            continue
        seen.add(key)
        parsed.append(
            Observation(
                variable=variable,
                observed_at=when,
                value=numeric,
                unit=unit,
                qc_score=score,
                qc_flag=flag,
                flag_present=flag_present,
            )
        )

    return parsed


def _build_url(api_hash: str, start: dt.datetime, end: dt.datetime) -> str:
    # CLOUDS documents `attr` (including score/flag) as CSV/HTML-only.  JSON is
    # useful for ordinary values, but it does not expose the QC attributes needed by
    # this operational workflow.  Request long-form CSV internally, then publish the
    # derived result as JSON.
    params = {
        "hash": api_hash,
        "loc": f"location={STATION}",
        "var": "precip1m|in,soilmoist|m3/m3,soilmoist20cm|m3/m3",
        "start": start.astimezone(EASTERN).strftime("%Y-%m-%d %H:%M:%S"),
        "end": end.astimezone(EASTERN).strftime("%Y-%m-%d %H:%M:%S"),
        "int": "1 hour",
        "obtype": "H",
        "output": "csv_long",
        "qclimit": "1",
        "timezone": "US/Eastern",
        "order": "location,datetime",
        "attr": ",".join(CSV_ATTRS),
        "metadata": "no",
        "attr_delim": ";",
    }
    return f"{API_ENDPOINT}?{urllib.parse.urlencode(params)}"


def _fetch_csv(api_hash: str, start: dt.datetime, end: dt.datetime, retries: int = 4) -> str:
    url = _build_url(api_hash, start, end)
    retryable = {429, 500, 502, 503, 504}
    last_error: Exception | None = None

    for attempt in range(retries):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "morganton-electric-weather-support/1.1",
                "Accept": "text/csv,text/plain;q=0.9,*/*;q=0.1",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                return response.read().decode("utf-8-sig")
        except urllib.error.HTTPError as exc:
            # Do not stringify the exception because its URL contains the secret hash.
            last_error = RuntimeError(f"CLOUDS API returned HTTP {exc.code}")
            if exc.code not in retryable or attempt == retries - 1:
                raise last_error from None
        except urllib.error.URLError:
            last_error = RuntimeError("CLOUDS API network request failed")
            if attempt == retries - 1:
                raise last_error from None
        except (TimeoutError, UnicodeDecodeError):
            last_error = RuntimeError("CLOUDS API response timed out or was not valid UTF-8 CSV")
            if attempt == retries - 1:
                raise last_error from None
        time.sleep(2**attempt)

    raise last_error or RuntimeError("CLOUDS API request failed")


def _normalize_csv_name(value: str) -> str:
    return value.strip().strip('\ufeff').strip().lower().replace(" ", "_")


def _observation_from_record(record: dict[str, Any], *, flag_present: bool) -> Observation | None:
    variable_raw = str(_first(record, ("var", "variable", "parameter")) or "").strip()
    variable = variable_raw.split("|", 1)[0]
    if variable not in VARIABLES:
        return None

    location = str(_first(record, ("location", "loc", "station", "station_id")) or STATION).strip()
    if location.upper() != STATION:
        return None

    # obtime is the actual observation time; datetime is the requested interval time.
    # Prefer obtime for freshness calculations when the API provides it.
    observed_at = _parse_time(_first(record, ("obtime", "ob_time")))
    if observed_at is None:
        observed_at = _parse_time(_first(record, ("datetime", "date_time", "time", "timestamp")))
    if observed_at is None:
        return None

    value = _as_float(_first(record, ("value", "val", "observation")))
    score = _as_int(_first(record, ("score", "qc_score", "qcscore", "qc")))
    if value is None or score is None:
        return None

    unit_raw = _first(record, ("unit", "units"))
    unit = str(unit_raw).strip() if unit_raw not in (None, "") else ("in" if variable == "precip1m" else "m3/m3")
    flag_raw = _first(record, ("flag", "qc_flag", "qcflag", "flags"))
    flag = None if flag_raw is None else str(flag_raw)

    return Observation(
        variable=variable,
        observed_at=observed_at,
        value=value,
        unit=unit,
        qc_score=score,
        qc_flag=flag,
        flag_present=flag_present,
    )


def parse_clouds_csv(text: str) -> list[Observation]:
    """Parse CLOUDS csv_long output with explicit QC score/flag attributes.

    CLOUDS CSV output can appear either as ordinary long-form columns or as an
    attribute-packed cell separated by `attr_delim`.  Supporting both forms keeps
    this bridge tolerant of presentation differences without accepting records that
    lack an explicit QC score and flag field.
    """
    raw_lines = [
        line for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("##")
    ]
    if not raw_lines:
        return []

    rows = list(csv.reader(io.StringIO("\n".join(raw_lines))))
    parsed: list[Observation] = []
    seen: set[tuple[str, dt.datetime, float, int]] = set()

    # Form 1: a conventional header row followed by one observation per row.
    header_index: int | None = None
    header: list[str] = []
    for i, row in enumerate(rows[:10]):
        normalized = [_normalize_csv_name(cell) for cell in row]
        names = set(normalized)
        if {"var", "value", "score"}.issubset(names) and ({"datetime", "obtime"} & names):
            header_index = i
            header = normalized
            break

    if header_index is not None:
        flag_present_in_header = "flag" in header or "qc_flag" in header
        for row in rows[header_index + 1:]:
            if not row:
                continue
            padded = row + [""] * max(0, len(header) - len(row))
            record = {header[i]: padded[i] for i in range(len(header))}
            obs = _observation_from_record(record, flag_present=flag_present_in_header)
            if obs is None:
                continue
            key = (obs.variable, obs.observed_at.astimezone(dt.UTC), obs.value, obs.qc_score)
            if key not in seen:
                seen.add(key)
                parsed.append(obs)

    # Form 2: CSV cells containing attr-delimited values in the requested order.
    # Example shape: MORG;2026-08-21 09:00;soilmoist;0.31;m3/m3;0;;2026-08-21 09:00
    for row in rows:
        for cell in row:
            cell = cell.strip()
            if ";" not in cell:
                continue
            parts = [part.strip() for part in cell.split(";")]
            if len(parts) < 6:
                continue
            if _normalize_csv_name(parts[0]) == "location":
                continue
            padded = parts + [""] * (len(CSV_ATTRS) - len(parts))
            record = {CSV_ATTRS[i]: padded[i] for i in range(len(CSV_ATTRS))}
            obs = _observation_from_record(record, flag_present=len(parts) >= 7)
            if obs is None:
                continue
            key = (obs.variable, obs.observed_at.astimezone(dt.UTC), obs.value, obs.qc_score)
            if key not in seen:
                seen.add(key)
                parsed.append(obs)

    return sorted(parsed, key=lambda item: (item.observed_at, item.variable))


def _dedupe_accepted(observations: Iterable[Observation]) -> list[Observation]:
    best: dict[tuple[str, dt.datetime], Observation] = {}
    for obs in observations:
        if obs.qc_score not in QC_ACCEPTED:
            continue
        key = (obs.variable, obs.observed_at.astimezone(dt.UTC))
        current = best.get(key)
        if current is None or obs.qc_score < current.qc_score:
            best[key] = obs
    return sorted(best.values(), key=lambda item: item.observed_at)


def _qc_status(score: int) -> str:
    return "good" if score == 0 else "likely_good"


def _age_minutes(now: dt.datetime, observed_at: dt.datetime) -> float:
    return max(0.0, (now.astimezone(dt.UTC) - observed_at.astimezone(dt.UTC)).total_seconds() / 60.0)


def _closest(observations: list[Observation], target: dt.datetime, tolerance_minutes: float = 90.0) -> Observation | None:
    if not observations:
        return None
    closest = min(
        observations,
        key=lambda item: abs((item.observed_at.astimezone(dt.UTC) - target.astimezone(dt.UTC)).total_seconds()),
    )
    delta = abs((closest.observed_at.astimezone(dt.UTC) - target.astimezone(dt.UTC)).total_seconds()) / 60.0
    return closest if delta <= tolerance_minutes else None


def _percentile_rank(values: list[float], latest: float) -> float | None:
    if len(values) < 4:
        return None
    less = sum(value < latest for value in values)
    equal = sum(value == latest for value in values)
    return round(100.0 * (less + 0.5 * equal) / len(values), 1)


def _soil_summary(variable: str, observations: list[Observation], now: dt.datetime) -> dict[str, Any]:
    series = [obs for obs in observations if obs.variable == variable]
    if not series:
        return {
            "depth_cm": SOIL_DEPTHS_CM[variable],
            "units": "m3/m3",
            "status": "missing",
            "observation_time": None,
            "age_minutes": None,
            "qc_score": None,
            "qc_status": "unavailable",
            "qc_flag": None,
            "change_24h_m3m3": None,
            "change_reference_time": None,
            "seven_day_percentile": None,
            "current_value_published": False,
        }

    latest = max(series, key=lambda item: item.observed_at)
    age = _age_minutes(now, latest.observed_at)
    reference = _closest(series, latest.observed_at - dt.timedelta(hours=24))
    change = None if reference is None else round(latest.value - reference.value, 4)
    percentile = _percentile_rank([obs.value for obs in series], latest.value)

    return {
        "depth_cm": SOIL_DEPTHS_CM[variable],
        "units": latest.unit,
        "status": "current" if age <= MAX_CURRENT_AGE_MINUTES else "stale",
        "observation_time": latest.observed_at.isoformat(),
        "age_minutes": round(age, 1),
        "qc_score": latest.qc_score,
        "qc_status": _qc_status(latest.qc_score),
        "qc_flag": latest.qc_flag,
        "change_24h_m3m3": change,
        "change_reference_time": None if reference is None else reference.observed_at.isoformat(),
        "seven_day_percentile": percentile,
        "current_value_published": False,
    }


def _rainfall_period(
    observations: list[Observation],
    now: dt.datetime,
    hours: int,
) -> dict[str, Any]:
    precip = [obs for obs in observations if obs.variable == "precip1m"]
    start = now - dt.timedelta(hours=hours)
    in_window = [obs for obs in precip if start < obs.observed_at <= now]
    total = round(sum(obs.value for obs in in_window), 3)
    coverage = round(min(100.0, 100.0 * len(in_window) / hours), 1) if hours else 0.0
    latest = max(in_window, key=lambda item: item.observed_at) if in_window else None
    return {
        "total_inches": total,
        "window_hours": hours,
        "window_start": start.isoformat(),
        "window_end": now.isoformat(),
        "accepted_hourly_observations": len(in_window),
        "expected_hourly_observations": hours,
        "coverage_percent": coverage,
        "through_observation_time": None if latest is None else latest.observed_at.isoformat(),
        "qc_rule": "scores 0-1 only",
    }


def build_summary(observations: list[Observation], now: dt.datetime) -> dict[str, Any]:
    accepted = _dedupe_accepted(observations)
    by_var = {var: [obs for obs in accepted if obs.variable == var] for var in VARIABLES}

    missing_qc_flags = sorted(
        var
        for var in VARIABLES
        if by_var[var] and not any(obs.flag_present for obs in by_var[var])
    )
    if missing_qc_flags:
        raise RuntimeError(
            "CLOUDS CSV did not expose QC flag metadata for: " + ", ".join(missing_qc_flags)
        )

    latest_all = max(accepted, key=lambda item: item.observed_at) if accepted else None
    latest_age = None if latest_all is None else round(_age_minutes(now, latest_all.observed_at), 1)
    latest_current = latest_age is not None and latest_age <= MAX_CURRENT_AGE_MINUTES

    soil_10 = _soil_summary("soilmoist", accepted, now)
    soil_20 = _soil_summary("soilmoist20cm", accepted, now)

    required_current = [soil_10["status"] == "current", soil_20["status"] == "current"]
    status = "current" if latest_current and all(required_current) else "stale"
    if not accepted:
        status = "unavailable"

    return {
        "schema_version": 1,
        "extractor_version": __version__,
        "generated_at": now.astimezone(dt.UTC).isoformat(),
        "station": {
            "id": STATION,
            "name": "North Carolina School of Science and Math - Morganton",
            "county": "Burke",
            "state": "NC",
        },
        "source": {
            "provider": "North Carolina State Climate Office",
            "network": "ECONet",
            "api": "CLOUDS",
            "api_endpoint": API_ENDPOINT,
            "attribution": "Data provided by the North Carolina State Climate Office",
        },
        "status": status,
        "freshness": {
            "latest_accepted_observation_time": None if latest_all is None else latest_all.observed_at.isoformat(),
            "age_minutes": latest_age,
            "maximum_current_age_minutes": MAX_CURRENT_AGE_MINUTES,
            "within_limit": bool(latest_current),
        },
        "quality_control": {
            "accepted_scores": [0, 1],
            "rejected_scores": [-1, 2, 3],
            "api_qclimit": 1,
            "flag_metadata_verified": not missing_qc_flags,
        },
        "rainfall": {
            "units": "in",
            "24h": _rainfall_period(accepted, now, 24),
            "72h": _rainfall_period(accepted, now, 72),
            "168h": _rainfall_period(accepted, now, 168),
        },
        "soil_moisture": {
            "10cm": soil_10,
            "20cm": soil_20,
        },
        "fallback_policy": {
            "if_morg_unavailable": "Nearby rainfall observations may be used as context only.",
            "soil_moisture_substitution": "Do not substitute another station's soil moisture for MORG.",
        },
        "notes": [
            "Only CLOUDS observations with QC score 0 or 1 are used.",
            "Current MORG soil-moisture context is considered usable only when the latest accepted observation is no more than 90 minutes old.",
            "Rolling rainfall totals and 24-hour soil-moisture changes are derived from accepted hourly observations.",
            "The public JSON does not redistribute raw hourly observations or exact current soil-moisture values; it publishes derived context, freshness, depth, units, and QC status.",
            "MORG sensor inventory identifies soil-moisture sensors at 10 cm and 20 cm; this extractor maps soilmoist to 10 cm and soilmoist20cm to 20 cm for MORG.",
        ],
    }


def unavailable_summary(now: dt.datetime, reason: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "extractor_version": __version__,
        "generated_at": now.astimezone(dt.UTC).isoformat(),
        "station": {"id": STATION, "county": "Burke", "state": "NC"},
        "source": {
            "provider": "North Carolina State Climate Office",
            "network": "ECONet",
            "api": "CLOUDS",
            "attribution": "Data provided by the North Carolina State Climate Office",
        },
        "status": "unavailable",
        "reason": reason,
        "fallback_policy": {
            "if_morg_unavailable": "Nearby rainfall observations may be used as context only.",
            "soil_moisture_substitution": "Do not substitute another station's soil moisture for MORG.",
        },
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--output", type=Path, default=Path("morg-econet.json"))
    result.add_argument("--now", type=_parse_time, help="override current time for reproducibility")
    result.add_argument("--hash-env", default="NCSCO_CLOUDS_HASH")
    result.add_argument("--strict", action="store_true", help="fail instead of publishing unavailable status on transient API errors")
    result.add_argument("--version", action="version", version=__version__)
    return result


def run(args: argparse.Namespace) -> dict[str, Any]:
    now = args.now or dt.datetime.now(dt.UTC)
    if not isinstance(now, dt.datetime):
        raise RuntimeError("invalid --now value")
    api_hash = os.environ.get(args.hash_env, "").strip()
    if not api_hash:
        raise RuntimeError(f"required secret environment variable {args.hash_env} is not set")

    start = now - dt.timedelta(hours=168)
    try:
        payload = _fetch_csv(api_hash, start, now)
    except RuntimeError as exc:
        if args.strict:
            raise
        return unavailable_summary(now, str(exc))

    observations = parse_clouds_csv(payload)
    if not observations:
        nonempty = [line for line in payload.splitlines() if line.strip()]
        raise RuntimeError(
            "CLOUDS CSV contained no parseable observations with explicit QC score/flag metadata; "
            f"response_lines={len(nonempty)}"
        )

    # Ensure all requested variables are represented after CSV parsing. This catches
    # API/schema changes rather than silently generating a misleading partial summary.
    present = {obs.variable for obs in observations}
    missing = sorted(set(VARIABLES) - present)
    if missing:
        raise RuntimeError("CLOUDS CSV missing requested variables: " + ", ".join(missing))

    return build_summary(observations, now)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = run(args)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {args.output} ({result.get('status', 'unknown')})")
        return 0
    except Exception as exc:
        print(f"morg-econet: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
