# Burke County HRRR extractor

`burke-hrrr` retrieves NOAA HRRR surface and pressure-level guidance for a
small envelope around Burke County, North Carolina. It records initialization
and valid times, source URLs, field units, and spatial min/median/90th
percentile/max summaries. Derived output includes DCAPE, 0–3-km and
700–500-mb lapse rates, 700-mb dewpoint depression, 700–500-mb humidity, and
0–6-km bulk shear when their input fields are complete.

## Install

The runtime needs ecCodes through the Python `eccodes` package, `cfgrib`,
`xarray`, and MetPy. In a network-enabled build environment:

```bash
uv pip install -e .
```

Bake those dependencies into the scheduled-run image; do not install them on
each outlook run.

## Usage

Preview discovery and NOAA requests without downloading GRIB files:

```bash
burke-hrrr --dry-run
```

Retrieve the newest cycle complete through the next local midnight:

```bash
burke-hrrr --output output/burke-hrrr.json
```

Use a fixed cycle for reproducibility:

```bash
burke-hrrr --cycle 2026082112 --through 2026-08-22T00:00:00-04:00
```

The program exits with status 2 if it cannot find a complete cycle, receives a
non-GRIB NOAA response, or cannot decode the data. This allows the outlook to
say that guidance is unavailable instead of silently using a stale cycle.

## Interpretation rules

- `GUST` and maximum 10-m `WIND` are model diagnostics, not predicted observed
  gusts at a specific address.
- Independently maximized `MAXUW` and `MAXVW` must never be vector-combined.
- Spatial maxima should be paired with storm coverage and simulated
  reflectivity; an isolated grid-cell maximum is not a county-wide forecast.
- Cycle initialization, valid time, units, and field provenance belong in the
  operational briefing whenever a value materially affects the verdict.
