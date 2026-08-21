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

    def test_v2_direct_shear_components(self) -> None:
        lat = np.array([[35.75]])
        lon = np.array([[-81.70]])

        def shear_field(name: str, value: float, depth: float) -> Field:
            return Field(
                name,
                "heightAboveGroundLayer",
                depth,
                "instant",
                "s^-1",
                np.array([[value]], dtype=float),
                lat,
                lon,
                # Simulate cfgrib grouping behavior: top/bottom metadata can
                # reflect the first layer even when the split coordinate is
                # the 6000-m layer. Field.level must therefore take priority.
                top_level=0.0,
                bottom_level=1000.0,
            )

        # Components 0.005 and 0.010 s^-1 over 6 km imply a vector difference
        # of about 67.1 m/s = 130.4 kt.
        surface = [
            shear_field("vucsh", 0.005, 6000.0),
            shear_field("vvcsh", 0.010, 6000.0),
        ]
        derived = derive_diagnostics(surface, [], BURKE_BOUNDS)
        shear = derived["bulk_shear_0_6km"]["summary"]["max"]
        self.assertAlmostEqual(shear, 130.4, places=1)


if __name__ == "__main__":
    unittest.main()
