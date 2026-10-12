from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


EASTERN = ZoneInfo("America/New_York")

ALLOWED_VERDICTS = {"LOW", "CONDITIONAL", "ELEVATED", "HIGH"}
ALLOWED_CONFIDENCE = {"Low", "Moderate", "High"}
ALLOWED_SPC = {
    "General",
    "Thunderstorms",
    "Marginal",
    "Slight",
    "Enhanced",
    "Moderate",
    "High",
}
ALLOWED_TRENDS = {"Lower", "Similar", "Higher"}

VERDICT_BANDS = (
    (0, 24, "LOW"),
    (25, 44, "CONDITIONAL"),
    (45, 64, "ELEVATED"),
    (65, 100, "HIGH"),
)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _parse_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _current_eastern() -> dt.datetime:
    return dt.datetime.now(dt.UTC).astimezone(EASTERN)


def _expected_verdict(index: int) -> str:
    for low, high, verdict in VERDICT_BANDS:
        if low <= index <= high:
            return verdict
    raise ValueError("downburst_index out of range")


def _version_tuple(value: Any) -> tuple[int, ...]:
    if not isinstance(value, str):
        return ()
    parts: list[int] = []
    for token in value.split("."):
        try:
            parts.append(int(token))
        except ValueError:
            break
    return tuple(parts)


def _econet_context(repo_root: Path, run: str, today: dt.date) -> dict[str, Any]:
    name = "morg-econet-0830.json" if run == "initial" else "morg-econet-1030.json"
    path = repo_root / "current" / name
    data = _read_json(path)
    if data is None:
        return {
            "file": name,
            "usable": False,
            "reason": "file missing or malformed",
        }

    generated = _parse_time(data.get("generated_at"))
    latest_obs = _parse_time(
        (data.get("freshness") or {}).get("latest_accepted_observation_time")
    )
    freshness = data.get("freshness") or {}
    qc = data.get("quality_control") or {}
    station = data.get("station") or {}

    reasons: list[str] = []
    if station.get("id") != "MORG":
        reasons.append("station.id is not MORG")
    if generated is None or generated.astimezone(EASTERN).date() != today:
        reasons.append("generated_at is not from the current Eastern date")
    if data.get("status") != "current":
        reasons.append("status is not current")
    if freshness.get("within_limit") is not True:
        reasons.append("freshness.within_limit is not true")
    if qc.get("flag_metadata_verified") is not True:
        reasons.append("quality_control.flag_metadata_verified is not true")
    if latest_obs is None:
        reasons.append("latest accepted observation time is unavailable")

    return {
        "file": name,
        "usable": not reasons,
        "reason": "; ".join(reasons) if reasons else "validated",
        "generated_at": data.get("generated_at"),
        "station": station,
        "status": data.get("status"),
        "freshness": freshness,
        "quality_control": qc,
        "rainfall": data.get("rainfall"),
        "soil_moisture": data.get("soil_moisture"),
        "notes": data.get("notes"),
    }


def _hrrr_context(repo_root: Path, today: dt.date) -> dict[str, Any]:
    path = repo_root / "current" / "burke-hrrr-12z.json"
    data = _read_json(path)
    if data is None:
        return {
            "file": "burke-hrrr-12z.json",
            "usable": False,
            "reason": "file missing or malformed",
        }

    generated = _parse_time(data.get("generated_at"))
    published = _parse_time(data.get("snapshot_published_at"))
    cycle = _parse_time(data.get("cycle_initialized"))
    through = _parse_time(data.get("through"))

    reasons: list[str] = []
    if _version_tuple(data.get("extractor_version")) < (0, 3, 2):
        reasons.append("extractor_version is older than 0.3.2")
    if cycle is None or cycle.astimezone(EASTERN).date() != today:
        reasons.append("cycle_initialized is not today's cycle")
    if cycle is not None and cycle.astimezone(dt.UTC).hour != 12:
        reasons.append("cycle_initialized is not 12Z")
    if generated is None or generated.astimezone(EASTERN).date() != today:
        reasons.append("generated_at is not current")
    if published is None or published.astimezone(EASTERN).date() != today:
        reasons.append("snapshot_published_at is not current")
    if through is None or through.astimezone(EASTERN).date() < today:
        reasons.append("through does not cover the current date")

    operational = data.get("operational_summary")
    if not isinstance(operational, dict):
        reasons.append("operational_summary is missing")

    return {
        "file": "burke-hrrr-12z.json",
        "usable": not reasons,
        "reason": "; ".join(reasons) if reasons else "validated",
        "extractor_version": data.get("extractor_version"),
        "cycle_initialized": data.get("cycle_initialized"),
        "generated_at": data.get("generated_at"),
        "snapshot_published_at": data.get("snapshot_published_at"),
        "through": data.get("through"),
        "forecast_hour_count": data.get("forecast_hour_count"),
        "operational_summary": operational,
    }


