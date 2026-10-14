"""Time-bounded independent vegetation-impact assessment for the Hub.

This is a qualitative briefing handoff, not a meteorological Downburst Index,
validated root-failure probability, or operational posture recommendation.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .econet import MAX_CURRENT_AGE_MINUTES

EASTERN = ZoneInfo("America/New_York")


def _time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def evaluated_vegetation_context(
    repo_root: Path, now: dt.datetime, econet_source: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Verify dated briefing judgment against derived ECONet evidence.

    A review remains visible, with a Stale warning, if the observations have
    aged since the *time of the review*. Expire the judgment after its date.
    Do not recompute the label from uncalibrated rainfall thresholds.
    """
    path = repo_root / "current" / "vegetation-vulnerability.json"
    try:
        handoff = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        handoff = None
    missing = {
        "status": "Not Assessed",
        "notes": "No current dated independent vegetation vulnerability briefing handoff.",
    }
    if handoff is None:
        return None, missing
    if not isinstance(handoff, dict):
        return None, {"status": "Unavailable", "notes": "Malformed vulnerability handoff."}
    if handoff.get("valid_date") != now.astimezone(EASTERN).date().isoformat():
        return None, missing

    def reject(reason: str) -> tuple[None, dict[str, Any]]:
        return None, {"status": "Unavailable", "notes": "Vegetation handoff rejected: " + reason}

    issued = _time(handoff.get("issued_at"))
    if (
        handoff.get("schema_version") != 1
        or handoff.get("source_run") not in ("8:30 AM Initial", "11:00 AM Model Refresh")
        or handoff.get("classification") not in ("Normal", "Elevated", "High")
        or handoff.get("confidence") not in ("Low", "Moderate", "High")
        or issued is None
        or issued.astimezone(EASTERN).date() != now.astimezone(EASTERN).date()
        or issued > now + dt.timedelta(minutes=5)
    ):
        return reject("schema, assessment classification, or timestamp invalid.")

    evidence = handoff.get("evidence") or {}
    ctx = econet_source.get("derived_context") or {}
    if not isinstance(evidence, dict) or ctx.get("station_id") != "MORG":
        return reject("MORG source or supporting evidence missing.")
    if ctx.get("qc_metadata_verified") is not True:
        return reject("ECONet source QC not verified.")
    r_now = _number(ctx.get("rainfall_168_in"))
    r_reported = _number(evidence.get("rainfall_168_in"))
    if r_now is None or r_reported is None or abs(r_now - r_reported) > 0.05:
        return reject("168-hour rainfall no longer matches the reviewed evidence.")

    soil = ctx.get("soil_moisture") or {}
    for depth in ("10cm", "20cm"):
        d = soil.get(depth) or {}
        p_now = _number(d.get("seven_day_percentile"))
        p_reviewed = _number(evidence.get(f"soil_{depth}_seven_day_percentile"))
        observed = _time(d.get("observation_time"))
        if (
            d.get("status") != "current"
            or type(d.get("qc_score")) is not int
            or d["qc_score"] not in (0, 1)
            or d.get("qc_status") not in ("good", "likely_good")
            or p_now is None or p_reviewed is None
            or abs(p_now - p_reviewed) > 0.11
            or observed is None
            or not 0 <= (issued - observed).total_seconds() / 60 <= MAX_CURRENT_AGE_MINUTES
        ):
            return reject(f"{depth} QC/age/percentile does not support the briefing.")

    freshness_now = econet_source.get("status", "Unavailable")
    assessment = {
        "classification": handoff["classification"],
        "confidence": handoff["confidence"],
        "status": "Provisional",
        "valid_date": handoff["valid_date"],
        "issued_at": issued.isoformat(),
        "source_run": handoff["source_run"],
        "basis": handoff.get("basis", ""),
        "source_freshness_at_snapshot": freshness_now,
        "methodology": "Independent qualitative tree/utility-impact briefing assessment",
        "affects_meteorological_downburst_index": False,
        "affects_calculated_posture": False,
    }
    source = {
        "status": freshness_now if freshness_now in ("Available", "Stale") else "Unavailable",
        "observation_time": econet_source.get("observation_time"),
        "retrieved_at": econet_source.get("retrieved_at"),
        "assessment_time": issued.isoformat(),
        "notes": (
            f"Provisional {handoff['classification']} tree/utility vulnerability "
            f"from independent {handoff['source_run']} briefing. Derived ECONet "
            f"evidence and source QC verified as of review; ECONet status now "
            f"{freshness_now}. No automatic risk/posture escalation."
        ),
    }
    return assessment, source
