# Burke County HRRR extractor — V2

`burke-hrrr` retrieves NOAA/NCEP HRRR 2D guidance for a small operational
envelope around Burke County, North Carolina. It is designed to publish a
compact JSON input for a later weather-assessment agent, not to replace NWS,
SPC, radar, observations, or forecaster judgment.

## What V2 adds

V2 keeps the working NOMADS/HRRR 2D architecture and adds direct severe-weather
fields that are especially useful for damaging-wind/downburst assessment:

- surface gust and hourly maximum 10-m wind;
- HRRR hourly maximum downdraft/updraft vertical velocity (`MAXDVV`, `MAXUVV`); `MAXDVV` is converted from its negative vertical-velocity sign to a positive speed magnitude in `key_diagnostics`;
- 2–5-km hourly maximum updraft helicity (`MXUPHL`);
- composite reflectivity, hourly maximum 1-km reflectivity, echo top, VIL, and
  HRRR lightning guidance;
- surface and pressure-from-ground-layer CAPE/CIN;
- 0–1-km and 0–3-km storm-relative helicity;
- HRRR VUCSH/VVCSH-derived 0–1-km and 0–6-km bulk shear;
- 700–500-mb lapse rate;
- 0–3-km AGL lapse rate when the filtered vertical profile supports it;
- 700-mb dewpoint depression and RH derived from temperature/dewpoint;
- PWAT.

The published JSON now has `schema_version: 2`, hourly `key_diagnostics`, and a
compact top-level `operational_summary` with peak county maximum and peak county
90th-percentile values plus their valid times.

## Why V2 does not calculate DCAPE

The NOMADS HRRR 2D filter exposes a sparse set of pressure levels. V2 therefore
does **not** reconstruct DCAPE from an under-resolved profile. Instead it uses
HRRR's direct `MAXDVV` field as the model's downdraft diagnostic and converts its
negative vertical-velocity sign to a positive magnitude for operational summaries. A later weather
assessment can pair that with SPC/NWS mesoanalysis, observed thermodynamics,
radar, and the other HRRR fields.

## Install

The project declares its Python dependencies in `pyproject.toml`:

```bash
python -m pip install -e .
```

## Usage

Preview the NOAA URLs without downloading GRIB files:

```bash
burke-hrrr --dry-run --cycle 2026082117 --max-forecast-hour 2
```

Retrieve the newest cycle complete through the next local midnight:

```bash
burke-hrrr --output output/burke-hrrr.json
```

Use a fixed cycle for reproducibility:

```bash
burke-hrrr --cycle 2026082112 --through 2026-08-22T00:00:00-04:00
```

The program exits with status 2 if it cannot find a suitable cycle, receives a
non-GRIB NOAA response, or cannot decode the requested data. NOAA requests use
bounded retries for transient 429/500/502/503/504 responses.

## Output structure

The most useful sections for a downstream agent are:

```text
generated_at
cycle_initialized
cycle_age_hours
through
bounds
operational_summary.metrics
forecast_hours[].valid_time
forecast_hours[].key_diagnostics
```

Each key diagnostic retains units, source-field provenance, and a Burke County
spatial summary (`min`, `median`, `p90`, `max`). The top-level operational
summary records when the peak county max and peak county p90 occur.

## Interpretation rules

- Direct HRRR fields are model guidance, not observations.
- A county maximum can be a single grid cell. Pair it with the county p90,
  simulated storm coverage, radar trends, and observational mesoanalysis.
- `MAXDVV` comes directly from HRRR, but its negative vertical-velocity sign is converted to a positive magnitude in the operational summary; do not call it DCAPE.
- CAPE/CIN keys preserve their GRIB layer labels (for example `90-0 mb above
  ground`) and are not silently relabeled as a parcel method.
- Independently maximized `MAXUW` and `MAXVW` are never vector-combined.
- Cycle initialization, valid time, units, and provenance should accompany any
  value that materially affects an operational verdict.


## Operational Readiness Hub snapshot

The repository now assembles a controlled-pilot snapshot at:

`current/operational-readiness-hub.json`

