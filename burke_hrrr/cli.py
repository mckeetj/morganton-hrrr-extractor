from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from . import __version__
from .config import BURKE_BOUNDS, SPECIAL_PRODUCTS
from .decode import read_fields, summarize_fields
from .derived import derive_diagnostics
from .noaa import (
    Cycle,
    build_nomads_url,
    discover_latest_complete_cycle,
    download_subset,
    forecast_hour_through,
)
from .operational import build_key_diagnostics, build_operational_summary

EASTERN = ZoneInfo("America/New_York")
PRODUCTS = ("surface", "pressure", *SPECIAL_PRODUCTS.keys())


def _parse_time(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=EASTERN)
    return parsed


def _cycle(value: str) -> Cycle:
    parsed = dt.datetime.strptime(value, "%Y%m%d%H").replace(tzinfo=dt.UTC)
    return Cycle(parsed)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--cycle", type=_cycle, help="fixed UTC cycle as YYYYMMDDHH")
    result.add_argument("--now", type=_parse_time, help="override current time for reproducibility")
    result.add_argument(
        "--through",
        type=_parse_time,
        help="required ending valid time; default next local midnight",
    )
    result.add_argument("--max-forecast-hour", type=int, help="override terminal forecast hour")
    result.add_argument("--workdir", type=Path, default=Path("hrrr-data"))
    result.add_argument("--output", type=Path, default=Path("burke-hrrr.json"))
    result.add_argument("--dry-run", action="store_true", help="discover/print URLs without downloading")
    result.add_argument("--keep-grib", action="store_true")
    result.add_argument("--version", action="version", version=__version__)
    return result


def _default_through(now: dt.datetime) -> dt.datetime:
    local = now.astimezone(EASTERN)
    return (local + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def run(args: argparse.Namespace) -> dict[str, object]:
    now = args.now or dt.datetime.now(dt.UTC)
    through = args.through or _default_through(now)
    if args.cycle:
        cycle = args.cycle
        terminal_hour = args.max_forecast_hour
        if terminal_hour is None:
            terminal_hour = forecast_hour_through(cycle.initialized, through)
    else:
        # Discover against a plausible terminal hour first, then recompute once
        # the selected cycle is known. Extended cycles are naturally preferred
        # when midnight lies beyond the 18-hour standard horizon.
        terminal_hour = args.max_forecast_hour
        if terminal_hour is None:
            latest_hour = now.astimezone(dt.UTC).replace(minute=0, second=0, microsecond=0)
            terminal_hour = forecast_hour_through(latest_hour, through)
        cycle = discover_latest_complete_cycle(now=now, required_forecast_hour=terminal_hour)
        terminal_hour = args.max_forecast_hour or forecast_hour_through(cycle.initialized, through)

    if terminal_hour < 0:
        raise RuntimeError("forecast hour must be nonnegative")
    if terminal_hour > 48:
        raise RuntimeError("requested period exceeds HRRR's 48-hour extended horizon")

    manifest: dict[str, object] = {
        "schema_version": 2,
        "extractor_version": __version__,
        "generated_at": now.astimezone(dt.UTC).isoformat(),
        "source": "NOAA NCEP HRRR 2D via NOMADS",
        "cycle_initialized": cycle.initialized.astimezone(dt.UTC).isoformat(),
        "cycle_age_hours": round(
            (now.astimezone(dt.UTC) - cycle.initialized).total_seconds() / 3600,
            2,
        ),
        "through": through.astimezone(EASTERN).isoformat(),
        "bounds": BURKE_BOUNDS.__dict__,
        "forecast_hours": [],
        "notes": [
            "Direct model fields are guidance, not observations.",
            "County maxima can represent isolated HRRR grid cells; use county p90 and storm coverage alongside maxima.",
            "V2 uses HRRR's direct MAXDVV field as the model downdraft diagnostic; DCAPE is not reconstructed from the sparse filtered 2D pressure profile.",
            "CAPE/CIN layer keys preserve the HRRR GRIB layer labels and are not relabeled as a parcel method.",
            "MAXUW and MAXVW are not vector-combined because their maxima can be noncontemporaneous.",
        ],
    }

    records: list[dict[str, object]] = []
    for forecast_hour in range(0, terminal_hour + 1):
        record: dict[str, object] = {
            "forecast_hour": forecast_hour,
            "valid_time": cycle.valid_time(forecast_hour).astimezone(dt.UTC).isoformat(),
            "products": {},
        }
        products = record["products"]
        assert isinstance(products, dict)

        for product in PRODUCTS:
            url = build_nomads_url(cycle, forecast_hour, BURKE_BOUNDS, product=product)
            product_record: dict[str, object] = {"url": url}
            if product in SPECIAL_PRODUCTS:
                variable, levels = SPECIAL_PRODUCTS[product]
                product_record["requested_variable"] = variable
                product_record["requested_levels"] = list(levels)

            if not args.dry_run:
                path = args.workdir / cycle.date / cycle.hour / f"f{forecast_hour:02d}-{product}.grib2"
                download_subset(cycle, forecast_hour, BURKE_BOUNDS, path, product=product)
                decoded_fields = read_fields(path)
                product_record["fields"] = summarize_fields(decoded_fields, BURKE_BOUNDS)
                product_record["_decoded"] = decoded_fields
                product_record["bytes"] = path.stat().st_size
                if not args.keep_grib:
                    path.unlink(missing_ok=True)
            products[product] = product_record

        if not args.dry_run:
            decoded_by_product: dict[str, list[object]] = {}
            for product in PRODUCTS:
                product_record = products[product]
                assert isinstance(product_record, dict)
                decoded_by_product[product] = product_record.pop("_decoded")  # type: ignore[assignment]

            surface = decoded_by_product["surface"]
            pressure = decoded_by_product["pressure"]
            special = {
                product: decoded_by_product[product]
                for product in SPECIAL_PRODUCTS
            }

            # Runtime values are Field lists; the broad object annotations above
            # keep the JSON-building code straightforward without leaking Field
            # objects into the serialized manifest.
            derived = derive_diagnostics(surface, pressure, BURKE_BOUNDS)  # type: ignore[arg-type]
            record["derived_diagnostics"] = derived
            record["key_diagnostics"] = build_key_diagnostics(
                surface,  # type: ignore[arg-type]
                pressure,  # type: ignore[arg-type]
                special,  # type: ignore[arg-type]
                derived,
                BURKE_BOUNDS,
            )
        else:
            record["derived_diagnostics"] = {}
            record["key_diagnostics"] = {}

        records.append(record)

    manifest["forecast_hours"] = records
    manifest["operational_summary"] = build_operational_summary(records)
    return manifest


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = run(args)
        rendered = json.dumps(result, indent=2, sort_keys=True)
        if args.dry_run:
            print(rendered)
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
            print(f"wrote {args.output}")
        return 0
    except Exception as exc:
        print(f"burke-hrrr: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
