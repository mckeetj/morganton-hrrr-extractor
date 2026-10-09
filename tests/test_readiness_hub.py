import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from burke_hrrr.readiness_hub import (
    EASTERN,
    _downburst_handoff,
    _load_stress,
    conservative_accumulation,
    source_aligned_qpf_24,
)


class ReadinessHubTests(unittest.TestCase):
    def test_source_aligned_qpf_uses_24_contiguous_hourly_slots(self) -> None:
        starts = []
        values = []
        base = dt.datetime(2026, 10, 9, 16, 0, tzinfo=EASTERN)
        for hour in range(30):
            start = base + dt.timedelta(hours=hour)
            end = start + dt.timedelta(hours=1)
            starts.append(
                f"<start-valid-time>{start.isoformat()}</start-valid-time>"
                f"<end-valid-time>{end.isoformat()}</end-valid-time>"
            )
            values.append("<value>0.1</value>")
        xml = (
            "<dwml><head><product><creation-date>"
            "2026-10-09T14:23:34-04:00"
            "</creation-date></product></head><data>"
            "<time-layout><layout-key>k-p1h-n1-0</layout-key>"
            + "".join(starts)
            + "</time-layout><parameters>"
            '<hourly-qpf type="floating" units="inches" time-layout="k-p1h-n1-0">'
            + "".join(values)
            + "</hourly-qpf></parameters></data></dwml>"
        )
        result = source_aligned_qpf_24(
            xml,
            dt.datetime(2026, 10, 9, 16, 25, tzinfo=EASTERN),
        )
        self.assertEqual(result["window_start"], base)
        self.assertEqual(result["window_end"], base + dt.timedelta(hours=24))
        self.assertAlmostEqual(result["qpf_24_in"], 2.4, places=6)

    def test_conservative_accumulation_rejects_nonzero_partial_interval(self) -> None:
        values = [
            {
                "validTime": "2026-10-09T12:00:00-04:00/PT6H",
                "value": 3.0,
            },
            {
                "validTime": "2026-10-09T18:00:00-04:00/PT6H",
                "value": 4.0,
            },
        ]
        result = conservative_accumulation(
            values,
            dt.datetime(2026, 10, 9, 16, 0, tzinfo=EASTERN),
            dt.datetime(2026, 10, 10, 0, 0, tzinfo=EASTERN),
        )
        self.assertIsNone(result)

    def test_conservative_accumulation_accepts_zero_partial_interval(self) -> None:
        values = [
            {
                "validTime": "2026-10-09T12:00:00-04:00/PT6H",
                "value": 0.0,
            },
            {
                "validTime": "2026-10-09T18:00:00-04:00/PT6H",
                "value": 4.0,
            },
        ]
        result = conservative_accumulation(
            values,
            dt.datetime(2026, 10, 9, 16, 0, tzinfo=EASTERN),
            dt.datetime(2026, 10, 10, 0, 0, tzinfo=EASTERN),
        )
        self.assertEqual(result, 4.0)

    def test_load_stress_level_one_is_normal_manual_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "current").mkdir()
            (root / "current" / "load-stress.json").write_text(
                json.dumps(
                    {
                        "level": 1,
                        "label": "Normal",
                        "assessed_at": "2026-10-09T16:39:35-04:00",
                    }
                ),
                encoding="utf-8",
            )
            level, source = _load_stress(
                root,
                dt.datetime(2026, 10, 9, 17, 0, tzinfo=EASTERN),
            )
            self.assertEqual(level, 1)
            self.assertEqual(source["status"], "Manual")
            self.assertIn("Level 1", source["notes"])
            self.assertIn("Normal", source["notes"])

    def test_downburst_handoff_must_be_current_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "current").mkdir()
            (root / "current" / "downburst-outlook.json").write_text(
                json.dumps(
                    {
                        "downburst_index": 5,
                        "verdict": "LOW",
                        "confidence": "High",
                        "source_run": "11:00 AM Model Refresh",
                        "valid_date": "2026-10-09",
                        "issued_at": "2026-10-09T11:05:53-04:00",
                        "spc_category": "General",
                        "spc_issued_at": "2026-10-09T12:13:00-04:00",
                        "index_basis": "Test basis.",
                    }
                ),
                encoding="utf-8",
            )
            index, spc, source, spc_source = _downburst_handoff(
                root,
                dt.datetime(2026, 10, 9, 16, 0, tzinfo=EASTERN),
            )
            self.assertEqual(index, 5)
            self.assertEqual(spc, "General")
            self.assertEqual(source["status"], "Available")
            self.assertEqual(spc_source["status"], "Available")

            index2, spc2, source2, _ = _downburst_handoff(
                root,
                dt.datetime(2026, 10, 10, 8, 0, tzinfo=EASTERN),
            )
            self.assertIsNone(index2)
            self.assertIsNone(spc2)
            self.assertEqual(source2["status"], "Unavailable")


if __name__ == "__main__":
    unittest.main()
