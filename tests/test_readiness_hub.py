import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from burke_hrrr.vegetation_context import (
    automated_vegetation_context,
    evaluated_vegetation_context,
)
from burke_hrrr.preliminary_outlook import make_preliminary_outlook

from burke_hrrr.readiness_hub import (
    EASTERN,
    _downburst_handoff,
    _econet_context,
    _load_stress,
    build_daily,
    build_timeline,
    conservative_accumulation,
    source_aligned_qpf_24,
)


class ReadinessHubTests(unittest.TestCase):
    def test_econet_freshness_uses_current_policy_for_legacy_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "current").mkdir()
            econet_path = root / "current" / "morg-econet-1030.json"
            legacy_source = {
                "generated_at": "2026-10-12T10:05:51-04:00",
                "freshness": {
                    "latest_accepted_observation_time": "2026-10-12T09:05:00-04:00",
                    "maximum_current_age_minutes": 90,
                },
                "rainfall": {"72h": {"total_inches": 7.37, "coverage_percent": 98.6}},
            }
            econet_path.write_text(json.dumps(legacy_source), encoding="utf-8")

            cases = (
                ("2026-10-12T11:00:00-04:00", "Available"),
                ("2026-10-12T12:05:00-04:00", "Available"),
                ("2026-10-12T12:05:01-04:00", "Stale"),
                ("2026-10-12T13:47:00-04:00", "Stale"),
            )
            for snapshot_time, expected_status in cases:
                with self.subTest(snapshot_time=snapshot_time):
                    rainfall, source = _econet_context(
                        root, dt.datetime.fromisoformat(snapshot_time)
                    )
                    self.assertEqual(rainfall, 7.37)
                    self.assertEqual(source["status"], expected_status)
                    self.assertEqual(source["freshness_threshold_hours"], 3.0)
                    self.assertIn("Freshness threshold=180.0 minutes.", source["notes"])
                    self.assertEqual(
                        source["observation_time"], "2026-10-12T09:05:00-04:00"
                    )

            # Preserve the previous fail-closed behavior when provenance is missing.
            del legacy_source["freshness"]["maximum_current_age_minutes"]
            econet_path.write_text(json.dumps(legacy_source), encoding="utf-8")
            _, source = _econet_context(
                root, dt.datetime.fromisoformat("2026-10-12T10:00:00-04:00")
            )
            self.assertEqual(source["status"], "Stale")

    def test_independent_vegetation_handoff_stays_separate_and_expires(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "current").mkdir()
            econet = {
                "generated_at": "2026-10-14T10:05:58-04:00",
                "station": {"id": "MORG"},
                "quality_control": {"flag_metadata_verified": True},
                "freshness": {
                    "latest_accepted_observation_time": "2026-10-14T09:05:00-04:00",
                    "maximum_current_age_minutes": 180,
                },
                "rainfall": {
                    "24h": {"total_inches": 0.0},
                    "72h": {"total_inches": 0.06, "coverage_percent": 98.6},
                    "168h": {"total_inches": 7.37, "coverage_percent": 99.4},
                },
                "soil_moisture": {
                    depth: {
                        "status": "current",
                        "observation_time": "2026-10-14T09:00:00-04:00",
                        "seven_day_percentile": 58.0,
                        "change_24h_m3m3": 0.0,
                        "qc_score": 0,
                        "qc_status": "good",
                        "current_value_m3m3": 999.0,  # forbidden raw value
                    }
                    for depth in ("10cm", "20cm")
                },
            }
            (root / "current" / "morg-econet-1030.json").write_text(
                json.dumps(econet), encoding="utf-8"
            )
            handoff = {
                "schema_version": 1,
                "valid_date": "2026-10-14",
                "issued_at": "2026-10-14T11:02:23-04:00",
                "source_run": "11:00 AM Model Refresh",
                "classification": "Elevated",
                "confidence": "Moderate",
                "basis": "7-day residual rainfall, near-median stable soils.",
                "evidence": {
                    "rainfall_168_in": 7.37,
                    "soil_10cm_seven_day_percentile": 58.0,
                    "soil_20cm_seven_day_percentile": 58.0,
                },
            }
            handoff_path = root / "current" / "vegetation-vulnerability.json"
            handoff_path.write_text(json.dumps(handoff), encoding="utf-8")

            before_expiry = dt.datetime.fromisoformat("2026-10-14T11:47:00-04:00")
            _, fresh_econet = _econet_context(root, before_expiry)
            fresh, source = evaluated_vegetation_context(
                root, before_expiry, fresh_econet
            )
            self.assertEqual(source["status"], "Available")
            self.assertEqual(fresh["classification"], "Elevated")
            self.assertFalse(fresh["affects_calculated_posture"])
            self.assertFalse(fresh["affects_meteorological_downburst_index"])

            later = dt.datetime.fromisoformat("2026-10-14T13:47:00-04:00")
            _, stale_econet = _econet_context(root, later)
            assessed, stale_source = evaluated_vegetation_context(
                root, later, stale_econet
            )
            self.assertEqual(stale_econet["status"], "Stale")
            self.assertEqual(stale_econet["derived_context"]["rainfall_168_in"], 7.37)
            self.assertEqual(assessed["classification"], "Elevated")
            self.assertEqual(assessed["source_freshness_at_snapshot"], "Stale")
            self.assertEqual(stale_source["status"], "Stale")
            self.assertNotIn("999.0", json.dumps(stale_econet))
            self.assertNotIn("999.0", json.dumps(assessed))

            next_day = dt.datetime.fromisoformat("2026-10-15T08:00:00-04:00")
            expired, expired_source = evaluated_vegetation_context(
                root, next_day, stale_econet
            )
            self.assertIsNone(expired)
            self.assertEqual(expired_source["status"], "Not Assessed")

            handoff["evidence"]["rainfall_168_in"] = 2.0
            handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
            rejected, rejected_source = evaluated_vegetation_context(
                root, later, stale_econet
            )
            self.assertIsNone(rejected)
            self.assertEqual(rejected_source["status"], "Unavailable")

    def test_automated_econet_vegetation_screen_and_safety_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "current").mkdir()
            path = root / "current" / "morg-econet-0830.json"
            econet = {
                "generated_at": "2026-10-20T08:05:20-04:00",
                "station": {"id": "MORG"},
                "status": "current",
                "quality_control": {"flag_metadata_verified": True},
                "freshness": {
                    "latest_accepted_observation_time": "2026-10-20T07:05:00-04:00",
                    "maximum_current_age_minutes": 180,
                },
                "rainfall": {
                    "24h": {"total_inches": 0, "coverage_percent": 95.8},
                    "72h": {"total_inches": 0, "coverage_percent": 98.6},
                    "168h": {"total_inches": 0, "coverage_percent": 99.4},
                },
                "soil_moisture": {
                    depth: {
                        "status": "current",
                        "observation_time": "2026-10-20T07:00:00-04:00",
                        "qc_score": 0,
                        "qc_status": "good",
                        "change_24h_m3m3": -0.01,
                        "seven_day_percentile": 5.1,
                        "current_value_m3m3": 999,  # Must never be republished.
                    }
                    for depth in ("10cm", "20cm")
                },
            }
            def check_at(snapshot_time: str):
                path.write_text(json.dumps(econet), encoding="utf-8")
                now = dt.datetime.fromisoformat(snapshot_time)
                _, source = _econet_context(root, now)
                assessment, status = automated_vegetation_context(now, source)
                self.assertNotIn("999", json.dumps(source))
                if assessment is not None:
                    self.assertNotIn("999", json.dumps(assessment))
                return assessment, status, source

            # The documented October 20 case produces a provisional
            # *moisture screening* Normal; NOT proof of stable roots.
            normal, source, context = check_at("2026-10-20T09:18:00-04:00")
            self.assertEqual(context["status"], "Available")
            self.assertEqual(normal["classification"], "Normal")
            self.assertEqual(normal["confidence"], "Moderate")
            self.assertEqual(normal["status"], "Provisional")
            self.assertEqual(normal["source_run"], "Automated MORG ECONet screen")
            self.assertEqual(normal["valid_date"], "2026-10-20")
            self.assertEqual(normal["soil_percentile_reference"],
                             "rolling_previous_7_days_not_climatology")
            self.assertIn("does not certify root stability", normal["basis"])
            self.assertFalse(normal["affects_calculated_posture"])
            self.assertFalse(normal["affects_meteorological_downburst_index"])
            self.assertEqual(source["status"], "Available")

            # A high rank within the previous week is not automatically
            # wet climatological soil; it cannot elevate with no rain.
            for d in ("10cm", "20cm"):
                econet["soil_moisture"][d]["seven_day_percentile"] = 99
            relative_rank_only, _, _ = check_at("2026-10-20T09:18:00-04:00")
            self.assertEqual(relative_rank_only["classification"], "Normal")

            # Residual heavy rainfall over seven days supports Elevated
            # despite quiet 24/72-hour weather (October 14 pattern).
            econet["rainfall"]["168h"]["total_inches"] = 7.37
            elevated, _, _ = check_at("2026-10-20T09:18:00-04:00")
            self.assertEqual(elevated["classification"], "Elevated")

            # Pre-existing 72-hour Major Event rainfall band supports a
            # high antecedent moisture screening signal.
            econet["rainfall"]["72h"]["total_inches"] = 5.0
            high, _, _ = check_at("2026-10-20T09:18:00-04:00")
            self.assertEqual(high["classification"], "High")

            # The novel soil-wetting condition requires corroborating
            # rainfall and both QC-valid soil depth measurements.
            econet["rainfall"]["168h"]["total_inches"] = 1.1
            econet["rainfall"]["72h"]["total_inches"] = 1.1
            for d in ("10cm", "20cm"):
                econet["soil_moisture"][d]["seven_day_percentile"] = 85
                econet["soil_moisture"][d]["change_24h_m3m3"] = .03
            soil_supported, _, _ = check_at("2026-10-20T09:18:00-04:00")
            self.assertEqual(soil_supported["classification"], "Elevated")

            # Same-day, formerly accepted observations may remain *visibly*
            # stale with Low confidence, never silently presented as current.
            old_assessment, old_source, _ = check_at("2026-10-20T15:00:00-04:00")
            self.assertEqual(old_assessment["classification"], "Elevated")
            self.assertEqual(old_assessment["source_freshness_at_snapshot"], "Stale")
            self.assertEqual(old_assessment["confidence"], "Low")
            self.assertEqual(old_source["status"], "Stale")
            self.assertIn("STALE", old_assessment["basis"])

            # Yesterday's source must not become today's daily assessment.
            missing, unavailable, _ = check_at("2026-10-21T08:30:00-04:00")
            self.assertIsNone(missing)
            self.assertEqual(unavailable["status"], "Not Assessed")

            # Fails closed on QC or insufficient rainfall coverage.
            econet["soil_moisture"]["10cm"]["qc_score"] = 2
            no_qc, source_qc, _ = check_at("2026-10-20T09:18:00-04:00")
            self.assertIsNone(no_qc)
            self.assertEqual(source_qc["status"], "Unavailable")
            econet["soil_moisture"]["10cm"]["qc_score"] = 0
            econet["rainfall"]["72h"]["coverage_percent"] = 83
            no_coverage, source_cov, _ = check_at("2026-10-20T09:18:00-04:00")
            self.assertIsNone(no_coverage)
            self.assertIn("coverage", source_cov["notes"])

    def test_morning_preliminary_outlook_preserves_partial_risk(self) -> None:
        now = dt.datetime.fromisoformat("2026-10-15T08:17:31-04:00")
        daily = []
        for date, name, gust, risk, hazard in (
            ("2026-10-15", "Today", 6.9, 0, "None"),
            ("2026-10-16", "Tomorrow", 27.6, 1, "Forecast Surface Gust"),
            ("2026-10-17", "Sat", 19.6, 0, "None"),
        ):
            start = dt.datetime.fromisoformat(date + "T08:00:00-04:00")
            daily.append({
                "label": name,
                "forecast_date": date,
                "window_start": start.isoformat(),
                "window_end": (start + dt.timedelta(days=1)).isoformat(),
                "data_status": "Partial",
                "overall_level": risk,
                "primary_hazard": hazard,
                "wind_gust_mph": gust,
                "qpf_24_in": 0.0,
                "min_temp_f": 46,
                "ice_accretion_in": 0.0,
                "snowfall_in": 0.0,
            })
        severe = {
            "nws_alert": "None",
            "rainfall_72_in": 0.0,
            "rainfall_168_in": 7.37,
        }
        sources = {
            "downburst_workflow": {"status": "Unavailable"},
            "nws_nbm": {"status": "Available"},
            "nws_alerts": {"status": "Available"},
            "econet": {"status": "Available"},
        }
        preview = make_preliminary_outlook(now, severe, sources, daily)
        self.assertIsNotNone(preview)
        self.assertEqual(preview["phase"], "Before initial model")
        self.assertEqual(preview["status"], "Preliminary")
        floor = preview["known_input_planning_floor"]
        self.assertEqual(floor["level"], 1)
        self.assertEqual(floor["label"], "Awareness")
        self.assertEqual(floor["period"], "Tomorrow")
        self.assertEqual(floor["primary_hazard"], "Forecast Surface Gust")
        self.assertEqual(floor["headline"], "At least Awareness — Partial")
        self.assertEqual(preview["current_day_inputs"]["forecast_gust_mph"], 6.9)
        self.assertEqual(preview["current_day_inputs"]["antecedent_rainfall_168_in"], 7.37)
        self.assertFalse(preview["full_forecast_risk_assessed"])
        self.assertFalse(preview["management_posture_assessed"])
        self.assertEqual(preview["source_health"]["econet"], "Available")

        # A stale ECONet source must never be presented as fresh, even if
        # current NWS hazards continue to support a planning floor.
        sources["econet"]["status"] = "Stale"
        stale = make_preliminary_outlook(now, severe, sources, daily)
        self.assertEqual(stale["source_health"]["econet"], "Stale")
        self.assertIn("Fresh MORG ECONet antecedent observations", stale["pending_inputs"])
        self.assertEqual(stale["known_input_planning_floor"]["level"], 1)

        # The normal full forecast takes priority once today's model arrives.
        sources["downburst_workflow"]["status"] = "Available"
        self.assertIsNone(make_preliminary_outlook(now, severe, sources, daily))
        sources["downburst_workflow"]["status"] = "Stale"
        after_refresh = dt.datetime.fromisoformat("2026-10-15T11:47:00-04:00")
        missing = make_preliminary_outlook(after_refresh, severe, sources, daily)
        self.assertEqual(missing["phase"], "Late model refresh missing")
        self.assertFalse(missing["management_posture_assessed"])

        # With no supported current NWS forecast, do not invent a Normal floor.
        sources["nws_nbm"]["status"] = "Unavailable"
        blank = make_preliminary_outlook(now, severe, sources, daily)
        self.assertIsNone(blank["known_input_planning_floor"]["level"])
        self.assertEqual(blank["known_input_planning_floor"]["label"], "Not established")

        # Previous-day day-0 rows must not masquerade as a new morning outlook.
        daily[0]["forecast_date"] = "2026-10-14"
        self.assertIsNone(make_preliminary_outlook(now, severe, sources, daily))

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

    def _outlook_fixture(self):
        base = dt.datetime(2026, 10, 10, 9, 0, tzinfo=EASTERN)
        qpf_rows = []
        gust_rows = []
        temp_rows = []
        for hour in range(169):
            start = base + dt.timedelta(hours=hour)
            end = start + dt.timedelta(hours=1)
            qpf_rows.append({"start": start, "end": end, "value": 0.05})
            gust_rows.append({"start": start, "end": end, "value": 30.0})
            temp_rows.append({"start": start, "end": end, "value": 60.0})
        six_hour = []
        for hour in range(0, 174, 6):
            start = base + dt.timedelta(hours=hour)
            six_hour.append(
                {
                    "validTime": f"{start.isoformat()}/PT6H",
                    "value": 0.0,
                }
            )
        return {
            "qpf_window_start": base,
            "qpf_hourly_rows": qpf_rows,
            "gust_series": gust_rows,
            "temperature_series": temp_rows,
            "snow_raw_values": six_hour,
            "snow_uom": "wmoUnit:mm",
            "ice_raw_values": six_hour,
            "ice_uom": "wmoUnit:mm",
        }

    def test_timeline_marks_future_downburst_not_assessed(self) -> None:
        nws = self._outlook_fixture()
        now = dt.datetime(2026, 10, 10, 9, 38, tzinfo=EASTERN)
        timeline = build_timeline(
            nws,
            now,
            rainfall_72_in=0.05,
            downburst_index=29,
            spc_category="General",
            downburst_valid_date="2026-10-10",
            load_stress_level=1,
        )
        self.assertEqual(len(timeline), 16)
        self.assertEqual(timeline[0]["downburst_index"], 29)
        self.assertEqual(timeline[0]["downburst_status"], "Available")
        tomorrow = next(row for row in timeline if row["valid_time"].startswith("2026-10-11"))
        self.assertIsNone(tomorrow["downburst_index"])
        self.assertEqual(tomorrow["downburst_status"], "Not Assessed")
        self.assertEqual(tomorrow["data_status"], "Partial")
        self.assertAlmostEqual(timeline[0]["qpf_24_in"], 1.2, places=6)
        self.assertAlmostEqual(timeline[0]["interval_qpf_in"], 0.15, places=6)

    def test_timeline_intervals_cover_72_hours_without_gaps(self) -> None:
        nws = self._outlook_fixture()
        base = nws["qpf_window_start"]
        # A gust in the latter half of a six-hour interval must be included.
        nws["gust_series"][29]["value"] = 55.0
        timeline = build_timeline(nws, base, 0.05, 29, "General", "2026-10-10", 1)
        self.assertEqual(
            [row["forecast_hour"] for row in timeline],
            [*range(0, 24, 3), *range(24, 72, 6)],
        )
        self.assertEqual([row["interval_hours"] for row in timeline], [3] * 8 + [6] * 8)
        for index, row in enumerate(timeline):
            start = dt.datetime.fromisoformat(row["valid_time"]).astimezone(dt.UTC)
            end = dt.datetime.fromisoformat(row["interval_end"]).astimezone(dt.UTC)
            self.assertEqual(end - start, dt.timedelta(hours=row["interval_hours"]))
            if index:
                self.assertEqual(row["valid_time"], timeline[index - 1]["interval_end"])
            self.assertAlmostEqual(row["interval_qpf_in"], 0.05 * row["interval_hours"])
            self.assertAlmostEqual(row["qpf_24_in"], 1.2)
        self.assertEqual(
            dt.datetime.fromisoformat(timeline[-1]["interval_end"]),
            base + dt.timedelta(hours=72),
        )
        self.assertAlmostEqual(sum(row["interval_qpf_in"] for row in timeline), 3.6)
        self.assertEqual(timeline[8]["wind_gust_mph"], 55.0)

    def test_timeline_missing_qpf_is_not_zero_or_redistributed(self) -> None:
        nws = self._outlook_fixture()
        nws["qpf_hourly_rows"][1]["value"] = None
        timeline = build_timeline(
            nws, nws["qpf_window_start"], 0.05, 29, "General", "2026-10-10", 1
        )
        self.assertIsNone(timeline[0]["interval_qpf_in"])
        self.assertIsNone(timeline[0]["qpf_24_in"])
        self.assertEqual(timeline[0]["data_status"], "Partial")
        self.assertIn("Unavailable NWS fields: interval QPF, next-24-hour QPF", timeline[0]["note"])
        self.assertAlmostEqual(timeline[1]["interval_qpf_in"], 0.15)
        self.assertAlmostEqual(timeline[1]["qpf_24_in"], 1.2)

        # Interval QPF may be valid when its separate forward window is not.
        nws = self._outlook_fixture()
        nws["qpf_hourly_rows"] = nws["qpf_hourly_rows"][:72]
        timeline = build_timeline(
            nws, nws["qpf_window_start"], 0.05, 29, "General", "2026-10-10", 1
        )
        self.assertAlmostEqual(timeline[-1]["interval_qpf_in"], 0.3)
        self.assertIsNone(timeline[-1]["qpf_24_in"])

    def test_timeline_labels_missing_econet_without_claiming_nws_failure(self) -> None:
        nws = self._outlook_fixture()
        timeline = build_timeline(
            nws, nws["qpf_window_start"], None, 29, "General", "2026-10-10", 1
        )
        self.assertTrue(all(row["data_status"] == "Partial" for row in timeline))
        self.assertIn("ECONet antecedent rainfall baseline is unavailable", timeline[0]["note"])
        self.assertNotIn("Unavailable NWS", timeline[0]["note"])
        self.assertIsNone(timeline[0]["rainfall_72_in"])

    def test_timeline_does_not_extend_downburst_across_midnight(self) -> None:
        nws = self._outlook_fixture()
        # A 23:00–02:00 interval extends beyond the handoff's valid date.
        nws["qpf_window_start"] = nws["qpf_window_start"].replace(hour=23)
        timeline = build_timeline(
            nws, nws["qpf_window_start"], 0.05, 29, "General", "2026-10-10", 1
        )
        self.assertIsNone(timeline[0]["downburst_index"])
        self.assertIsNone(timeline[0]["spc_category"])
        self.assertEqual(timeline[0]["downburst_status"], "Not Assessed")
        self.assertEqual(timeline[0]["data_status"], "Partial")

    def test_timeline_interval_qpf_and_offsets_across_dst(self) -> None:
        for anchor in ("2026-10-31T21:00:00-04:00", "2027-03-13T21:00:00-05:00"):
            for named_zone in (False, True):
                with self.subTest(anchor=anchor, named_zone=named_zone):
                    base = dt.datetime.fromisoformat(anchor)
                    if named_zone:
                        base = base.astimezone(EASTERN)
                    base_utc = base.astimezone(dt.UTC)
                    nws = self._outlook_fixture()
                    nws["qpf_window_start"] = base
                    qpf = []
                    gust = []
                    temp = []
                    for hour in range(96):
                        start = base_utc + dt.timedelta(hours=hour)
                        end = start + dt.timedelta(hours=1)
                        qpf.append({"start": start, "end": end, "value": (hour + 1) / 1000})
                        gust.append({"start": start, "end": end, "value": 10.0})
                        temp.append({"start": start, "end": end, "value": 50.0})
                    nws.update(qpf_hourly_rows=qpf, gust_series=gust, temperature_series=temp)
                    zero = [{"validTime": f"{base_utc.isoformat()}/P4D", "value": 0}]
                    nws.update(snow_raw_values=zero, ice_raw_values=zero)
                    rows = build_timeline(nws, base, 0.05, 5, "General", base.date().isoformat(), 1)
                    for row in rows:
                        offset = row["forecast_hour"]
                        duration = row["interval_hours"]
                        start = base_utc + dt.timedelta(hours=offset)
                        end = start + dt.timedelta(hours=duration)
                        self.assertEqual(row["valid_time"], start.astimezone(EASTERN).isoformat())
                        self.assertEqual(row["interval_end"], end.astimezone(EASTERN).isoformat())
                        self.assertAlmostEqual(
                            row["interval_qpf_in"], sum((h + 1) / 1000 for h in range(offset, offset + duration))
                        )
                    self.assertAlmostEqual(
                        sum(row["interval_qpf_in"] for row in rows), sum((h + 1) / 1000 for h in range(72))
                    )

    def test_daily_is_seven_planning_periods_and_future_is_partial(self) -> None:
        nws = self._outlook_fixture()
        now = dt.datetime(2026, 10, 10, 9, 38, tzinfo=EASTERN)
        daily = build_daily(
            nws,
            now,
            rainfall_72_in=0.05,
            downburst_index=29,
            spc_category="General",
            downburst_valid_date="2026-10-10",
            load_stress_level=1,
        )
        self.assertEqual(len(daily), 7)
        self.assertEqual(daily[0]["label"], "Today")
        self.assertEqual(daily[0]["downburst_status"], "Available")
        self.assertEqual(daily[1]["downburst_status"], "Not Assessed")
        self.assertEqual(daily[1]["data_status"], "Partial")
        self.assertIn("planning floor only", daily[1]["note"])
        self.assertAlmostEqual(daily[0]["qpf_24_in"], 1.2, places=6)

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