def _prior_handoff(repo_root: Path, today: dt.date) -> dict[str, Any] | None:
    data = _read_json(repo_root / "current" / "downburst-outlook.json")
    if data is None:
        return None
    if data.get("valid_date") != today.isoformat():
        return None
    return data


def _is_run_already_complete(
    prior: dict[str, Any] | None,
    run: str,
) -> bool:
    if prior is None:
        return False
    source = prior.get("source_run")
    if run == "initial":
        return source in {"8:30 AM Initial", "11:00 AM Model Refresh"}
    return source == "11:00 AM Model Refresh"


def _response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "downburst_index",
            "verdict",
            "confidence",
            "trend",
            "most_likely_window",
            "spc_category",
            "spc_issued_at",
            "index_basis",
        ],
        "properties": {
            "downburst_index": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
            },
            "verdict": {
                "type": "string",
                "enum": ["LOW", "CONDITIONAL", "ELEVATED", "HIGH"],
            },
            "confidence": {
                "type": "string",
                "enum": ["Low", "Moderate", "High"],
            },
            "trend": {
                "anyOf": [
                    {
                        "type": "string",
                        "enum": ["Lower", "Similar", "Higher"],
                    },
                    {"type": "null"},
                ]
            },
            "most_likely_window": {"type": "string"},
            "spc_category": {
                "type": "string",
                "enum": [
                    "General",
                    "Thunderstorms",
                    "Marginal",
                    "Slight",
                    "Enhanced",
                    "Moderate",
                    "High",
                ],
            },
            "spc_issued_at": {
                "anyOf": [{"type": "string"}, {"type": "null"}]
            },
            "index_basis": {"type": "string"},
        },
    }


def _system_instructions(run: str) -> str:
    run_name = "8:30 AM Initial" if run == "initial" else "11:00 AM Model Refresh"
    return f"""
You are producing the machine-readable Daily Downburst Model handoff for the
City of Morganton Electric Department Operational Readiness Hub.

This is the {run_name}. Assess Morganton and Burke County, North Carolina,
for late morning through midnight local time.

Use the supplied local repository data as authoritative for those files. Use
web search only for current official meteorological context, prioritizing:
- NWS Greenville-Spartanburg forecasts, discussions, warnings and observations
- SPC Day 1 Convective Outlook and mesoanalysis
- current radar/observational context from authoritative NOAA/NWS sources

Do not use antecedent rainfall, soil moisture, or tree vulnerability to raise
the meteorological Downburst Index. Those are impact modifiers only.

The Downburst Index is a granular expression of the SAME meteorological
synthesis used to assign the verdict:
- LOW = 0-24
- CONDITIONAL = 25-44
- ELEVATED = 45-64
- HIGH = 65-100

Use the lower third of a verdict band for weak or marginal support, the middle
third for a representative signal, and the upper third only when multiple
independent meteorological signals strongly support that verdict or conditions
are close to the next category.

For HIGH, reserve 80-100 for unusually strong, well-corroborated damaging
downburst environments. Values 90-100 should be rare.

Do not let one isolated county-max model spike control the number. Compare
county maxima with p90/coverage and with convective reflectivity/updraft/
downdraft signals. Missing or conflicting evidence should lower confidence.

For the 11:00 refresh, give meaningful weight to validated 12Z HRRR guidance
but use the full synthesis rather than one model field. Treat
bulk_shear_sfc_500mb_kt only as a 10 m-to-500 mb deep-layer organization
proxy, not exact 0-6 km bulk shear.

spc_category must represent the current SPC categorical outlook covering
Morganton/Burke County. Use General when the point is only within general
thunderstorms/no higher categorical severe risk.

Return only the structured object required by the response schema.
""".strip()


