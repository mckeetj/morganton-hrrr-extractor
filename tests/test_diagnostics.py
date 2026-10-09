import unittest

import numpy as np

from burke_hrrr.config import BURKE_BOUNDS
from burke_hrrr.diagnostics import (
    bulk_shear_ms,
    interpolate_at_height,
    layer_lapse_rate_k_per_km,
    summarize,
)
from burke_hrrr.decode import Field
from burke_hrrr.derived import (
    derive_diagnostics,
    dewpoint_from_rh_k,
    relative_humidity_from_t_td_percent,
)


class DiagnosticTests(unittest.TestCase):
    def test_lapse_rate(self) -> None:
        result = layer_lapse_rate_k_per_km(
            np.array([290.0]),
            np.array([270.0]),
            np.array([3000.0]),
            np.array([6000.0]),
        )
        self.assertAlmostEqual(float(result[0]), 6.6667, places=3)

    def test_interpolate_columns(self) -> None:
        heights = np.array([[[0.0, 0.0]], [[1000.0, 2000.0]], [[3000.0, 4000.0]]])
        values = np.array([[[10.0, 10.0]], [[20.0, 30.0]], [[40.0, 50.0]]])
        result = interpolate_at_height(heights, values, np.array([[2000.0, 1000.0]]))
        np.testing.assert_allclose(result, [[30.0, 20.0]])

    def test_bulk_shear(self) -> None:
        result = bulk_shear_ms(
            np.array([0.0]),
            np.array([0.0]),
            np.array([3.0]),
            np.array([4.0]),
        )
        self.assertEqual(float(result[0]), 5.0)

    def test_summary_ignores_missing(self) -> None:
        result = summarize(np.array([1.0, 2.0, np.nan, 9.0]))
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["max"], 9.0)

    def test_dewpoint_from_rh(self) -> None:
        dewpoint = dewpoint_from_rh_k(np.array([293.15]), np.array([50.0]))
        self.assertAlmostEqual(float(dewpoint[0] - 273.15), 9.26, places=1)

    def test_relative_humidity_from_temperature_and_dewpoint(self) -> None:
        rh = relative_humidity_from_t_td_percent(
            np.array([293.15]),
            np.array([283.15]),
        )
        self.assertAlmostEqual(float(rh[0]), 52.5, places=1)

    def test_v2_pressure_level_diagnostics(self) -> None:
        lat = np.array([[35.75]])
        lon = np.array([[-81.70]])

        def pressure_field(name: str, level: float, value: float, units: str) -> Field:
            return Field(
                name,
                "isobaricInhPa",
                level,
                "instant",
                units,
                np.array([[value]], dtype=float),
                lat,
                lon,
            )

        pressure = [
            pressure_field("t", 700, 283.0, "K"),
            pressure_field("t", 500, 269.0, "K"),
            pressure_field("dpt", 700, 273.0, "K"),
            pressure_field("gh", 700, 3000.0, "gpm"),
            pressure_field("gh", 500, 5600.0, "gpm"),
        ]
        derived = derive_diagnostics([], pressure, BURKE_BOUNDS)

        lapse = derived["lapse_rate_700_500mb"]["summary"]["max"]
        self.assertAlmostEqual(lapse, 5.3846, places=3)
        self.assertEqual(
            derived["dewpoint_depression_700mb"]["summary"]["max"],
            10.0,
        )
        rh = derived["relative_humidity_700mb"]["summary"]["max"]
        self.assertTrue(45.0 < rh < 55.0)


    def test_provisional_mwpi_downburst_candidate(self) -> None:
        lat = np.array([[35.75]])
        lon = np.array([[-81.70]])

        def fld(name, type_of_level, level, value, units):
            return Field(
                name,
                type_of_level,
                level,
                "instant",
                units,
                np.array([[value]], dtype=float),
                lat,
                lon,
            )

        surface = [
            fld("cape", "surface", 0, 2000.0, "J kg-1"),
        ]
        pressure = [
            fld("t", "isobaricInhPa", 850, 300.0, "K"),
            fld("dpt", "isobaricInhPa", 850, 290.0, "K"),
            fld("gh", "isobaricInhPa", 850, 1500.0, "gpm"),
            fld("t", "isobaricInhPa", 700, 290.0, "K"),
            fld("dpt", "isobaricInhPa", 700, 285.0, "K"),
            fld("gh", "isobaricInhPa", 700, 3000.0, "gpm"),
            fld("t", "isobaricInhPa", 500, 270.0, "K"),
            fld("dpt", "isobaricInhPa", 500, 265.0, "K"),
            fld("gh", "isobaricInhPa", 500, 5600.0, "gpm"),
        ]

        derived = derive_diagnostics(surface, pressure, BURKE_BOUNDS)
        mwpi = derived["mwpi_environment"]["summary"]["max"]
        candidate = derived["downburst_index_candidate_0_100"]["summary"]["max"]

        self.assertAlmostEqual(mwpi, 4.3711, places=3)
        self.assertAlmostEqual(candidate, 87.4220, places=3)
        self.assertIn(
            "not approved",
            derived["downburst_index_candidate_0_100"]["method"].lower(),
        )

    def test_v32_surface_to_500mb_shear_proxy(self) -> None:
        lat = np.array([[35.75]])
        lon = np.array([[-81.70]])

        def fld(name, type_of_level, level, value, units="m s^-1"):
            return Field(
                name, type_of_level, level, "instant", units,
                np.array([[value]], dtype=float), lat, lon,
            )

        surface = [
            fld("10u", "heightAboveGround", 10, 2.0),
            fld("10v", "heightAboveGround", 10, 1.0),
        ]
        pressure = [
            fld("u", "isobaricInhPa", 500, 14.0),
            fld("v", "isobaricInhPa", 500, 7.0),
        ]
        derived = derive_diagnostics(surface, pressure, BURKE_BOUNDS)
        metric = derived["bulk_shear_sfc_500mb"]
        expected_kt = ((12.0 ** 2 + 6.0 ** 2) ** 0.5) * 1.943844
        self.assertAlmostEqual(metric["summary"]["max"], expected_kt, places=3)
        self.assertIn("proxy", metric["method"])
        self.assertNotIn("bulk_shear_0_6km", derived)


if __name__ == "__main__":
    unittest.main()
