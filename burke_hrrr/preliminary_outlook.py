"""An informational morning planning preview from already-assessed hazard inputs.

The preview is not a calculated forecast-risk level or a recommended posture.
It makes existing conservative planning floors legible when today's Downburst
and SPC handoff has not yet been completed.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
LEVEL_LABELS = ("Normal", "Awareness", "Elevated", "Storm Posture", "Major Event")
REQUIRED_DAY_FIELDS = (
    "wind_gust_mph",
    "qpf_24_in",
    "min_temp_f",
    "snowfall_in",
    "ice_accretion_in",
)


def _valid_level(value: Any) -> int | None:
    if type(value) is int and 0 <= value <= 4:
        return value
    return None


def make_preliminary_outlook(
    now: dt.datetime,
    severe: dict[str, Any],
    sources: dict[str, Any],
    daily: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Present current known-input floors, never a definitive risk or posture.

    Only for a missing/stale current Downburst/SPC handoff. If a present-day
    Downburst handoff is available, the normal Hub risk interface owns display.
    The NWS forecast and latest permitted ECONet observations are from today's
    snapshot, never yesterday's meteorological Downburst score.
    """
    downburst = sources.get("downburst_workflow", {})
    handoff_status = downburst.get("status", "Unavailable")
    if handoff_status == "Available" or not daily:
        return None
    if handoff_status not in ("Unavailable", "Stale"):
        return None
    local_now = now.astimezone(EASTERN)
    today = local_now.date().isoformat()
    first_day = daily[0]
    if first_day.get("forecast_date") != today:
        return None

    near_term = daily[:3]
    usable = [
        d for d in near_term if _valid_level(d.get("overall_level")) is not None
        and any(d.get(key) is not None for key in REQUIRED_DAY_FIELDS)
    ]
    winning = max(
        usable, key=lambda d: _valid_level(d["overall_level"]) or 0
    ) if usable else None

    available_nws = sources.get("nws_nbm", {}).get("status") == "Available"
    rainfall_available = (
        severe.get("rainfall_72_in") is not None
        and sources.get("econet", {}).get("status") == "Available"
    )
    all_nws_fields = (
        available_nws and len(usable) == len(near_term)
        and all(all(d.get(key) is not None for key in REQUIRED_DAY_FIELDS)
                for d in near_term)
    )
    status = (
        "Before initial model"
        if local_now.hour < 9 and handoff_status == "Unavailable"
        else "Late model refresh missing" if handoff_status == "Stale"
        else "Current model missing or delayed"
    )
    if winning is None or not available_nws:
        floor = {
            "level": None,
            "label": "Not established",
            "headline": "Known-input planning floor unavailable",
            "primary_hazard": None,
            "period": None,
            "window_start": None,
            "window_end": None,
        }
    else:
        level = _valid_level(winning["overall_level"])
        assert level is not None
        headline = (
            f"At least {LEVEL_LABELS[level]} — Partial"
            if level > 0
            else "No elevated signal in assessed inputs — Partial"
        )
        floor = {
            "level": level,
            "label": LEVEL_LABELS[level],
            "headline": headline,
            "primary_hazard": winning.get("primary_hazard"),
            "period": winning.get("label"),
            "window_start": winning.get("window_start"),
            "window_end": winning.get("window_end"),
        }

    pending = ["Today's Downburst Index and SPC handoff"]
    if not available_nws:
        pending.append("Current NWS Digital Forecast/grid")
    if not rainfall_available:
        pending.append("Fresh MORG ECONet antecedent observations")
    if sources.get("nws_alerts", {}).get("status") != "Available":
        pending.append("NWS alert applicability")

    return {
        "status": "Preliminary",
        "phase": status,
        "generated_at": local_now.isoformat(),
        "valid_date": today,
        "evaluation_window": "Next 72 hours (first three supplied daily periods)",
        "known_input_planning_floor": floor,
        "current_day_inputs": {
            "forecast_gust_mph": first_day.get("wind_gust_mph"),
            "forecast_qpf_24_in": first_day.get("qpf_24_in"),
            "minimum_temperature_f": first_day.get("min_temp_f"),
            "antecedent_rainfall_72_in": severe.get("rainfall_72_in"),
            "antecedent_rainfall_168_in": severe.get("rainfall_168_in"),
            "nws_alert": severe.get("nws_alert"),
        },
        "source_health": {
            "nws_forecast": sources.get("nws_nbm", {}).get("status", "Unavailable"),
            "nws_alerts": sources.get("nws_alerts", {}).get("status", "Unavailable"),
            "econet": sources.get("econet", {}).get("status", "Unavailable"),
            "downburst": handoff_status,
        },
        "known_input_completeness": (
            "NWS fields available; source freshness shown separately"
            if all_nws_fields
            else "Some forecast fields missing; only supported evidence shown"
        ),
        "pending_inputs": pending,
        "full_forecast_risk_assessed": False,
        "management_posture_assessed": False,
        "notes": (
            "This is a conservative floor from available weather and other "
            "assessed hazard inputs, not an all-hazards forecast risk or a "
            "management posture. The unassessed Downburst/SPC inputs could "
            "raise the eventual risk level. Never treat a Normal known-input "
            "floor as clearance; follow current NWS alerts and City procedures."
        ),
    }
