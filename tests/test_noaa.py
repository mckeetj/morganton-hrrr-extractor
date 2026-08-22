import datetime as dt
import unittest
from unittest.mock import patch

from burke_hrrr.config import BURKE_BOUNDS
from burke_hrrr.noaa import (
    Cycle,
    build_nomads_url,
    discover_latest_complete_cycle,
    forecast_hour_through,
)


class NoaaTests(unittest.TestCase):
    def test_forecast_hour_rounds_up(self) -> None:
        cycle = dt.datetime(2026, 8, 21, 12, tzinfo=dt.UTC)
        through = dt.datetime(2026, 8, 22, 0, 30, tzinfo=dt.UTC)
        self.assertEqual(forecast_hour_through(cycle, through), 13)

    def test_surface_url_contains_bbox_and_v2_fields(self) -> None:
        cycle = Cycle(dt.datetime(2026, 8, 21, 12, tzinfo=dt.UTC))
        url = build_nomads_url(cycle, 5, BURKE_BOUNDS, product="surface")
        self.assertIn("filter_hrrr_2d.pl", url)
        self.assertIn("hrrr.t12z.wrfsfcf05.grib2", url)
        self.assertIn("var_GUST=on", url)
        self.assertIn("var_HLCY=on", url)
        self.assertNotIn("var_VUCSH=on", url)
        self.assertIn("var_REFC=on", url)
        self.assertIn("lev_90-0_mb_above_ground=on", url)
        self.assertIn("leftlon=-82.1", url)

    def test_pressure_url_uses_supported_hrrr_2d_levels(self) -> None:
        cycle = Cycle(dt.datetime(2026, 8, 21, 12, tzinfo=dt.UTC))
        url = build_nomads_url(cycle, 5, BURKE_BOUNDS, product="pressure")
        self.assertIn("filter_hrrr_2d.pl", url)
        self.assertIn("hrrr.t12z.wrfsfcf05.grib2", url)
        for level in (1000, 925, 850, 700, 500, 300, 250):
            self.assertIn(f"lev_{level}_mb=on", url)
        self.assertNotIn("lev_975_mb=on", url)
        self.assertIn("var_DPT=on", url)
        self.assertIn("var_HGT=on", url)

    def test_special_downdraft_url_is_unambiguous(self) -> None:
        cycle = Cycle(dt.datetime(2026, 8, 21, 12, tzinfo=dt.UTC))
        url = build_nomads_url(cycle, 5, BURKE_BOUNDS, product="max_downdraft")
        self.assertIn("var_MAXDVV=on", url)
        self.assertIn("lev_100-1000_mb_above_ground=on", url)
        self.assertNotIn("var_MAXUVV=on", url)

    def test_special_updraft_helicity_url_is_unambiguous(self) -> None:
        cycle = Cycle(dt.datetime(2026, 8, 21, 12, tzinfo=dt.UTC))
        url = build_nomads_url(cycle, 5, BURKE_BOUNDS, product="updraft_helicity")
        self.assertIn("var_MXUPHL=on", url)
        self.assertIn("lev_5000-2000_m_above_ground=on", url)

    @patch("burke_hrrr.noaa.cycle_has_forecast")
    def test_discovery_selects_newest_complete_cycle(self, available) -> None:
        available.side_effect = [False, False, True]
        now = dt.datetime(2026, 8, 21, 14, 15, tzinfo=dt.UTC)
        cycle = discover_latest_complete_cycle(now=now, required_forecast_hour=10)
        self.assertEqual(cycle.initialized, dt.datetime(2026, 8, 21, 12, tzinfo=dt.UTC))


if __name__ == "__main__":
    unittest.main()
