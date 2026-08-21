import unittest

import numpy as np

from burke_hrrr.diagnostics import bulk_shear_ms, interpolate_at_height, layer_lapse_rate_k_per_km, summarize
from burke_hrrr.decode import Field
from burke_hrrr.derived import derive_diagnostics, dewpoint_from_rh_k


class DiagnosticTests(unittest.TestCase):
    def test_lapse_rate(self) -> None:
        result = layer_lapse_rate_k_per_km(
            np.array([290.0]), np.array([270.0]), np.array([3000.0]), np.array([6000.0])
        )
        self.assertAlmostEqual(float(result[0]), 6.6667, places=3)

    def test_interpolate_columns(self) -> None:
        heights = np.array([[[0.0, 0.0]], [[1000.0, 2000.0]], [[3000.0, 4000.0]]])
        values = np.array([[[10.0, 10.0]], [[20.0, 30.0]], [[40.0, 50.0]]])
        result = interpolate_at_height(heights, values, np.array([[2000.0, 1000.0]]))
        np.testing.assert_allclose(result, [[30.0, 20.0]])

    def test_bulk_shear(self) -> None:
        result = bulk_shear_ms(np.array([0.0]), np.array([0.0]), np.array([3.0]), np.array([4.0]))
        self.assertEqual(float(result[0]), 5.0)

    def test_summary_ignores_missing(self) -> None:
        result = summarize(np.array([1.0, 2.0, np.nan, 9.0]))
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["max"], 9.0)

    def test_dewpoint_from_rh(self) -> None:
        dewpoint = dewpoint_from_rh_k(np.array([293.15]), np.array([50.0]))
        self.assertAlmostEqual(float(dewpoint[0] - 273.15), 9.26, places=1)

    def test_pressure_level_diagnostics(self) -> None:
        lat = np.array([[35.7]])
        lon = np.array([[-81.7]])

        def field(name, level, values, units):
            return Field(name, "isobaricInhPa", level, "instant", units,
                         np.array([[values]], dtype=float), lat, lon)

        pressure = [
            field("t", 700, 283.0, "K"), field("t", 600, 276.0, "K"),
            field("t", 500, 269.0, "K"), field("r", 700, 50.0, "%"),
            field("r", 600, 40.0, "%"), field("r", 500, 30.0, "%"),
            field("gh", 700, 3000.0, "gpm"), field("gh", 600, 4300.0, "gpm"),
            field("gh", 500, 5600.0, "gpm"),
        ]
        derived = derive_diagnostics([], pressure)
        lapse = derived["lapse_rate_700_500mb"]["summary"]["max"]
        self.assertAlmostEqual(lapse, 5.3846, places=3)
        self.assertEqual(derived["minimum_rh_700_500mb"]["summary"]["min"], 30.0)


if __name__ == "__main__":
    unittest.main()
