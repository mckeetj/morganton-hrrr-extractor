import unittest

import numpy as np

from burke_hrrr.config import BURKE_BOUNDS
from burke_hrrr.decode import Field
from burke_hrrr.operational import build_key_diagnostics, build_operational_summary


LAT = np.array([[35.70, 35.80], [35.90, 35.95]])
LON = np.array([[-81.90, -81.75], [-81.60, -81.45]])


def field(
    name: str,
    type_of_level: str,
    level: float | None,
    step_type: str,
    units: str,
    values,
    *,
    top: float | None = None,
    bottom: float | None = None,
) -> Field:
    return Field(
        name,
        type_of_level,
        level,
        step_type,
        units,
        np.asarray(values, dtype=float),
        LAT,
        LON,
        top_level=top,
        bottom_level=bottom,
    )


class OperationalTests(unittest.TestCase):
    def test_key_diagnostics_maps_direct_hrrr_fields(self) -> None:
        surface = [
            field("gust", "surface", 0, "instant", "m s-1", [[10, 20], [15, 25]]),
            field("max_10si", "heightAboveGround", 10, "max", "m s-1", [[12, 21], [16, 26]]),
            field("unknown", "heightAboveGround", 1000, "max", "dB", [[30, 45], [50, 55]]),
            field("refc", "atmosphere", 0, "instant", "dB", [[20, 35], [40, 50]]),
            field("pwat", "atmosphereSingleLayer", 0, "instant", "kg m-2", [[25.4, 38.1], [50.8, 63.5]]),
            field("cape", "surface", 0, "instant", "J kg-1", [[500, 1000], [1500, 2000]]),
            field("cape", "pressureFromGroundLayer", 9000, "instant", "J kg-1", [[600, 1100], [1600, 2100]]),
            field("hlcy", "heightAboveGroundLayer", 3000, "instant", "m2 s-2", [[50, 100], [150, 200]]),
        ]
        specials = {
            "max_downdraft": [
                field("unknown", "pressureFromGroundLayer", 10000, "max", "m s-1", [[-2, -4], [-6, -8]])
            ],
            "max_updraft": [
                field("unknown", "pressureFromGroundLayer", 10000, "max", "m s-1", [[5, 10], [15, 20]])
            ],
            "updraft_helicity": [
                field("unknown", "heightAboveGroundLayer", 5000, "max", "m2 s-2", [[25, 50], [75, 100]])
            ],
        }
        derived = {
            "lapse_rate_700_500mb": {
                "units": "K/km",
                "method": "test",
                "summary": {"count": 4, "min": 5.0, "median": 6.0, "p90": 7.0, "max": 7.5},
            }
        }
        result = build_key_diagnostics(surface, [], specials, derived, BURKE_BOUNDS)

        self.assertAlmostEqual(result["surface_gust_mph"]["summary"]["max"], 55.9234, places=3)
        self.assertEqual(result["max_reflectivity_1km_dbz"]["summary"]["max"], 55.0)
        self.assertEqual(result["cape_90_0mb_jkg"]["summary"]["max"], 2100.0)
        self.assertEqual(result["srh_0_3km_m2s2"]["summary"]["max"], 200.0)
        self.assertEqual(result["max_downdraft_magnitude_ms"]["summary"]["max"], 8.0)
        self.assertEqual(result["updraft_helicity_2_5km_m2s2"]["summary"]["max"], 100.0)
        self.assertEqual(result["lapse_rate_700_500mb_k_per_km"]["summary"]["max"], 7.5)

    def test_operational_summary_keeps_peak_time_and_p90(self) -> None:
        def metric(maximum: float, p90: float):
            return {
                "units": "mph",
                "summary": {"count": 4, "min": 1.0, "median": 2.0, "p90": p90, "max": maximum},
            }

        records = [
            {
                "forecast_hour": 1,
                "valid_time": "2026-08-21T18:00:00+00:00",
                "key_diagnostics": {"surface_gust_mph": metric(40, 35)},
            },
            {
                "forecast_hour": 2,
                "valid_time": "2026-08-21T19:00:00+00:00",
                "key_diagnostics": {"surface_gust_mph": metric(55, 45)},
            },
        ]
        summary = build_operational_summary(records)
        gust = summary["metrics"]["surface_gust_mph"]
        self.assertEqual(gust["peak_county_max"]["value"], 55.0)
        self.assertEqual(gust["peak_county_max"]["forecast_hour"], 2)
        self.assertEqual(gust["peak_county_p90"]["value"], 45.0)


if __name__ == "__main__":
    unittest.main()
