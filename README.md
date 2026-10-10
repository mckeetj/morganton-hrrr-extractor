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
- Daily Downburst Model: reviewed 0-100 Downburst Index plus the current SPC categorical handoff in `current/downburst-outlook.json`.
- MORG ECONet: rolling 72-hour antecedent rainfall and source freshness.
- Morganton Electric: manual Load Stress Level 1-5 in `current/load-stress.json`.

The builder does not turn missing data into a numeric risk level. When an input
cannot be established conservatively, it is omitted so the Hub's degraded-data
propagation can require review.

The snapshot workflow runs hourly at :15 and also reruns immediately when the
Downburst or Load Stress handoff files change.

### Controlled-pilot operating routine

- The 8:30 AM Daily Downburst Outlook publishes the morning Downburst handoff,
  which immediately triggers a Hub snapshot refresh.
- The 11:00 AM Daily Downburst Model Refresh publishes the late-morning handoff,
  which immediately triggers another Hub snapshot refresh.
- Hourly :15 refreshes keep NWS forecast, QPF, winter, alert, and provenance
  fields current throughout the rest of the day.
- Load Stress remains an explicit manual Morganton Electric Level 1-5
  assessment until changed by an operator.
- Before a meaningful operational decision, import the latest
  `current/operational-readiness-hub.json`, review Snapshot Age and Source
  Health, and use the Hub as decision support. NWS warnings, City procedures,
  and management judgment remain authoritative.
