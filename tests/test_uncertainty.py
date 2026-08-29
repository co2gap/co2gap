from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "pipeline"), str(ROOT / "ingest"),
                str(ROOT / "lab"), str(ROOT)]

from decompose import _bounded_cruise_alt_ft  # noqa: E402
from uncertainty import (UncertaintyError, _metrics, _require_nested_counts,  # noqa: E402
                         block_resample_days,
                         _require_outside_repository, poststratified_selection,
                         stratified_sample,
                         selection_audit, selection_flags, validate_registry,
                         validate_scenarios)


class RegistryTests(unittest.TestCase):
    def test_tracked_register_and_scenarios_validate(self):
        registry = json.loads((ROOT / "uncertainty-register.json").read_text())
        scenarios = json.loads((ROOT / "uncertainty-scenarios.json").read_text())
        self.assertEqual(validate_registry(registry), {"estimands": 6, "sources": 16})
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


class SelectionTests(unittest.TestCase):
    @staticmethod
    def population():
        rows = []
        for flight_id in range(10):
            rows.append({
                "day": "2026-01-01", "flight_id": flight_id,
                "typecode": "A320",
                "origin_icao": None if flight_id == 0 else "LIRF",
                "dest_icao": "LIMC", "gc_km": 500.0,
                "flown_km": 520.0,
                "coverage_frac": 0.80 if flight_id == 1 else 0.99,
                "max_gap_s": 180.0, "flown_ge_09gc": True,
                "co2_kg_v0": 1000.0,
            })
        return pd.DataFrame(rows)

    def test_flags_preserve_overlapping_failure_dimensions(self):
        frame = self.population()
        frame.loc[0, "coverage_frac"] = 0.80
        flags = selection_flags(frame, coverage_min=0.85, gc_min_km=150.0)
        self.assertFalse(flags.loc[0, "endpoints_resolved"])
        self.assertFalse(flags.loc[0, "coverage_sufficient"])
        self.assertEqual(int(flags.all(axis=1).sum()), 8)

    def test_audit_rebuilds_exact_decomposition_keyset(self):
        with tempfile.TemporaryDirectory(prefix="co2gap-selection-") as raw:
            root = Path(raw)
            flights_dir = root / "flights"
            decomposition_dir = root / "decomposition"
            (flights_dir / "2026-01-01").mkdir(parents=True)
            decomposition_dir.mkdir()
            self.population().to_parquet(
                flights_dir / "2026-01-01" / "flights.parquet", index=False)
            pd.DataFrame({"flight_id": list(range(2, 10))}).to_parquet(
                decomposition_dir / "2026-01-01.parquet", index=False)
            manifest = root / "release-manifest.json"
            manifest.write_text(json.dumps({
                "schema_version": 1,
                "release": {"id": "test", "days": ["2026-01-01"]},
                "configuration": {"track_quality": {
                    "gap_threshold_s": 120.0,
                    "coverage_min_fraction": 0.85,
                    "flown_min_fraction": 0.9,
                    "great_circle_min_km": 150,
                }},
            }))
            result = selection_audit(
                manifest_path=manifest, flights_dir=flights_dir,
                decomposition_dir=decomposition_dir, min_group_n=10,
                verify=False)
            self.assertEqual(result["coverage_statement"]["source"]["flights"], 10)
            self.assertEqual(result["coverage_statement"]["retained"]["flights"], 8)
            self.assertTrue(result["gate"]["exact_keyset_match_to_decomposition"])

            pd.DataFrame({"flight_id": list(range(1, 10))}).to_parquet(
                decomposition_dir / "2026-01-01.parquet", index=False)
            with self.assertRaisesRegex(UncertaintyError, "1 extra"):
                selection_audit(
                    manifest_path=manifest, flights_dir=flights_dir,
                    decomposition_dir=decomposition_dir, min_group_n=10,
                    verify=False)

    def test_poststratification_reports_support_and_closes_target(self):
        frame = pd.DataFrame({
            "typecode": ["A320", "A320", "B738", "B738"],
            "gc_km": [400.0, 400.0, 900.0, 900.0],
            "co2_kg_v0": [120.0, 120.0, 260.0, 260.0],
            "ideal_gc_co2_kg": [100.0, 100.0, 200.0, 200.0],
            "hybrid_co2_kg": [110.0, 110.0, 230.0, 230.0],
            "co2_real_kg": [120.0, 120.0, 260.0, 260.0],
            "co2_ideal_kg": [100.0, 100.0, 200.0, 200.0],
            "co2_hybrid_kg": [110.0, 110.0, 230.0, 230.0],
        })
        result = poststratified_selection(
            frame, pd.Series([True, False, True, False]))
        diagnostics = result["diagnostics"]
        self.assertEqual(diagnostics["strata"], 2)
        self.assertEqual(diagnostics["supported_strata"], 2)
        self.assertEqual(diagnostics["support_share_of_nominal_ideal_co2"], 1.0)
        self.assertEqual(diagnostics["maximum_weight"], 2.0)
        self.assertAlmostEqual(
            diagnostics["target_ideal_co2_closure_relative"], 0.0)

        partial = poststratified_selection(
            frame, pd.Series([True, False, False, False]))
        self.assertAlmostEqual(
            partial["diagnostics"]["support_share_of_nominal_ideal_co2"],
            1.0 / 3.0)

    def test_selection_stress_guards_fail_loudly(self):
        frame = pd.DataFrame({
            "typecode": ["A320"], "gc_km": [400.0],
            "co2_kg_v0": [120.0], "ideal_gc_co2_kg": [100.0],
            "hybrid_co2_kg": [110.0], "co2_real_kg": [120.0],
            "co2_ideal_kg": [100.0], "co2_hybrid_kg": [110.0],
        })
        with self.assertRaisesRegex(UncertaintyError, "retains no flights"):
            poststratified_selection(frame, pd.Series([False]))
        broken = frame.copy()
        broken.loc[0, "ideal_gc_co2_kg"] = -1.0
        with self.assertRaisesRegex(UncertaintyError, "non-positive"):
            poststratified_selection(broken, pd.Series([True]))
        with self.assertRaisesRegex(UncertaintyError, "not nested"):
            _require_nested_counts(
                {"loose": 10, "strict": 11}, [("loose", "strict")])


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
