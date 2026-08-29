from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "pipeline"), str(ROOT / "ingest"),
                str(ROOT / "lab"), str(ROOT)]

from decompose import _bounded_cruise_alt_ft  # noqa: E402
from uncertainty import (UncertaintyError, _metrics, block_resample_days,  # noqa: E402
                         _require_outside_repository, stratified_sample,
                         validate_registry,
                         validate_scenarios)


class RegistryTests(unittest.TestCase):
    def test_tracked_register_and_scenarios_validate(self):
        registry = json.loads((ROOT / "uncertainty-register.json").read_text())
        scenarios = json.loads((ROOT / "uncertainty-scenarios.json").read_text())
        self.assertEqual(validate_registry(registry), {"estimands": 6, "sources": 15})
        self.assertEqual(validate_scenarios(scenarios)["nominal"], "nominal")

    def test_quantified_source_requires_a_range(self):
        registry = json.loads((ROOT / "uncertainty-register.json").read_text())
        broken = copy.deepcopy(registry)
        broken["sources"][0]["status"] = "quantified"
        broken["sources"][0]["range"] = None
        with self.assertRaisesRegex(UncertaintyError, "has no range"):
            validate_registry(broken)

    def test_scenario_values_are_bounded_and_diagnostic(self):
        scenarios = json.loads((ROOT / "uncertainty-scenarios.json").read_text())
        broken = copy.deepcopy(scenarios)
        broken["scenarios"][0]["load_factor"] = 1.1
        with self.assertRaisesRegex(UncertaintyError, "between 0 and 1"):
            validate_scenarios(broken)
        broken = copy.deepcopy(scenarios)
        broken["publication_status"] = "confidence_interval"
        with self.assertRaisesRegex(UncertaintyError, "diagnostic_only"):
            validate_scenarios(broken)


class SamplingTests(unittest.TestCase):
    @staticmethod
    def population():
        rows = []
        fid = 0
        for typecode, distance, coverage, n in (
            ("A320", 400.0, 1.0, 7),
            ("A320", 900.0, 0.95, 3),
            ("B738", 400.0, 1.0, 5),
        ):
            for i in range(n):
                rows.append({
                    "day": f"2026-01-{i + 1:02d}", "flight_id": fid,
                    "typecode": typecode, "gc_km": distance,
                    "coverage_frac": coverage,
                })
                fid += 1
        return pd.DataFrame(rows)

    def test_sample_is_deterministic_and_weights_rebuild_population(self):
        population = self.population()
        first = stratified_sample(population, per_stratum=2, seed=17)
        second = stratified_sample(population, per_stratum=2, seed=17)
        pd.testing.assert_frame_equal(first, second)
        self.assertAlmostEqual(first.weight.sum(), len(population))
        self.assertTrue((first.sample_n <= first.population_n).all())

    def test_duplicate_keys_are_rejected(self):
        population = self.population()
        population.loc[1, ["day", "flight_id"]] = population.loc[0, ["day", "flight_id"]]
        with self.assertRaisesRegex(UncertaintyError, "duplicate"):
            stratified_sample(population, per_stratum=2, seed=17)

    def test_per_flight_sample_cannot_be_written_inside_repository(self):
        with self.assertRaisesRegex(UncertaintyError, "outside the repository"):
            _require_outside_repository(ROOT / "sample.json", "sample")
        _require_outside_repository(Path("/tmp/co2gap-sample.json"), "sample")


class MetricTests(unittest.TestCase):
    @staticmethod
    def frame():
        return pd.DataFrame({
            "day": ["2026-01-01", "2026-01-02", "2026-01-03"],
            "co2_kg_v0": [120.0, 260.0, 165.0],
            "ideal_gc_co2_kg": [100.0, 200.0, 150.0],
            "hybrid_co2_kg": [110.0, 230.0, 158.0],
            "co2_real_kg": [132.0, 260.0, 156.75],
            "co2_ideal_kg": [110.0, 200.0, 142.5],
            "co2_hybrid_kg": [121.0, 230.0, 150.1],
        })

    def test_headline_components_are_additive(self):
        result = _metrics(self.frame())
        self.assertAlmostEqual(
            result["gap_total_pct"],
            result["gap_lateral_pct"] + result["gap_vertical_pct"],
        )
        self.assertNotEqual(
            result["gap_total_pct"], result["gap_total_pct_calibrated"])

    def test_day_block_resampling_is_deterministic(self):
        daily = self.frame()
        first = block_resample_days(daily, iterations=50, seed=9)
        second = block_resample_days(daily, iterations=50, seed=9)
        self.assertEqual(first, second)
        for quantiles in first.values():
            self.assertLessEqual(quantiles["q05"], quantiles["q50"])
            self.assertLessEqual(quantiles["q50"], quantiles["q95"])


class BaselineHookTests(unittest.TestCase):
    def test_zero_offset_is_identity_and_offsets_are_ceiling_bounded(self):
        aircraft = {"ceiling": 12000.0}
        base = 35000.0
        self.assertEqual(_bounded_cruise_alt_ft(aircraft, base, 0.0), base)
        self.assertEqual(_bounded_cruise_alt_ft(aircraft, base, -1000.0), 34000.0)
        ceiling_margin_ft = (12000.0 - 500.0) / 0.3048
        self.assertAlmostEqual(
            _bounded_cruise_alt_ft(aircraft, base, 10000.0), ceiling_margin_ft)
        self.assertEqual(_bounded_cruise_alt_ft({}, base, 0.0), base)


if __name__ == "__main__":
    unittest.main()