def _user_prompt(
    run: str,
    now: dt.datetime,
    econet: dict[str, Any],
    hrrr: dict[str, Any] | None,
    prior: dict[str, Any] | None,
) -> str:
    run_name = "8:30 AM Initial" if run == "initial" else "11:00 AM Model Refresh"

    context = {
        "run": run_name,
        "current_eastern_time": now.isoformat(),
        "valid_date": now.date().isoformat(),
        "morg_econet": econet,
        "prior_same_day_handoff": prior,
    }
    if run == "refresh":
        context["hrrr_12z"] = hrrr

    trend_rule = (
        "trend must be null for the Initial run."
        if run == "initial"
        else (
            "trend must be Lower, Similar, or Higher compared with the supplied "
            "same-day prior handoff when one exists. If no same-day prior handoff "
            "exists, use Similar as a neutral operational placeholder and explain "
            "the present environment in index_basis without claiming a measured "
            "morning change."
        )
    )

    return (
        "Produce the current Morganton/Burke County Downburst handoff.\n\n"
        f"{trend_rule}\n\n"
        "Repository context follows. A source marked usable=false must not be "
        "treated as current evidence. Explicitly lower confidence when important "
        "meteorological evidence is missing or conflicting.\n\n"
        + json.dumps(context, indent=2, ensure_ascii=False)
    )


def _call_openai(
    api_key: str,
    model: str,
    system_text: str,
    user_text: str,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "reasoning": {"effort": "medium"},
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": system_text}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": user_text}],
            },
        ],
        "tools": [
            {
                "type": "web_search",
                "search_context_size": "medium",
                "filters": {
                    "allowed_domains": [
                        "weather.gov",
                        "spc.noaa.gov",
                        "noaa.gov",
                    ]
                },
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "morganton_downburst_handoff",
                "strict": True,
                "schema": _response_schema(),
            }
        },
        "max_output_tokens": 2500,
        "store": False,
    }

    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"OpenAI Responses API HTTP {exc.code}: {detail[:2000]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenAI Responses API request failed: {exc}") from exc

    response_json = json.loads(raw)
    if response_json.get("status") != "completed":
        raise RuntimeError(
            "OpenAI response did not complete: "
            + json.dumps(
                {
                    "status": response_json.get("status"),
                    "error": response_json.get("error"),
                    "incomplete_details": response_json.get("incomplete_details"),
                }
            )
        )

    output_text: str | None = None
    for item in response_json.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                output_text = content.get("text")
                break
        if output_text is not None:
            break

    if not output_text:
        raise RuntimeError("OpenAI response contained no output_text")

    value = json.loads(output_text)
    if not isinstance(value, dict):
        raise RuntimeError("OpenAI structured output was not an object")
    return value


