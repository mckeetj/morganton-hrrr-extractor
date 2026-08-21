from __future__ import annotations

import datetime as dt
import unittest
from zoneinfo import ZoneInfo

from burke_hrrr.econet import (
    EASTERN,
    Observation,
    _build_url,
    build_summary,
    parse_clouds_json,
)


class EconetTests(unittest.TestCase):
    def test_url_requests_morg_hourly_json_qc(self) -> None:
        now = dt.datetime(2026, 8, 21, 10, 15, tzinfo=EASTERN)
        url = _build_url("SECRET_HASH", now - dt.timedelta(days=7), now)
        self.assertIn("location%3DMORG", url)
        self.assertIn("output=json", url)
        self.assertIn("obtype=H", url)
        self.assertIn("int=1+hour", url)
        self.assertIn("qclimit=1", url)
        self.assertIn("precip1m%7Cin", url)
        self.assertIn("soilmoist%7Cm3%2Fm3", url)
        self.assertIn("soilmoist20cm%7Cm3%2Fm3", url)

    def test_parse_long_records(self) -> None:
        payload = {
            "data": [
                {
                    "location": "MORG",
                    "datetime": "2026-08-21T09:00:00-04:00",
                    "var": "soilmoist",
                    "value": 0.31,
                    "unit": "m3/m3",
                    "score": 0,
                    "flag": "",
                },
                {
                    "location": "MORG",
                    "datetime": "2026-08-21T09:00:00-04:00",
                    "var": "precip1m",
                    "value": 0.05,
                    "unit": "in",
                    "score": 1,
                    "flag": "LG",
                },
            ]
        }
        obs = parse_clouds_json(payload)
        self.assertEqual(len(obs), 2)
        self.assertEqual({item.variable for item in obs}, {"soilmoist", "precip1m"})

    def test_parse_nested_records(self) -> None:
        payload = {
            "MORG": {
                "2026-08-21 09:00:00": {
                    "soilmoist20cm": {
                        "value": "0.40",
                        "unit": "m3/m3",
                        "score": "0",
                        "flag": "OK",
                    }
                }
            }
        }
        obs = parse_clouds_json(payload)
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0].variable, "soilmoist20cm")
        self.assertAlmostEqual(obs[0].value, 0.40)

    def test_summary_filters_qc_and_calculates_windows_and_change(self) -> None:
        now = dt.datetime(2026, 8, 21, 10, 15, tzinfo=EASTERN)
        observations: list[Observation] = []
        # 168 hourly precip records ending at 10:00; 0.01 in each.
        for i in range(168):
            when = now.replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=i)
            observations.append(Observation("precip1m", when, 0.01, "in", 0, "", True))
        # Rejected bad-QC precip must not affect totals.
        observations.append(Observation("precip1m", now - dt.timedelta(hours=1), 5.0, "in", 2, "BAD", True))

        for variable, latest, prior in (
            ("soilmoist", 0.35, 0.30),
            ("soilmoist20cm", 0.42, 0.40),
        ):
            for i in range(0, 169, 6):
                when = now.replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=i)
                value = latest if i == 0 else (prior if i == 24 else 0.32 + i / 10000)
                observations.append(Observation(variable, when, value, "m3/m3", 0, "", True))

        result = build_summary(observations, now)
        self.assertEqual(result["status"], "current")
        self.assertAlmostEqual(result["rainfall"]["24h"]["total_inches"], 0.24, places=3)
        self.assertAlmostEqual(result["rainfall"]["72h"]["total_inches"], 0.72, places=3)
        self.assertAlmostEqual(result["rainfall"]["168h"]["total_inches"], 1.68, places=3)
        self.assertAlmostEqual(result["soil_moisture"]["10cm"]["change_24h_m3m3"], 0.05, places=4)
        self.assertAlmostEqual(result["soil_moisture"]["20cm"]["change_24h_m3m3"], 0.02, places=4)
        self.assertFalse(result["soil_moisture"]["10cm"]["current_value_published"])
        self.assertNotIn("value", result["soil_moisture"]["10cm"])

    def test_stale_soil_marks_summary_stale(self) -> None:
        now = dt.datetime(2026, 8, 21, 10, 15, tzinfo=EASTERN)
        old = now - dt.timedelta(hours=2)
        obs = [
            Observation("precip1m", old, 0.0, "in", 0, "", True),
            Observation("soilmoist", old, 0.3, "m3/m3", 0, "", True),
            Observation("soilmoist20cm", old, 0.4, "m3/m3", 0, "", True),
        ]
        result = build_summary(obs, now)
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["soil_moisture"]["10cm"]["status"], "stale")

    def test_missing_flag_metadata_fails_closed(self) -> None:
        now = dt.datetime(2026, 8, 21, 10, 15, tzinfo=EASTERN)
        obs = [
            Observation("precip1m", now, 0.0, "in", 0, None, False),
            Observation("soilmoist", now, 0.3, "m3/m3", 0, None, False),
            Observation("soilmoist20cm", now, 0.4, "m3/m3", 0, None, False),
        ]
        with self.assertRaisesRegex(RuntimeError, "QC flag metadata"):
            build_summary(obs, now)


if __name__ == "__main__":
    unittest.main()
