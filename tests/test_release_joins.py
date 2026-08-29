from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "lab"))

from release_data import load_release_data  # noqa: E402


class ReleaseJoinTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="co2gap-joins-")
        self.root = Path(self.temp.name)
        self.dec = self.root / "decomposition"
        self.ground = self.root / "ground"
        self.dec.mkdir()
        self.ground.mkdir()
        self.calibration = self.root / "calibration.json"
        self.calibration.write_text(json.dumps({"factors": {"A320": 1.0}}))

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def decomposition(day: str, ids: list[int]) -> pd.DataFrame:
        return pd.DataFrame({
            "day": [day] * len(ids), "flight_id": ids,
            "typecode": ["A320"] * len(ids),
            "gc_km": [200.0] * len(ids), "flown_km": [210.0] * len(ids),
            "co2_kg_v0": [100.0] * len(ids),
            "ideal_gc_co2_kg": [80.0] * len(ids),
            "hybrid_co2_kg": [90.0] * len(ids),
        })

    @staticmethod
    def ground_frame(day: str, ids: list[int]) -> pd.DataFrame:
        return pd.DataFrame({
            "day": [day] * len(ids), "flight_id": ids,
            "fuel_recomputed_kg": [100.0] * len(ids),
            "fuel_a3000t70_kg": [10.0] * len(ids),
            "fuel_a3000t70_dep_kg": [4.0] * len(ids),
            "fuel_a3000t70_arr_kg": [6.0] * len(ids),
        })

    def load(self):
        return load_release_data(
            self.dec, self.ground, self.calibration,
            manifest=None, verify_manifest=False)

    def test_missing_ground_day_is_rejected(self):
        self.decomposition("2026-01-01", [1]).to_parquet(
            self.dec / "2026-01-01.parquet")
        self.ground_frame("2026-01-02", [1]).to_parquet(
            self.ground / "2026-01-02.parquet")
        with self.assertRaisesRegex(SystemExit, "quota di terra assente.*giorni"):
            self.load()

    def test_missing_flight_in_join_is_rejected(self):
        self.decomposition("2026-01-01", [1, 2]).to_parquet(
            self.dec / "2026-01-01.parquet")
        self.ground_frame("2026-01-01", [1]).to_parquet(
            self.ground / "2026-01-01.parquet")
        with self.assertRaisesRegex(SystemExit, "1 assenti non autorizzati"):
            self.load()


if __name__ == "__main__":
    unittest.main()
