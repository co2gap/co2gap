from __future__ import annotations

import unittest

import numpy as np
import xarray as xr

from wind.era5 import (ERA5ValidationError, WindField, required_wind_days,
                       validate_era5_dataset)


class Era5Tests(unittest.TestCase):
    DAY = "2026-01-01"

    @staticmethod
    def dataset(hours: int) -> xr.Dataset:
        dims = ("valid_time", "pressure_level", "latitude", "longitude")
        shape = (hours, 1, 2, 2)
        times = np.datetime64(Era5Tests.DAY, "h") + np.arange(hours).astype(
            "timedelta64[h]")
        return xr.Dataset(
            {"u": (dims, np.zeros(shape, dtype=np.float32)),
             "v": (dims, np.ones(shape, dtype=np.float32))},
            coords={"valid_time": times, "pressure_level": [1000.0],
                    "latitude": [1.0, 0.0], "longitude": [0.0, 1.0]})

    def validate(self, dataset):
        validate_era5_dataset(
            dataset, self.DAY, levels=[1000], area=[1, 0, 0, 1], grid=[1, 1])

    def test_exact_day_passes_and_23_hours_fail(self):
        self.validate(self.dataset(24))
        with self.assertRaisesRegex(ERA5ValidationError, "exactly 00:00..23:00"):
            self.validate(self.dataset(23))

    def test_adjacent_day_is_required_for_samples_after_2300(self):
        start = np.datetime64(self.DAY, "s").astype(np.int64)
        sample = np.array([start + 23 * 3600 + 30 * 60,
                           start + 24 * 3600 + 12 * 60], dtype=float)
        field = WindField.__new__(WindField)
        field.times = start + np.arange(24) * 3600
        with self.assertRaisesRegex(ValueError, "outside available hourly coverage"):
            field._require_time_coverage(sample)
        field.times = start + np.arange(48) * 3600
        field._require_time_coverage(sample)
        self.assertEqual(
            required_wind_days([self.DAY]), ["2026-01-01", "2026-01-02"])


if __name__ == "__main__":
    unittest.main()