def _validate_model_result(value: dict[str, Any], run: str) -> dict[str, Any]:
    index = value.get("downburst_index")
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 100:
        raise ValueError("downburst_index must be an integer from 0 through 100")

    verdict = value.get("verdict")
    if verdict not in ALLOWED_VERDICTS:
        raise ValueError("verdict is invalid")
    expected = _expected_verdict(index)
    if verdict != expected:
        raise ValueError(
            f"verdict/index mismatch: index {index} requires {expected}"
        )

    confidence = value.get("confidence")
    if confidence not in ALLOWED_CONFIDENCE:
        raise ValueError("confidence is invalid")

    trend = value.get("trend")
    if run == "initial":
        if trend is not None:
            raise ValueError("Initial trend must be null")
    elif trend not in ALLOWED_TRENDS:
        raise ValueError("Refresh trend must be Lower, Similar, or Higher")

    window = value.get("most_likely_window")
    if not isinstance(window, str) or not window.strip():
        raise ValueError("most_likely_window must be a non-empty string")

    spc = value.get("spc_category")
    if spc not in ALLOWED_SPC:
        raise ValueError("spc_category is invalid")

    spc_issued = value.get("spc_issued_at")
    if spc_issued is not None:
        parsed = _parse_time(spc_issued)
        if parsed is None:
            raise ValueError("spc_issued_at must be offset-aware or null")

    basis = value.get("index_basis")
    if not isinstance(basis, str) or not basis.strip():
        raise ValueError("index_basis must be a non-empty string")

    return {
        "downburst_index": index,
        "verdict": verdict,
        "confidence": confidence,
        "trend": trend,
        "most_likely_window": window.strip(),
        "spc_category": spc,
        "spc_issued_at": spc_issued,
        "index_basis": basis.strip(),
    }


def build_handoff(
    repo_root: Path,
    run: str,
    model: str,
    api_key: str,
    force: bool = False,
) -> tuple[dict[str, Any] | None, str]:
    now = _current_eastern()
    today = now.date()
    prior = _prior_handoff(repo_root, today)

    if not force and _is_run_already_complete(prior, run):
        source = prior.get("source_run") if prior else "unknown"
        return None, f"same-day handoff already satisfies {run}: {source}"

    econet = _econet_context(repo_root, run, today)
    hrrr = _hrrr_context(repo_root, today) if run == "refresh" else None

    result = _call_openai(
        api_key=api_key,
        model=model,
        system_text=_system_instructions(run),
        user_text=_user_prompt(run, now, econet, hrrr, prior),
    )
    clean = _validate_model_result(result, run)

    source_run = "8:30 AM Initial" if run == "initial" else "11:00 AM Model Refresh"
    notes = (
        "8:30 AM Daily Downburst Model handoff for the Operational Readiness Hub."
        if run == "initial"
        else "11:00 AM Daily Downburst Model handoff for the Operational Readiness Hub."
    )

    handoff = {
        "schema_version": 1,
        **clean,
        "source_run": source_run,
        "valid_date": today.isoformat(),
        "issued_at": now.isoformat(),
        "handoff_status": "Automated reviewed handoff",
        "notes": notes,
    }
    return handoff, "generated"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--run",
        choices=("initial", "refresh"),
        required=True,
    )
    parser.add_argument(
        "--output",
        default="current/downburst-outlook.json",
    )
    parser.add_argument(
        "--force",
        action="store_true",
    )
    parser.add_argument(
        "--repo-root",
        default=".",
    )
    args = parser.parse_args(argv)

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        print(
            "::error::OPENAI_API_KEY is not configured. "
            "Add it as a GitHub Actions repository secret.",
            file=sys.stderr,
        )
        return 2

    model = os.environ.get("OPENAI_MODEL", "gpt-5.1").strip() or "gpt-5.1"
    repo_root = Path(args.repo_root).resolve()

    try:
        handoff, status = build_handoff(
            repo_root=repo_root,
            run=args.run,
            model=model,
            api_key=api_key,
            force=args.force,
        )
    except Exception as exc:
        print(f"::error::Downburst handoff generation failed: {exc}", file=sys.stderr)
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
                "status": status,
                "model": model,
                "output": str(output),
                "source_run": handoff["source_run"],
                "valid_date": handoff["valid_date"],
                "downburst_index": handoff["downburst_index"],
                "verdict": handoff["verdict"],
                "confidence": handoff["confidence"],
                "spc_category": handoff["spc_category"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
