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


# Conservative PILOT screening thresholds; NOT observed tree-failure probabilities.
# 72-hour rainfall bands align with the Hub's existing rainfall-vulnerability
# thresholds. 168-hour persistence and supported soil-wetting gates are new
# screening choices requiring local historical outage/tree-damage validation.
AUTO_SCREEN_VERSION = "pilot-moisture-screen-v1"
MIN_RAIN_COVERAGE_PCT = 90.0
ELEVATED_RAINFALL_72_IN = 2.0
ELEVATED_RAINFALL_168_IN = 3.0
HIGH_RAINFALL_72_IN = 5.0
HIGH_COMPOUND_RAINFALL_24_IN = 3.0
HIGH_COMPOUND_RAINFALL_72_IN = 3.0
HIGH_COMPOUND_RAINFALL_168_IN = 5.0
CORROBORATING_RAINFALL_72_IN = 1.0
CORROBORATING_SOIL_CHANGE_24H = 0.02
CORROBORATING_WEEK_RANK = 75.0


def automated_vegetation_context(
    now: dt.datetime, econet_source: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Derive a provisional *antecedent moisture* impact screening result.

    This is NOT a prediction of tree failure or an absolute soil saturation
    estimate. The seven_day_percentile is a rank within the *previous week of
    samples*, not the NC State Climate Office's long-term soil climatology.
    It is allowed to corroborate a rainfall/wetting signal, never to trigger
    Elevated/High by itself.

    Evaluate only derived, QC-accepted observations from today's MORG source.
    A last-known same-day result can remain visible with a STALE source label;
    it is never represented as a fresh/complete observation.
    """
    local_now = now.astimezone(EASTERN)
    status = econet_source.get("status", "Unavailable")

    def reject(reason: str, *, unavailable: bool = True
               ) -> tuple[None, dict[str, Any]]:
        return None, {
            "status": "Unavailable" if unavailable else "Not Assessed",
            "notes": "Automated vegetation-moisture screen not established: " + reason,
        }

    if status not in ("Available", "Stale"):
        return reject("MORG ECONet source unavailable.")
    context = econet_source.get("derived_context")
    if not isinstance(context, dict) or context.get("station_id") != "MORG":
        return reject("MORG station-derived measurements not verified.")
    if context.get("qc_metadata_verified") is not True:
        return reject("ECONet quality-control metadata unavailable.")

    retrieved = _time(econet_source.get("retrieved_at"))
    last_observation = _time(econet_source.get("observation_time"))
    if retrieved is None or last_observation is None:
        return reject("missing or invalid source timestamps.")
    if retrieved.astimezone(EASTERN).date() != local_now.date():
        return reject("source file is not from this Eastern calendar date.",
                      unavailable=False)
    if retrieved > now + dt.timedelta(minutes=5):
        return reject("source was published in the future.")
    age_when_generated = (retrieved - last_observation).total_seconds() / 60
    if not 0 <= age_when_generated <= MAX_CURRENT_AGE_MINUTES:
        return reject("source observation was already outside 180-minute freshness at publication.")
    age_now = (now - last_observation).total_seconds() / 60
    if age_now < -5:
        return reject("source observation time is in the future.")
    if status == "Available" and age_now > MAX_CURRENT_AGE_MINUTES:
        return reject("source health is inconsistent with observation age.")

    rainfall: dict[str, float] = {}
    for hours in (24, 72, 168):
        value = _number(context.get(f"rainfall_{hours}_in"))
        coverage = _number(context.get(f"rainfall_{hours}_coverage_percent"))
        if value is None or not 0 <= value <= 100 or (
            coverage is None or not MIN_RAIN_COVERAGE_PCT <= coverage <= 100
        ):
            return reject(f"{hours}-hour rainfall amount or coverage is insufficient.")
        rainfall[str(hours)] = value

    soil = context.get("soil_moisture")
    if not isinstance(soil, dict):
        return reject("soil-moisture metadata missing.")
    weekly_rank: dict[str, float] = {}
    change_24h: dict[str, float] = {}
    for depth in ("10cm", "20cm"):
        datum = soil.get(depth)
        if not isinstance(datum, dict):
            return reject(f"{depth} soil-moisture context unavailable.")
        sample_time = _time(datum.get("observation_time"))
        rank = _number(datum.get("seven_day_percentile"))
        change = _number(datum.get("change_24h_m3m3"))
        if (
            datum.get("status") != "current"
            or type(datum.get("qc_score")) is not int
            or datum["qc_score"] not in (0, 1)
            or datum.get("qc_status") not in ("good", "likely_good")
            or sample_time is None
            or rank is None or not 0 <= rank <= 100
            or change is None or not -1 <= change <= 1
            or not 0 <= (retrieved - sample_time).total_seconds() / 60 <= MAX_CURRENT_AGE_MINUTES
        ):
            return reject(f"{depth} observation, rank, QC, or 24-hour trend invalid.")
        weekly_rank[depth] = rank
        change_24h[depth] = change

    rain24, rain72, rain168 = (
        rainfall["24"], rainfall["72"], rainfall["168"]
    )
    soil_wetting = all(
        weekly_rank[d] >= CORROBORATING_WEEK_RANK
        and change_24h[d] >= CORROBORATING_SOIL_CHANGE_24H
        for d in ("10cm", "20cm")
    )
    if (
        rain72 >= HIGH_RAINFALL_72_IN
        or (
            rain24 >= HIGH_COMPOUND_RAINFALL_24_IN
            and rain72 >= HIGH_COMPOUND_RAINFALL_72_IN
            and rain168 >= HIGH_COMPOUND_RAINFALL_168_IN
        )
    ):
        classification = "High"
        trigger = "exceptionally heavy recent rainfall"
    elif (
        rain72 >= ELEVATED_RAINFALL_72_IN
        or rain168 >= ELEVATED_RAINFALL_168_IN
        or (rain72 >= CORROBORATING_RAINFALL_72_IN and soil_wetting)
    ):
        classification = "Elevated"
        trigger = (
            "recent/persistent antecedent rainfall"
            if rain72 >= ELEVATED_RAINFALL_72_IN
            or rain168 >= ELEVATED_RAINFALL_168_IN
            else "rainfall plus corroborating relative soil wetting"
        )
    else:
        classification = "Normal"
        trigger = "no elevated rainfall-plus-wetting screening signal"

    freshness_note = (
        "MORG observations are now STALE; this is the last known same-day "
        "screen, not an updated live assessment."
        if status == "Stale"
        else "MORG source observations are within the 180-minute policy."
    )
    basis = (
        f"Provisional antecedent-moisture screen ({AUTO_SCREEN_VERSION}): "
        f"24h rainfall {rain24:.2f} in; 72h {rain72:.2f} in; "
        f"168h {rain168:.2f} in. 10/20-cm 24h changes "
        f"{change_24h['10cm']:+.3f}/{change_24h['20cm']:+.3f} m3/m3; "
        f"within-week relative ranks {weekly_rank['10cm']:.1f}/"
        f"{weekly_rank['20cm']:.1f}. Trigger: {trigger}. "
        "Within-week ranks are NOT climatological soil-saturation percentiles. "
        "A Normal screen does not certify root stability or rule out localized "
        "saturated ground, erosion, or hazardous trees. "
        f"{freshness_note}"
    )
    assessment: dict[str, Any] = {
        "status": "Provisional",
        "classification": classification,
        "confidence": "Moderate" if status == "Available" else "Low",
        "valid_date": local_now.date().isoformat(),
        "issued_at": retrieved.isoformat(),
        "source_run": "Automated MORG ECONet screen",
        "basis": basis,
        "source_freshness_at_snapshot": status,
        "methodology": (
            "Rule-based provisional antecedent moisture screening; not a "
            "calibrated tree-failure probability or full tree-failure composite"
        ),
        "methodology_version": AUTO_SCREEN_VERSION,
        "screening_only": True,
        "soil_percentile_reference": "rolling_previous_7_days_not_climatology",
        "affects_meteorological_downburst_index": False,
        "affects_calculated_posture": False,
    }
    source: dict[str, Any] = {
        "status": status,
        "observation_time": last_observation.isoformat(),
        "retrieved_at": retrieved.isoformat(),
        "assessment_time": retrieved.isoformat(),
        "notes": (
            f"Automated {AUTO_SCREEN_VERSION}: provisional {classification} "
            f"antecedent-moisture signal; {freshness_note} "
            "Site-specific tree failure is NOT predicted and calculated "
            "risk/posture remain unchanged."
        ),
    }
    return assessment, source
