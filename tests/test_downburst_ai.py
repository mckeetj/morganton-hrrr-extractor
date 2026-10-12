import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from burke_hrrr.downburst_ai import (
    EASTERN,
    _expected_verdict,
    _is_run_already_complete,
    _validate_model_result,
    build_handoff,
)


class DownburstAiTests(unittest.TestCase):
    def test_expected_verdict_bands(self) -> None:
        self.assertEqual(_expected_verdict(0), "LOW")
        self.assertEqual(_expected_verdict(24), "LOW")
        self.assertEqual(_expected_verdict(25), "CONDITIONAL")
        self.assertEqual(_expected_verdict(44), "CONDITIONAL")
        self.assertEqual(_expected_verdict(45), "ELEVATED")
        self.assertEqual(_expected_verdict(64), "ELEVATED")
        self.assertEqual(_expected_verdict(65), "HIGH")
        self.assertEqual(_expected_verdict(100), "HIGH")

    def test_verdict_index_mismatch_is_rejected(self) -> None:
        value = {
            "downburst_index": 20,
            "verdict": "CONDITIONAL",
            "confidence": "Moderate",
            "trend": None,
            "most_likely_window": "No meaningful window",
            "spc_category": "General",
            "spc_issued_at": None,
            "index_basis": "Test basis",
        }
        with self.assertRaises(ValueError):
            _validate_model_result(value, "initial")

    def test_initial_requires_null_trend(self) -> None:
        value = {
            "downburst_index": 20,
            "verdict": "LOW",
            "confidence": "Moderate",
            "trend": "Similar",
            "most_likely_window": "No meaningful window",
            "spc_category": "General",
            "spc_issued_at": None,
            "index_basis": "Test basis",
        }
        with self.assertRaises(ValueError):
            _validate_model_result(value, "initial")

    def test_refresh_requires_trend(self) -> None:
        value = {
            "downburst_index": 20,
            "verdict": "LOW",
            "confidence": "Moderate",
            "trend": None,
            "most_likely_window": "No meaningful window",
            "spc_category": "General",
            "spc_issued_at": None,
            "index_basis": "Test basis",
        }
        with self.assertRaises(ValueError):
            _validate_model_result(value, "refresh")

    def test_idempotent_run_rules(self) -> None:
        initial = {"source_run": "8:30 AM Initial"}
        refresh = {"source_run": "11:00 AM Model Refresh"}
        self.assertTrue(_is_run_already_complete(initial, "initial"))
        self.assertFalse(_is_run_already_complete(initial, "refresh"))
        self.assertTrue(_is_run_already_complete(refresh, "initial"))
        self.assertTrue(_is_run_already_complete(refresh, "refresh"))

    @patch("burke_hrrr.downburst_ai._call_openai")
    @patch("burke_hrrr.downburst_ai._current_eastern")
    def test_build_handoff_adds_script_owned_metadata(
        self,
        mock_now,
        mock_call,
    ) -> None:
        now = dt.datetime(2026, 10, 12, 8, 35, tzinfo=EASTERN)
        mock_now.return_value = now
        mock_call.return_value = {
            "downburst_index": 7,
            "verdict": "LOW",
            "confidence": "Moderate",
            "trend": None,
            "most_likely_window": "No meaningful convective window",
            "spc_category": "General",
            "spc_issued_at": None,
            "index_basis": "Dry and weakly unstable.",
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "current").mkdir()
            (root / "current" / "morg-econet-0830.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-10-12T12:05:00+00:00",
                        "status": "current",
                        "station": {"id": "MORG"},
                        "freshness": {
                            "within_limit": True,
                            "latest_accepted_observation_time": "2026-10-12T07:05:00-04:00",
                        },
                        "quality_control": {"flag_metadata_verified": True},
                    }
                ),
                encoding="utf-8",
            )

            handoff, status = build_handoff(
                repo_root=root,
                run="initial",
                model="test-model",
                api_key="test-key",
            )

        self.assertEqual(status, "generated")
        self.assertIsNotNone(handoff)
        assert handoff is not None
        self.assertEqual(handoff["source_run"], "8:30 AM Initial")
        self.assertEqual(handoff["valid_date"], "2026-10-12")
        self.assertEqual(handoff["issued_at"], now.isoformat())
        self.assertEqual(handoff["handoff_status"], "Automated reviewed handoff")


if __name__ == "__main__":
    unittest.main()