The snapshot is always generated in **REVIEWED** mode. It never self-designates
as OPERATIONAL.

Inputs are deliberately separated by responsibility:

- NWS Digital Forecast / NDFD and api.weather.gov grid data: wind gust, source-aligned 24-hour QPF, minimum temperature, snowfall, ice accumulation, and active NWS alerts.
- Deterministic Downburst Model v1.0: free, rule-based 0-100 Downburst Index plus the current SPC categorical handoff in `current/downburst-outlook.json`. The production score uses official SPC/NWS data and, for the late-morning refresh, validated 12Z HRRR diagnostics. It does not depend on a paid AI API or on a ChatGPT task writing to GitHub.
- MORG ECONet: rolling 72-hour antecedent rainfall and source freshness.
- Morganton Electric: manual Load Stress Level 1-5 in `current/load-stress.json`.

The builder does not turn missing data into a numeric risk level. When an input
cannot be established conservatively, it is omitted so the Hub's degraded-data
propagation can require review.

The normal snapshot worker is dispatch-driven. Timing is supplied by
`.github/workflows/hub-snapshot-orchestrator.yml`, which self-relays through
`workflow_dispatch` and dispatches the worker at :17 and :47 Eastern. This avoids
depending on GitHub's native scheduled-event delivery for the operational clock.
The Daily Downburst and Load Stress handoff files still trigger immediate snapshot
refreshes when they change.

The resilient Hub clock catches up immediately if it starts while the production
snapshot is already more than 45 minutes old, relays itself before GitHub's six-hour
job limit, and hands off to the next Eastern date after the final daily checkpoint.
The Weather Data Orchestrator also checks that the Hub clock is alive and can restart
it if necessary.

### Deterministic Downburst Model v1.0

The machine score is intentionally transparent and conservative.

- The primary official baseline is the SPC Day 1 **wind probability** at the
  Morganton point. The broader SPC categorical outlook supplies only a small
  floor because a categorical risk can be driven by hail or tornado risk rather
  than damaging wind.
- NWS hourly thunderstorm wording can add a small confirmation signal.
- The 8:35 AM Initial does not use 12Z HRRR.
- The 11:05 AM Refresh can add HRRR support from county p90/max diagnostics for
  surface CAPE, simulated reflectivity, updraft/downdraft magnitude, PWAT,
  low-level lapse rate, 700-mb dryness, deep-layer shear proxy, and surface gust.
- HRRR organization and gust points are gated by actual instability or convective
  support. An isolated modeled gust spike cannot raise the index by itself.
- A multi-signal synergy term is allowed only when instability, convection and
  downdraft support are all present.
- Antecedent rainfall and ECONet soil moisture never raise the meteorological
  Downburst Index; they remain separate tree/utility-impact modifiers.

The existing handoff bands remain:

- 0-24 = LOW
- 25-44 = CONDITIONAL
- 45-64 = ELEVATED
- 65-100 = HIGH

This deterministic score is a machine decision-support index, not an NWS product
or warning. The twice-daily ChatGPT briefing remains useful as an independent
human-readable cross-check during the pilot.

### Controlled-pilot operating routine

- The resilient Hub clock dispatches snapshot builds at approximately :17 and :47
  each hour.
- At approximately 8:35 AM Eastern, GitHub generates the deterministic Initial
  Downburst handoff from current SPC/NWS information.
- At approximately 11:05 AM Eastern, GitHub generates the deterministic Model
  Refresh using current SPC/NWS information plus the validated 12Z HRRR extraction.
- The :17/:47 resilient Hub clock also runs the deterministic generator in
  idempotent recovery mode before each Hub refresh. If the required same-day
  handoff already exists, it exits without changing the file.
- The separate ChatGPT 8:30/11:00 briefings remain human-readable decision-support
  products only; production Hub publication no longer depends on those tasks.
- Load Stress remains an explicit manual Morganton Electric Level 1-5
  assessment until changed by an operator.
- The Site checks for a newer repository snapshot while open and warns when the
  loaded snapshot exceeds the 90-minute pilot freshness threshold.
- NWS warnings, City procedures, and management judgment remain authoritative.
