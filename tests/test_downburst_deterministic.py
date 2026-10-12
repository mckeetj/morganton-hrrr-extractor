import datetime as dt
import unittest
from pathlib import Path
from unittest.mock import patch

from burke_hrrr.downburst_deterministic import (
    EASTERN,
    _auto_run,
    _trend,
    calculate_score,
    hrrr_components,
    verdict_for_index,
)


def metric(p90=None, maximum=None, p90_time="2026-10-12T18:00:00+00:00"):
    value = {}
    if p90 is not None:
        value["peak_county_p90"] = {
            "value": p90,
            "valid_time": p90_time,
        }
    if maximum is not None:
        value["peak_county_max"] = {
            "value": maximum,
            "valid_time": p90_time,
        }
    return value


def hrrr_fixture(metrics):
    return {
        "usable": True,
        "reason": "validated",
        "data": {
            "operational_summary": {
                "metrics": metrics,
            }
        },
    }


class DeterministicDownburstTests(unittest.TestCase):
    def test_verdict_bands_match_hub_handoff_scale(self):
        self.assertEqual(verdict_for_index(0), "LOW")
        self.assertEqual(verdict_for_index(24), "LOW")
        self.assertEqual(verdict_for_index(25), "CONDITIONAL")
        self.assertEqual(verdict_for_index(44), "CONDITIONAL")
        self.assertEqual(verdict_for_index(45), "ELEVATED")
        self.assertEqual(verdict_for_index(64), "ELEVATED")
        self.assertEqual(verdict_for_index(65), "HIGH")
        self.assertEqual(verdict_for_index(100), "HIGH")

    def test_today_like_refresh_scores_six_despite_isolated_gust_spike(self):
        hrrr = hrrr_fixture(
            {
                "cape_surface_jkg": metric(p90=0, maximum=200),
                "composite_reflectivity_dbz": metric(p90=-10, maximum=8.25),
                "max_reflectivity_1km_dbz": metric(p90=0, maximum=28.4),
                "max_updraft_velocity_ms": metric(p90=1.74, maximum=3.88),
                "vil_kg_m2": metric(p90=0, maximum=0.004),
                "max_downdraft_magnitude_ms": metric(p90=1.82, maximum=3.34),
                "pwat_inches": metric(p90=0.95, maximum=1.01),
                "dewpoint_depression_700mb_k": metric(p90=37.96, maximum=44.46),
                "lapse_rate_0_3km_agl_k_per_km": metric(p90=5.99, maximum=6.23),
                "bulk_shear_sfc_500mb_kt": metric(p90=42.97, maximum=46.29),
                "surface_gust_mph": metric(p90=47.0, maximum=59.09),
            }
        )
        score, parts = calculate_score(2, 0, 0, hrrr)
        self.assertEqual(score, 6)
        self.assertEqual(parts["official_spc"], 5)
        self.assertEqual(parts["hrrr_instability"], 1)
        self.assertEqual(parts["hrrr_gust"], 0)
        self.assertEqual(parts["hrrr_organization"], 0)

    def test_no_spc_polygon_still_uses_general_low_floor(self):
        hrrr = hrrr_fixture(
            {
                "cape_surface_jkg": metric(p90=0, maximum=200),
                "composite_reflectivity_dbz": metric(p90=-10, maximum=8.25),
                "max_reflectivity_1km_dbz": metric(p90=0, maximum=28.4),
                "max_updraft_velocity_ms": metric(p90=1.74, maximum=3.88),
                "vil_kg_m2": metric(p90=0, maximum=0.004),
                "max_downdraft_magnitude_ms": metric(p90=1.82, maximum=3.34),
                "pwat_inches": metric(p90=0.95, maximum=1.01),
                "dewpoint_depression_700mb_k": metric(p90=37.96, maximum=44.46),
                "lapse_rate_0_3km_agl_k_per_km": metric(p90=5.99, maximum=6.23),
                "bulk_shear_sfc_500mb_kt": metric(p90=42.97, maximum=46.29),
                "surface_gust_mph": metric(p90=47.0, maximum=59.09),
            }
        )
        score, parts = calculate_score(0, 0, 0, hrrr)
        self.assertEqual(score, 6)
        self.assertEqual(parts["official_spc"], 5)
        self.assertEqual(parts["hrrr_gust"], 0)

    def test_general_thunder_initial_is_low_seven(self):
        score, parts = calculate_score(2, 0, 1, None)
        self.assertEqual(score, 7)
        self.assertEqual(verdict_for_index(score), "LOW")
        self.assertEqual(parts["official_spc"], 5)
        self.assertEqual(parts["nws_thunder"], 2)

    def test_spc_fifteen_percent_wind_is_conditional_without_hrrr(self):
        score, _ = calculate_score(3, 15, 0, None)
        self.assertEqual(score, 32)
        self.assertEqual(verdict_for_index(score), "CONDITIONAL")

    def test_strong_corroborated_hrrr_can_raise_general_to_elevated(self):
        hrrr = hrrr_fixture(
            {
                "cape_surface_jkg": metric(p90=1800, maximum=2600),
                "composite_reflectivity_dbz": metric(p90=52, maximum=64),
                "max_reflectivity_1km_dbz": metric(p90=50, maximum=62),
                "max_updraft_velocity_ms": metric(p90=6.5, maximum=12),
                "vil_kg_m2": metric(p90=18, maximum=30),
                "max_downdraft_magnitude_ms": metric(p90=6.5, maximum=9),
                "pwat_inches": metric(p90=1.7, maximum=1.9),
                "dewpoint_depression_700mb_k": metric(p90=26, maximum=33),
                "lapse_rate_0_3km_agl_k_per_km": metric(p90=7.3, maximum=8.0),
                "bulk_shear_sfc_500mb_kt": metric(p90=40, maximum=48),
                "surface_gust_mph": metric(p90=52, maximum=66),
            }
        )
        score, parts = calculate_score(2, 0, 4, hrrr)
        self.assertGreaterEqual(score, 45)
        self.assertEqual(verdict_for_index(score), "ELEVATED")
        self.assertGreaterEqual(parts["hrrr_synergy"], 4)

    def test_auto_run_windows(self):
        self.assertIsNone(
            _auto_run(dt.datetime(2026, 10, 12, 8, 34, tzinfo=EASTERN))
        )
        self.assertEqual(
            _auto_run(dt.datetime(2026, 10, 12, 8, 35, tzinfo=EASTERN)),
            "initial",
        )
        self.assertEqual(
            _auto_run(dt.datetime(2026, 10, 12, 11, 5, tzinfo=EASTERN)),
            "refresh",
        )

    def test_refresh_trend_only_compares_against_initial(self):
        self.assertEqual(
            _trend(
                30,
                {"source_run": "8:30 AM Initial", "downburst_index": 20},
                "refresh",
            ),
            "Higher",
        )
        self.assertEqual(
            _trend(
                30,
                {"source_run": "11:00 AM Model Refresh", "downburst_index": 20},
                "refresh",
            ),
            "Similar",
        )

    def test_hrrr_component_gate_blocks_uncorroborated_gust(self):
        hrrr = hrrr_fixture(
            {
                "cape_surface_jkg": metric(p90=0, maximum=0),
                "composite_reflectivity_dbz": metric(p90=0, maximum=10),
                "max_reflectivity_1km_dbz": metric(p90=0, maximum=20),
                "max_updraft_velocity_ms": metric(p90=0, maximum=1),
                "vil_kg_m2": metric(p90=0, maximum=0),
                "max_downdraft_magnitude_ms": metric(p90=1, maximum=2),
                "pwat_inches": metric(p90=1.0, maximum=1.1),
                "dewpoint_depression_700mb_k": metric(p90=30, maximum=35),
                "lapse_rate_0_3km_agl_k_per_km": metric(p90=7.5, maximum=8.0),
                "bulk_shear_sfc_500mb_kt": metric(p90=50, maximum=55),
                "surface_gust_mph": metric(p90=60, maximum=70),
            }
        )
        components = hrrr_components(hrrr)
        self.assertEqual(components["organization"], 0)
        self.assertEqual(components["gust"], 0)


if __name__ == "__main__":
    unittest.main()
