from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ingest"))
sys.path.insert(0, str(ROOT / "pipeline"))

from source import BBox  # noqa: E402
from trajectories import (flights_from_trace, flights_from_trace_with_audit,  # noqa: E402
                          selection_configuration)


class TrajectorySelectionFunnelTests(unittest.TestCase):
    BOX = BBox(lat_min=35.0, lat_max=52.0, lon_min=-10.0, lon_max=25.0)

    @staticmethod
    def trace(*, points: int = 30, step_s: int = 60) -> dict:
        rows = []
        for index in range(points):
            altitude = 0.0 if index in {0, points - 1} else 20000.0
            rows.append([
                index * step_s, 45.0 + index * 0.001, 9.0,
                altitude, 250.0, 0.0, 0, 0.0, {}, "adsb",
                None, None, 230.0, 0.0,
            ])
        return {
            "icao": "fixture", "t": "A320", "r": None,
            "timestamp": 1_700_000_000.0, "trace": rows,
        }

    def test_complete_trace_preserves_existing_flight_output(self):
        raw = self.trace()
        audited, counts = flights_from_trace_with_audit(raw, self.BOX)
        ordinary = flights_from_trace(raw, self.BOX)
        self.assertEqual(len(audited), 1)
        self.assertEqual(len(ordinary), 1)
        self.assertEqual(counts, {"legs_total": 1, "complete_flights": 1})
        self.assertEqual(selection_configuration()["min_points"], 30)

    def test_each_leg_has_one_exclusive_outcome(self):
        _, short = flights_from_trace_with_audit(
            self.trace(step_s=10), self.BOX)
        self.assertEqual(short["legs_total"], 1)
        self.assertEqual(short["legs_rejected_duration_too_short"], 1)
        _, sparse = flights_from_trace_with_audit(
            self.trace(points=29), self.BOX)
        self.assertEqual(sparse["legs_total"], 1)
        self.assertEqual(sparse["legs_rejected_too_few_points"], 1)


if __name__ == "__main__":
    unittest.main()
