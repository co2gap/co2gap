from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "pipeline"), str(ROOT / "ingest"),
                str(ROOT / "lab"), str(ROOT)]

from decompose import _sensitivity_cruise_alt_ft  # noqa: E402
from release_manifest import ReleaseManifest, sha256_file  # noqa: E402
from uncertainty import (UncertaintyError, _metrics, _require_nested_counts,  # noqa: E402
                         _require_nominal_baseline,
                         balanced_sensitivity_allocation, _sample_allocation_options,
                         write_sample_manifest,
                         block_resample_days,
                         paired_sensitivity,
                         _require_outside_repository, poststratified_selection,
                         selection_validation_result, stratified_sample,
                         stratified_selection_validation_sample,
                         selection_audit, selection_flags,
                         selection_sensitivity, validate_registry,
                         validate_scenarios, validate_selection_design,
                         validate_selection_sensitivity_design,
                         targeted_validation_result,
                         validate_targeted_validation_design,
                         validate_targeted_validation_registration,
                         verify_registered_selection_artifacts,
                         verify_registered_targeted_artifacts,
                         write_targeted_validation_artifacts,
                         write_selection_validation_artifacts)


class RegistryTests(unittest.TestCase):
    def test_tracked_register_and_scenarios_validate(self):
        registry = json.loads((ROOT / "uncertainty-register.json").read_text())
        scenarios = json.loads((ROOT / "uncertainty-scenarios.json").read_text())
        design = json.loads((ROOT / "selection-validation-design.json").read_text())
        sensitivity_design = json.loads(
            (ROOT / "selection-sensitivity-design.json").read_text())
        targeted_design = json.loads(
            (ROOT / "targeted-validation-design.json").read_text())
        targeted_registration = json.loads(
            (ROOT / "targeted-validation-registration.json").read_text())
        self.assertEqual(validate_registry(registry), {"estimands": 6, "sources": 16})
        self.assertEqual(validate_scenarios(scenarios)["nominal"], "nominal")
        self.assertEqual(
            validate_selection_design(design, ROOT / "release-manifest.json"),
            {"population_rows": 2115824, "sample_rows": 5000, "strata": 843})
        self.assertEqual(
            validate_selection_sensitivity_design(
                sensitivity_design,
                ROOT / "selection-sensitivity-design.json"),
            {"stress_levels": 3, "mapped_masks": 9})
        self.assertEqual(
            validate_targeted_validation_design(
                targeted_design, ROOT / "targeted-validation-design.json"),
            {"target_rows": 2279, "masks": 4})
        self.assertEqual(
            validate_targeted_validation_registration(
                targeted_registration, targeted_design,
                ROOT / "targeted-validation-design.json"),
            {"sample_rows": 2279, "masks": 4})

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

    def test_selection_design_cannot_overclaim_or_break_its_partition(self):
        design = json.loads((ROOT / "selection-validation-design.json").read_text())
        broken = copy.deepcopy(design)
        broken["primary_headline_bias_bounded"] = True
        with self.assertRaisesRegex(UncertaintyError, "cannot claim"):
            validate_selection_design(broken, ROOT / "release-manifest.json")
        broken = copy.deepcopy(design)
        broken["by_failure_mask"][0]["sample_rows"] -= 1
        with self.assertRaisesRegex(UncertaintyError, "do not close on sample"):
            validate_selection_design(broken, ROOT / "release-manifest.json")


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


class BalancedSensitivitySamplingTests(unittest.TestCase):
    def test_exact_integer_quotas_ties_and_census(self):
        sizes = pd.Series({"small": 1, "a": 10, "b": 10, "c": 30})
        result = balanced_sensitivity_allocation(sizes, minimum=2, target=16)
        # Floor=7; remaining 9 apportioned over capacities 0,8,8,28.
        self.assertEqual(result.to_dict(), {"a": 4, "b": 3, "c": 8, "small": 1})
        pd.testing.assert_series_equal(result, balanced_sensitivity_allocation(
            sizes.iloc[::-1], minimum=2, target=16))
        self.assertEqual(balanced_sensitivity_allocation(
            sizes, minimum=2, target=7).to_dict(), {"a": 2, "b": 2, "c": 2, "small": 1})
        pd.testing.assert_series_equal(balanced_sensitivity_allocation(
            sizes, minimum=2, target=51), sizes.sort_index())
        self.assertEqual(balanced_sensitivity_allocation(
            pd.Series({"only": 1}), minimum=2, target=1).iloc[0], 1)

    def test_random_allocations_close_without_overfilling_or_losing_strata(self):
        rng = np.random.default_rng(193)
        for _ in range(100):
            sizes = pd.Series(rng.integers(1, 80, size=12), index=[f"s{i}" for i in range(12)])
            minimum = int(rng.integers(2, 6))
            floor = sizes.clip(upper=minimum)
            target = int(rng.integers(int(floor.sum()), int(sizes.sum()) + 1))
            result = balanced_sensitivity_allocation(sizes, minimum=minimum, target=target)
            self.assertEqual(int(result.sum()), target)
            self.assertTrue((result >= floor.reindex(result.index)).all())
            self.assertTrue((result <= sizes.reindex(result.index)).all())

    def test_invalid_counts_and_impossible_targets_fail(self):
        good = pd.Series({"a": 10, "b": 20})
        for minimum, target in ((1, 10), (2.5, 10), (True, 10), (2, 3),
                                 (2, 31), (2, 0), (2, 8.5), (2, True)):
            with self.subTest(minimum=minimum, target=target):
                with self.assertRaises(UncertaintyError):
                    balanced_sensitivity_allocation(good, minimum=minimum, target=target)
        for sizes in (pd.Series(dtype=int), pd.Series([3, 4], index=["a", "a"]),
                      pd.Series({"a": 0}), pd.Series({"a": 3.2}), pd.Series({"a": True})):
            with self.assertRaises(UncertaintyError):
                balanced_sensitivity_allocation(sizes, minimum=2, target=4)

    def test_draw_reproducible_after_row_shuffle_and_preserves_every_cell(self):
        population = SamplingTests.population()
        first = stratified_sample(population, per_stratum=2, seed=19, target_sample=10)
        shuffled = stratified_sample(population.sample(frac=1, random_state=2),
                                     per_stratum=2, seed=19, target_sample=10)
        pd.testing.assert_frame_equal(first, shuffled)
        self.assertEqual(len(first), 10)
        self.assertEqual(first.stratum.nunique(), 3)
        self.assertAlmostEqual(first.weight.sum(), len(population))
        self.assertFalse(first.duplicated(["day", "flight_id"]).any())
        self.assertTrue((first.sample_n >= 2).all())
        for _, group in first.groupby("stratum"):
            self.assertEqual(group.sample_n.unique().tolist(), [len(group)])
            self.assertAlmostEqual(group.weight.sum(), float(group.population_n.iloc[0]))

    def test_invalid_numeric_strata_cannot_silently_become_a_nan_cell(self):
        for column in ("gc_km", "coverage_frac"):
            for value in ("bad", float("nan"), float("inf"), -float("inf")):
                population = SamplingTests.population().astype({column: object})
                population.loc[0, column] = value
                with self.subTest(column=column, value=value):
                    with self.assertRaisesRegex(UncertaintyError, "non-numeric or non-finite"):
                        stratified_sample(population, per_stratum=2, seed=1, target_sample=10)
            nullable = SamplingTests.population().astype({column: "Float64"})
            nullable.loc[0, column] = pd.NA
            with self.assertRaisesRegex(UncertaintyError, "non-numeric or non-finite"):
                stratified_sample(nullable, per_stratum=2, seed=1, target_sample=10)

    def test_option_combinations_are_explicit(self):
        def options(**kwargs):
            return SimpleNamespace(**dict(
                dict(allocation="equal", per_stratum=None, min_per_stratum=None,
                     target_sample=None), **kwargs))
        self.assertEqual(_sample_allocation_options(options()), (5, None))
        self.assertEqual(_sample_allocation_options(options(per_stratum=7)), (7, None))
        self.assertEqual(_sample_allocation_options(options(allocation="balanced", target_sample=20)), (2, 20))
        self.assertEqual(_sample_allocation_options(options(allocation="balanced", target_sample=20, min_per_stratum=3)), (3, 20))
        for args in (options(target_sample=20), options(min_per_stratum=2),
                     options(allocation="balanced"), options(allocation="balanced", per_stratum=5, target_sample=20),
                     options(allocation="balanced", target_sample=20, min_per_stratum=1),
                     options(allocation="balanced", target_sample=-1), options(allocation="unknown")):
            with self.assertRaises(UncertaintyError):
                _sample_allocation_options(args)

    def test_manifest_names_the_new_allocation_and_does_not_call_minimum_a_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            manifest_path = base / "release.json"
            manifest_path.write_text("{}")
            manifest = SimpleNamespace(path=manifest_path, release_id="test")
            population = SamplingTests.population()
            sample = stratified_sample(population, per_stratum=2, seed=17, target_sample=10)
            result = write_sample_manifest(manifest=manifest, population=population,
                                           sample=sample, per_stratum=2, seed=17,
                                           output=base / "sample.json", verified=False,
                                           target_sample=10)
            self.assertNotIn("per_stratum", result)
            self.assertEqual(result["sampling_design"]["minimum_per_stratum"], 2)
            self.assertEqual(result["sampling_design"]["target_sample_rows"], 10)
            self.assertFalse(result["sampling_design"]["uses_sensitivity_outcomes"])
            with self.assertRaisesRegex(UncertaintyError, "row count differs"):
                write_sample_manifest(manifest=manifest, population=population,
                                      sample=sample, per_stratum=2, seed=17,
                                      output=base / "bad.json", verified=False,
                                      target_sample=11)
            self.assertFalse((base / "bad.json").exists())
            equal = stratified_sample(population, per_stratum=2, seed=17)
            old = write_sample_manifest(manifest=manifest, population=population,
                                       sample=equal, per_stratum=2, seed=17,
                                       output=base / "old.json", verified=False)
            self.assertIn("per_stratum", old)
            self.assertNotIn("sampling_design", old)


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


class SelectionSensitivityTests(unittest.TestCase):
    @staticmethod
    def inputs(root: Path) -> tuple[Path, Path]:
        names = (
            "release-manifest.json", "release-headlines.json",
            "selection-validation-design.json", "opensky-day-audit-result.json",
        )
        for name in names:
            (root / name).write_bytes((ROOT / name).read_bytes())
        validation = json.loads(
            (root / "selection-validation-design.json").read_text())
        combinations = []
        for row in validation["by_failure_mask"]:
            combinations.append({
                "failure_mask": row["failure_mask"],
                "failed_criteria": [],
                "activity": {
                    "flights": row["population_rows"],
                    "first_pass_gate_to_gate_co2_tonnes": float(
                        row["population_rows"]),
                },
            })
        audit = {
            "schema_version": 1,
            "kind": "co2gap-selection-audit",
            "release_id": "2026-09-01",
            "release_manifest_sha256": sha256_file(
                root / "release-manifest.json"),
            "source_manifest_verified": True,
            "gate": {"exact_keyset_match_to_decomposition": True},
            "coverage_statement": {"source": {
                "flights": validation["population_rows"],
                "first_pass_gate_to_gate_co2_tonnes": float(
                    validation["population_rows"]),
            }},
            "failure_combinations": combinations,
        }
        audit_path = root / "selection-audit.json"
        audit_path.write_text(json.dumps(audit, sort_keys=True))
        design = json.loads(
            (ROOT / "selection-sensitivity-design.json").read_text())
        design["input_contracts"]["selection_audit"][
            "expected_sha256"] = sha256_file(audit_path)
        design_path = root / "selection-sensitivity-design.json"
        design_path.write_text(json.dumps(design, sort_keys=True))
        return design_path, audit_path

    def test_stress_ladder_is_additive_symmetric_and_ranks_masks(self):
        with tempfile.TemporaryDirectory(
                prefix="co2gap-selection-sensitivity-") as raw:
            design_path, audit_path = self.inputs(Path(raw))
            result = selection_sensitivity(
                design_path=design_path, selection_audit_path=audit_path)
            self.assertFalse(result["claims"]["bounds_release_headline"])
            self.assertEqual(len(result["stress_ladder"]), 3)
            central = next(
                row for row in result["stress_ladder"]
                if row["id"] == "centrale")
            lower = central["profiles"]["adverse_lower"][
                "shift_from_frozen_headline_percentage_points"]
            upper = central["profiles"]["adverse_upper"][
                "shift_from_frozen_headline_percentage_points"]
            for component in (
                    "gap_total_pct", "gap_lateral_pct", "gap_vertical_pct"):
                self.assertAlmostEqual(lower[component], -upper[component])
            self.assertAlmostEqual(
                upper["gap_total_pct"],
                upper["gap_lateral_pct"] + upper["gap_vertical_pct"])
            self.assertEqual(
                result["external_validation_priorities"][0]["failure_mask"],
                "0100")

    def test_guardians_reject_overclaim_count_drift_and_schema_drift(self):
        with tempfile.TemporaryDirectory(
                prefix="co2gap-selection-sensitivity-") as raw:
            root = Path(raw)
            design_path, audit_path = self.inputs(root)
            design = json.loads(design_path.read_text())
            design["claims"]["bounds_release_headline"] = True
            design_path.write_text(json.dumps(design, sort_keys=True))
            with self.assertRaisesRegex(UncertaintyError, "cannot claim"):
                selection_sensitivity(
                    design_path=design_path, selection_audit_path=audit_path)

            design_path, audit_path = self.inputs(root)
            audit = json.loads(audit_path.read_text())
            audit["failure_combinations"][0]["activity"]["flights"] -= 1
            audit_path.write_text(json.dumps(audit, sort_keys=True))
            design = json.loads(design_path.read_text())
            design["input_contracts"]["selection_audit"][
                "expected_sha256"] = sha256_file(audit_path)
            design_path.write_text(json.dumps(design, sort_keys=True))
            with self.assertRaisesRegex(UncertaintyError, "population differs"):
                selection_sensitivity(
                    design_path=design_path, selection_audit_path=audit_path)

            design_path, audit_path = self.inputs(root)
            opensky_path = root / "opensky-day-audit-result.json"
            opensky = json.loads(opensky_path.read_text())
            opensky["by_failure_mask"][0]["proxy"]["total_gap_pct"] += 1.0
            opensky_path.write_text(json.dumps(opensky, sort_keys=True))
            design = json.loads(design_path.read_text())
            design["input_contracts"]["opensky_day_audit_result"][
                "sha256"] = sha256_file(opensky_path)
            design_path.write_text(json.dumps(design, sort_keys=True))
            with self.assertRaisesRegex(UncertaintyError, "breaks total"):
                selection_sensitivity(
                    design_path=design_path, selection_audit_path=audit_path)


class SelectionValidationTests(unittest.TestCase):
    @staticmethod
    def population():
        rows = []
        for flight_id in range(12):
            passed = flight_id < 6
            rows.append({
                "day": "2026-01-01", "flight_id": flight_id,
                "typecode": "A320" if flight_id % 2 == 0 else "B738",
                "origin_icao": "LIRF", "dest_icao": "LIMC",
                "failure_mask": "0000" if passed else "0100",
                "gate_pass": passed, "distance_band": "300_500",
                "coverage_band": "095_099" if passed else "050_085",
                "dep_ts": 1767225600 + flight_id * 3600,
                "arr_ts": 1767227400 + flight_id * 3600,
                "o_lat": 41.8, "o_lon": 12.2,
                "d_lat": 45.6, "d_lon": 8.7,
            })
        return pd.DataFrame(rows)

    def test_private_validation_sample_is_deterministic_and_expands(self):
        population = self.population()
        first, common = stratified_selection_validation_sample(
            population, per_stratum=2, top_types=1, target_sample=10, seed=19,
            namespace="test-release")
        second, _ = stratified_selection_validation_sample(
            population, per_stratum=2, top_types=1, target_sample=10, seed=19,
            namespace="test-release")
        pd.testing.assert_frame_equal(first, second)
        self.assertEqual(common, ["A320"])
        self.assertAlmostEqual(first.weight.sum(), len(population))
        self.assertEqual(set(first.failure_mask), {"0000", "0100"})
        with self.assertRaisesRegex(UncertaintyError, "at least 2"):
            stratified_selection_validation_sample(
                population, per_stratum=1, top_types=1, target_sample=10, seed=19,
                namespace="test-release")
        with self.assertRaisesRegex(UncertaintyError, "below the"):
            stratified_selection_validation_sample(
                population, per_stratum=2, top_types=1, target_sample=7, seed=19,
                namespace="test-release")

    def test_match_list_is_blinded_and_complete_outcomes_are_estimable(self):
        with tempfile.TemporaryDirectory(
                prefix="co2gap-selection-validation-") as raw:
            root = Path(raw)
            manifest_path = root / "release-manifest.json"
            manifest_path.write_text(json.dumps({
                "schema_version": 1,
                "release": {"id": "test-release", "days": ["2026-01-01"]},
            }))
            manifest = ReleaseManifest.load(manifest_path)
            population = self.population()
            sample, common = stratified_selection_validation_sample(
                population, per_stratum=2, top_types=1, target_sample=10, seed=19,
                namespace=manifest.release_id)
            sample_path = root / "sample.json"
            match_path = root / "match.json"
            sample_value, match_value = write_selection_validation_artifacts(
                manifest=manifest, population=population, sample=sample,
                common_types=common, per_stratum=2, top_types=1, seed=19,
                target_sample=10,
                output=sample_path, match_output=match_path, verified=False)
            registered = {"private_artifact_sha256": {
                "sample": sha256_file(sample_path),
                "match_list": sha256_file(match_path),
            }}
            verify_registered_selection_artifacts(
                registered, sample_path=sample_path, match_path=match_path)
            broken_registration = copy.deepcopy(registered)
            broken_registration["private_artifact_sha256"]["sample"] = "0" * 64
            with self.assertRaisesRegex(UncertaintyError, "differs"):
                verify_registered_selection_artifacts(
                    broken_registration, sample_path=sample_path,
                    match_path=match_path)
            self.assertEqual(len(sample_value["rows"]), len(match_value["rows"]))
            self.assertFalse(match_value["gate_status_disclosed"])
            for row in match_value["rows"]:
                self.assertNotIn("flight_id", row)
                self.assertNotIn("failure_mask", row)
                self.assertNotIn("coverage_band", row)

            outcomes = {
                "schema_version": 1,
                "kind": "co2gap-independent-selection-outcomes",
                "publication_status": "private_per_flight",
                "sample_sha256": sha256_file(sample_path),
                "match_list_sha256": sha256_file(match_path),
                "source": {
                    "name": "synthetic independent source",
                    "version": "test-v1",
                    "method_reference": "test protocol",
                    "quality_rule": "synthetic complete trajectory",
                    "independent_of_primary_adsb_lol": True,
                    "primary_trajectory_used": False,
                    "matching_received_gate_status": False,
                },
                "rows": [],
            }
            gate_by_id = {
                row["sample_id"]: row["gate_pass"]
                for row in sample_value["rows"]
            }
            for sample_id, passed in gate_by_id.items():
                outcomes["rows"].append({
                    "sample_id": sample_id, "status": "measured",
                    "real_co2_kg": 110.0 if passed else 130.0,
                    "ideal_co2_kg": 100.0,
                    "hybrid_co2_kg": 105.0 if passed else 110.0,
                    "match_candidate_count": 1,
                    "departure_time_delta_s": 0.0,
                    "arrival_time_delta_s": 0.0,
                    "origin_distance_km": 0.0,
                    "destination_distance_km": 0.0,
                    "proxy_coverage_fraction": 1.0,
                    "proxy_quality_pass": True,
                })
            outcomes_path = root / "outcomes.json"
            outcomes_path.write_text(json.dumps(outcomes))
            result = selection_validation_result(
                sample_path=sample_path, match_path=match_path,
                outcomes_path=outcomes_path)
            self.assertEqual(result["estimation_status"], "complete_proxy_estimate")
            effect = result["proxy_estimate"][
                "selection_effect_full_minus_gate_pass"]["gap_total_pct"]
            self.assertAlmostEqual(effect["point_percentage_points"], 10.0)
            self.assertAlmostEqual(effect["design_standard_error_percentage_points"], 0.0)

            outcomes["rows"][0]["match_candidate_count"] = 2
            outcomes_path.write_text(json.dumps(outcomes))
            with self.assertRaisesRegex(UncertaintyError, "exactly one"):
                selection_validation_result(
                    sample_path=sample_path, match_path=match_path,
                    outcomes_path=outcomes_path)
            outcomes["rows"][0]["match_candidate_count"] = 1
            outcomes["rows"][0]["proxy_quality_pass"] = False
            outcomes_path.write_text(json.dumps(outcomes))
            with self.assertRaisesRegex(UncertaintyError, "quality rule"):
                selection_validation_result(
                    sample_path=sample_path, match_path=match_path,
                    outcomes_path=outcomes_path)
            outcomes["rows"][0]["proxy_quality_pass"] = True

            outcomes["rows"][0].update({
                "status": "not_found", "reason": "not present in second source"})
            outcomes_path.write_text(json.dumps(outcomes))
            with self.assertRaisesRegex(UncertaintyError, "contains real_co2_kg"):
                selection_validation_result(
                    sample_path=sample_path, match_path=match_path,
                    outcomes_path=outcomes_path)
            outcomes["rows"][0].update({
                "real_co2_kg": None, "ideal_co2_kg": None,
                "hybrid_co2_kg": None,
            })
            outcomes_path.write_text(json.dumps(outcomes))
            blocked = selection_validation_result(
                sample_path=sample_path, match_path=match_path,
                outcomes_path=outcomes_path)
            self.assertEqual(
                blocked["estimation_status"],
                "blocked_incomplete_independent_outcomes")
            self.assertNotIn("proxy_estimate", blocked)

            tampered_match = copy.deepcopy(match_value)
            tampered_match["rows"][0]["failure_mask"] = "0000"
            match_path.write_text(json.dumps(tampered_match))
            outcomes["match_list_sha256"] = sha256_file(match_path)
            outcomes_path.write_text(json.dumps(outcomes))
            with self.assertRaisesRegex(UncertaintyError, "discloses gate"):
                selection_validation_result(
                    sample_path=sample_path, match_path=match_path,
                    outcomes_path=outcomes_path)

            match_path.write_text(json.dumps(match_value))
            outcomes["match_list_sha256"] = sha256_file(match_path)
            outcomes["source"]["independent_of_primary_adsb_lol"] = False
            outcomes_path.write_text(json.dumps(outcomes))
            with self.assertRaisesRegex(UncertaintyError, "must declare"):
                selection_validation_result(
                    sample_path=sample_path, match_path=match_path,
                    outcomes_path=outcomes_path)


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
    def test_zero_offset_is_identity_and_steps_are_exact(self):
        base = 35000.0
        self.assertEqual(_sensitivity_cruise_alt_ft(base, 0.0), base)
        self.assertEqual(_sensitivity_cruise_alt_ft(base, -1000.0), 34000.0)
        self.assertEqual(_sensitivity_cruise_alt_ft(base, 1000.0), 36000.0)

    def test_steps_do_not_silently_correct_an_infeasible_nominal_baseline(self):
        # Even a stored baseline outside a candidate model's ceiling must not
        # turn either direction into an undeclared baseline correction.
        base = 42000.0
        self.assertEqual(_sensitivity_cruise_alt_ft(base, 0.0), 42000.0)
        self.assertEqual(_sensitivity_cruise_alt_ft(base, -1000.0), 41000.0)
        self.assertEqual(_sensitivity_cruise_alt_ft(base, 1000.0), 43000.0)

    def test_sensitivity_altitude_has_a_positive_floor(self):
        self.assertEqual(_sensitivity_cruise_alt_ft(1500.0, -1000.0), 1000.0)

    def test_invalid_stored_altitude_override_fails_loudly(self):
        import decompose

        original_model = decompose.openap_model
        original_ac = decompose._get_ac
        try:
            decompose.openap_model = lambda _typecode: "a320"
            decompose._get_ac = lambda _model: {}
            for invalid in (float("nan"), float("inf"), 0.0, 999.0):
                with self.subTest(invalid=invalid):
                    with self.assertRaisesRegex(ValueError, "finite and at least 1000"):
                        decompose.decompose_flight(
                            "A320", 1.0, 300.0, 320.0,
                            np.array([40.0, 41.0]), np.array([9.0, 10.0]),
                            0, None, cruise_alt_override_ft=invalid,
                        )
        finally:
            decompose.openap_model = original_model
            decompose._get_ac = original_ac

    def test_stored_altitude_bypasses_optimizer_and_moves_both_baselines(self):
        import decompose

        for offset in (-1000.0, 0.0, 1000.0):
            with self.subTest(offset=offset):
                optimizer = Mock(side_effect=AssertionError("cache must not be read"))
                profile = Mock(return_value=object())
                with patch.multiple(
                    decompose,
                    openap_model=Mock(return_value="a320"),
                    _get_ac=Mock(return_value={}),
                    optimal_cruise_alt_ft=optimizer,
                    _cruise_tas_kt=Mock(return_value=440.0),
                    mean_along_track_wind_ms=Mock(return_value=0.0),
                    mean_wind_along_track=Mock(return_value=0.0),
                    _build_profile=profile,
                    estimate_fuel=Mock(return_value=SimpleNamespace(ok=True, co2_kg=100.0)),
                    _cruise_state=Mock(return_value=(None, None)),
                    enroute_dist_ratio=Mock(return_value=(1.0, 200.0)),
                ):
                    result = decompose.decompose_flight(
                        "A320", 120.0, 300.0, 320.0,
                        np.array([40.0, 41.0]), np.array([9.0, 10.0]), 0, None,
                        cruise_alt_override_ft=22000.0, cruise_alt_offset_ft=offset,
                    )
                optimizer.assert_not_called()
                self.assertEqual(result["cruise_alt_ft"], 22000.0 + offset)
                self.assertEqual(profile.call_count, 2)
                self.assertEqual(
                    [call.args[2] for call in profile.call_args_list],
                    [22000.0 + offset, 22000.0 + offset])

    def test_nominal_closure_accepts_roundoff_but_rejects_drift_and_nonfinite(self):
        _require_nominal_baseline(100.0, 100.0, "exact")
        _require_nominal_baseline(100.0, 100.0 + 1e-8, "roundoff")
        for stored, recomputed in ((100.0, 100.001), (100.0, float("nan")),
                                    (float("inf"), 100.0), (0.0, 0.0)):
            with self.subTest(stored=stored, recomputed=recomputed):
                with self.assertRaisesRegex(UncertaintyError, "nominal baseline mismatch"):
                    _require_nominal_baseline(stored, recomputed, "regression")

    def test_runner_uses_stored_altitude_and_refuses_a_changed_reference(self):
        import decompose
        import emissions
        import uncertainty
        import wind.era5

        scenarios = ROOT / "uncertainty-scenarios.json"
        config = json.loads(scenarios.read_text())
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            manifest_path = base / "manifest.json"
            manifest_path.write_text("{}")
            sample_path = base / "sample.json"
            sample_path.write_text(json.dumps({
                "schema_version": 1, "kind": "co2gap-uncertainty-sample",
                "release_id": "test", "release_manifest_sha256": sha256_file(manifest_path),
                "rows": [{"day": "2026-01-01", "flight_id": 1, "weight": 1.0}],
            }))
            manifest = SimpleNamespace(
                release_id="test", path=manifest_path, days=["2026-01-01"],
                data={"configuration": {"era5": {
                    "pressure_levels_hpa": [250], "area_nwse": [72, -32, 27, 45],
                    "grid_degrees": [0.25, 0.25], "variables": ["u", "v"],
                }}},
            )
            tables = {
                "decomposition": pd.DataFrame([{
                    "flight_id": 1, "typecode": "A320", "gc_km": 300.0,
                    "flown_km": 320.0, "dep_ts": 0, "co2_kg_v0": 120.0,
                    "ideal_gc_co2_kg": 100.0, "hybrid_co2_kg": 110.0,
                    "cruise_alt_ft": 22000.0,
                }]),
                "ground": pd.DataFrame([{
                    "flight_id": 1, "fuel_recomputed_kg": 100.0,
                    **{f"fuel_{name}_kg": 0.0 for name in uncertainty.GROUND_DEFINITIONS},
                }]),
                "points": pd.DataFrame([{
                    "flight_id": 1, "t": t, "lat": 40.0, "lon": 9.0,
                    "alt_ft": 20000.0, "gs_kt": 400.0, "ias_kt": 250.0,
                    "vs_fpm": 0.0,
                } for t in (0, 60)]),
            }

            def read_table(path, **kwargs):
                table = (tables["points"] if path.name == "points.parquet"
                         else tables[path.parent.name])
                return SimpleNamespace(to_pandas=lambda: table.copy())

            for drift in (0.0, 0.01):
                with self.subTest(drift=drift):
                    decomposition = Mock(return_value={
                        "ideal_gc_co2_kg": 100.0 + drift, "hybrid_co2_kg": 110.0,
                    })
                    with patch.object(uncertainty.ReleaseManifest, "load", return_value=manifest), \
                         patch.object(uncertainty.pq, "read_table", side_effect=read_table), \
                         patch.object(uncertainty, "_load_calibration", return_value={}), \
                         patch.object(uncertainty, "_flight_from_points", return_value=object()), \
                         patch.object(wind.era5, "WindField", return_value=object()), \
                         patch.object(emissions, "estimate_fuel", return_value=SimpleNamespace(ok=True, co2_kg=120.0)), \
                         patch.object(decompose, "decompose_flight", decomposition):
                        kwargs = dict(
                            sample_path=sample_path, scenarios_path=scenarios,
                            manifest_path=manifest_path, flights_dir=base / "flights",
                            decomposition_dir=base / "decomposition", ground_dir=base / "ground",
                            era5_dir=base / "era5", calibration=base / "calibration.json",
                            verify=False,
                        )
                        if drift:
                            with self.assertRaisesRegex(UncertaintyError, "nominal baseline mismatch"):
                                paired_sensitivity(**kwargs)
                        else:
                            result = paired_sensitivity(**kwargs)
                            self.assertTrue(result["population_expansion_complete"])
                            self.assertEqual(result["nominal_baseline_reconstruction"]["max_abs_ideal_kg"], 0.0)
                    self.assertEqual(decomposition.call_count, len(config["scenarios"]))
                    self.assertTrue(all(call.kwargs["cruise_alt_override_ft"] == 22000.0
                                        for call in decomposition.call_args_list))


class CorrectedWindReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.manifest_path = self.base / "manifest.json"
        self.manifest_path.write_text("{}")
        self.sample_path = self.base / "sample.json"
        self.sample_path.write_text(json.dumps({
            "schema_version": 1, "kind": "co2gap-uncertainty-sample",
            "release_id": "test", "release_manifest_sha256": sha256_file(self.manifest_path),
            "rows": [{"day": "2026-01-01", "flight_id": fid, "weight": 3.0}
                     for fid in (1, 2)],
        }))
        self.config = json.loads((ROOT / "uncertainty-scenarios.json").read_text())
        self.scenarios_path = self.base / "scenarios.json"
        self.scenarios_path.write_text(json.dumps(self.config))
        self.manifest = SimpleNamespace(
            release_id="test", path=self.manifest_path, days=["2026-01-01"],
            data={"configuration": {
                "ground": {"definition": "a3000t70"},
                "era5": {"pressure_levels_hpa": [250], "area_nwse": [72, -32, 27, 45],
                         "grid_degrees": [0.25, 0.25], "variables": ["u", "v"]},
            }},
            verify_set=Mock(), verify_file=Mock(),
        )
        import uncertainty
        self.tables = {
            "decomposition": pd.DataFrame([{
                "flight_id": fid, "typecode": "A320", "gc_km": 300.0,
                "flown_km": 330.0, "dep_ts": 0, "co2_kg_v0": 120.0,
                "ideal_gc_co2_kg": 100.0, "hybrid_co2_kg": 110.0,
                "cruise_alt_ft": 22000.0,
                "mean_wpar_gc_ms": 0.0, "mean_wpar_track_ms": 0.0,
            } for fid in (1, 2)]),
            "ground": pd.DataFrame([{
                "flight_id": fid, "fuel_recomputed_kg": 100.0,
                **{f"fuel_{name}_kg": 0.0 for name in uncertainty.GROUND_DEFINITIONS},
            } for fid in (1, 2)]),
            "points": pd.DataFrame([{
                "flight_id": fid, "t": t, "lat": 40.0, "lon": 9.0,
                "alt_ft": 20000.0, "gs_kt": 400.0, "ias_kt": 250.0, "vs_fpm": 0.0,
            } for fid in (1, 2) for t in (0, 60)]),
        }

    def run_fixture(self, *, profile="corrected-wind", wind_change=2.0,
                    unexplained_drift=0.0, missing_wind=False, limit=None,
                    fail_scenario=False, verify=False, bad_replay=False,
                    missing_profile=False, sampling_precision=False,
                    precision_audit_out=None):
        import decompose
        import emissions
        import excess_wind
        import uncertainty
        import wind.era5

        def read_table(path, **kwargs):
            if path.name == "points.parquet":
                self.assertEqual(kwargs["filters"], [("flight_id", "in", [1] if limit == 1 else [1, 2])])
                table = self.tables["points"]
            else:
                table = self.tables[path.parent.name]
            return SimpleNamespace(to_pandas=lambda: table.copy())

        def estimate(flight, **kwargs):
            value = flight.distance / 3.0 + flight.wind if hasattr(flight, "wind") else 120.0
            return SimpleNamespace(ok=not bad_replay or not hasattr(flight, "wind"), co2_kg=value)

        def decompose_stub(*args, **kwargs):
            if (fail_scenario and kwargs["load_factor"] == 0.72
                    and (fail_scenario is True or args[1] > 120.0)):
                return None
            result = {
                "ideal_gc_co2_kg": 100.0 + wind_change + unexplained_drift,
                "hybrid_co2_kg": 110.0 + 10.0 * (kwargs["load_factor"] - 0.82),
                "mean_wpar_gc_ms": wind_change, "mean_wpar_track_ms": 0.0,
            }
            if missing_wind:
                result.pop("mean_wpar_gc_ms")
            return result

        with patch.object(uncertainty.ReleaseManifest, "load", return_value=self.manifest), \
             patch.object(uncertainty.pq, "read_table", side_effect=read_table), \
             patch.object(uncertainty, "_load_calibration", return_value={"A320": 1.5}), \
             patch.object(uncertainty, "_flight_from_points", return_value=object()), \
             patch.object(wind.era5, "WindField", return_value=object()), \
             patch.object(emissions, "estimate_fuel", side_effect=estimate), \
             patch.object(excess_wind, "_build_profile", side_effect=lambda tc, dist, alt, w: None if missing_profile else SimpleNamespace(distance=dist, wind=w)), \
             patch.object(decompose, "decompose_flight", side_effect=decompose_stub):
            return paired_sensitivity(
                sample_path=self.sample_path, scenarios_path=self.scenarios_path,
                manifest_path=self.manifest_path, flights_dir=self.base / "flights",
                decomposition_dir=self.base / "decomposition", ground_dir=self.base / "ground",
                era5_dir=self.base / "era5", calibration=self.base / "calibration.json",
                verify=verify, limit=limit, reference_profile=profile,
                sampling_precision=sampling_precision,
                precision_audit_out=precision_audit_out)

    def prepare_precision_sample(self):
        sample = json.loads(self.sample_path.read_text())
        sample.update(sample_rows=2, population_rows=6, strata=1,
                      source_manifest_verified=True, seed=1, per_stratum=2)
        for row in sample["rows"]:
            row.update(stratum="A320|300_500|090_099", sample_n=2, population_n=6)
        self.sample_path.write_text(json.dumps(sample))

    def test_precision_is_optional_preserves_metrics_and_writes_private_audit(self):
        import uncertainty
        self.prepare_precision_sample()
        for profile in ("frozen-release", "corrected-wind"):
            with self.subTest(profile=profile):
                plain = self.run_fixture(profile=profile, wind_change=0.0, verify=True)
                audit = self.base / f"{profile}-moments.json"
                with patch.object(uncertainty, "_verified_precision_sample") as regeneration:
                    result = self.run_fixture(profile=profile, wind_change=0.0, verify=True,
                                              sampling_precision=True, precision_audit_out=audit)
                regeneration.assert_called_once()
                precision = result.pop("sampling_precision")
                result["implementation_sha256"].pop("lab/sampling_precision.py")
                self.assertEqual(result, plain)
                self.assertEqual(precision["private_moments_sha256"], sha256_file(audit))
                self.assertTrue(precision["draw_regenerated_from_verified_frame"])
                moments = json.loads(audit.read_text())
                self.assertEqual(moments["provenance"]["sample_sha256"], sha256_file(self.sample_path))
                self.assertNotIn("flight_id", audit.read_text())
                original = audit.read_bytes()
                with self.assertRaisesRegex(UncertaintyError, "already exists"):
                    self.run_fixture(sampling_precision=True, verify=True, precision_audit_out=audit)
                self.assertEqual(audit.read_bytes(), original)

    def test_precision_refuses_partial_unverified_or_unpaired_runs(self):
        import uncertainty
        from sampling_precision import SamplingPrecisionError
        self.prepare_precision_sample()
        for kwargs in ({"verify": False}, {"verify": True, "limit": 1},
                       {"verify": True, "limit": 2}):
            with self.assertRaisesRegex(UncertaintyError, "verified full inputs and no limit"):
                self.run_fixture(sampling_precision=True, **kwargs)
        with self.assertRaisesRegex(UncertaintyError, "requires sampling-precision"):
            self.run_fixture(precision_audit_out=self.base / "not-written.json")
        with self.assertRaisesRegex(UncertaintyError, "outside the repository"):
            self.run_fixture(sampling_precision=True, verify=True,
                             precision_audit_out=ROOT / "do-not-write.json")
        self.tables["decomposition"].loc[1, "co2_kg_v0"] = 121
        with patch.object(uncertainty, "_verified_precision_sample"):
            with self.assertRaisesRegex(SamplingPrecisionError, "incomplete paired population"):
                self.run_fixture(verify=True, sampling_precision=True, fail_scenario="one",
                                 precision_audit_out=self.base / "not-written.json")
        self.assertFalse((self.base / "not-written.json").exists())

    def test_precision_regenerates_the_declared_draw_from_the_frame(self):
        import uncertainty
        population = SamplingTests.population()
        for target in (None, 10):
            draw = stratified_sample(population, 2, 17, target_sample=target)
            sample = write_sample_manifest(
                manifest=self.manifest, population=population, sample=draw,
                per_stratum=2, seed=17, output=self.base / "generated.json",
                verified=True, target_sample=target)
            with patch.object(uncertainty, "build_population", return_value=population):
                uncertainty._verified_precision_sample(sample, self.manifest, self.base, self.base)
                changed = copy.deepcopy(sample)
                changed["rows"][0]["weight"] += 1
                with self.assertRaisesRegex(UncertaintyError, "differs from regenerated"):
                    uncertainty._verified_precision_sample(changed, self.manifest, self.base, self.base)
                changed = copy.deepcopy(sample)
                changed["source_manifest_verified"] = False
                with self.assertRaisesRegex(UncertaintyError, "verified sample manifest"):
                    uncertainty._verified_precision_sample(changed, self.manifest, self.base, self.base)
                changed = copy.deepcopy(sample)
                changed["seed"] = True
                with self.assertRaisesRegex(UncertaintyError, "integer seed"):
                    uncertainty._verified_precision_sample(changed, self.manifest, self.base, self.base)
                if target is not None:
                    changed = copy.deepcopy(sample)
                    changed["sampling_design"]["within_stratum"] = "unknown design"
                    with self.assertRaisesRegex(UncertaintyError, "declared SRS design"):
                        uncertainty._verified_precision_sample(changed, self.manifest, self.base, self.base)

    def test_profiles_separate_wind_change_from_scenario_effect(self):
        for profile in ("frozen-release", "corrected-wind"):
            for wind_change in (0.0, 2.0):
                with self.subTest(profile=profile, wind_change=wind_change):
                    if profile == "frozen-release" and wind_change:
                        with self.assertRaisesRegex(UncertaintyError, "nominal baseline mismatch"):
                            self.run_fixture(profile=profile, wind_change=wind_change)
                        continue
                    result = self.run_fixture(profile=profile, wind_change=wind_change)
                    self.assertEqual(result["schema_version"], 2)
                    self.assertEqual(result["reference_profile"], profile)
                    self.assertTrue(result["population_expansion_complete"])
                    frozen = result["frozen_same_sample_reference"]
                    nominal = result["scenarios"]["nominal"]
                    self.assertEqual(frozen["expanded_population_weight"], 6.0)
                    self.assertEqual(nominal["expanded_population_weight"], 6.0)
                    self.assertEqual(frozen["gap_total_pct"], 20.0)
                    self.assertAlmostEqual(result["nominal_minus_frozen_same_sample"]["gap_total_pct"],
                                           (120.0 / (100.0 + wind_change) - 1) * 100 - 20)
                    self.assertEqual(nominal["delta_from_nominal"]["gap_total_pct"], 0.0)
                    self.assertAlmostEqual(result["scenarios"]["load_minus_0.10"]["delta_from_nominal"]["gap_lateral_pct"],
                                           -100.0 / (100.0 + wind_change))
                    for values in result["scenarios"].values():
                        self.assertAlmostEqual(values["additivity_residual_pct"], 0.0)
                    if profile == "corrected-wind":
                        self.assertEqual(result["stored_wind_replay"]["checked_flights"], 2)
                        self.assertEqual(result["stored_wind_replay"]["max_abs_ideal_kg"], 0.0)
                    else:
                        self.assertIsNone(result["stored_wind_replay"])

    def test_corrected_profile_rejects_unexplained_downstream_drift(self):
        with self.assertRaisesRegex(UncertaintyError, "corrected-wind-only/ideal"):
            self.run_fixture(unexplained_drift=0.01)

    def test_corrected_profile_rejects_changed_frozen_fuel(self):
        self.tables["decomposition"].loc[0, "hybrid_co2_kg"] += 0.01
        with self.assertRaisesRegex(UncertaintyError, "stored-wind-replay/hybrid"):
            self.run_fixture()

    def test_corrected_profile_requires_finite_stored_and_current_winds(self):
        with self.assertRaisesRegex(UncertaintyError, "corrected/mean_wpar_gc_ms"):
            self.run_fixture(missing_wind=True)
        for value in (None, float("nan"), float("inf")):
            with self.subTest(value=value):
                self.tables["decomposition"].loc[0, "mean_wpar_gc_ms"] = value
                with self.assertRaisesRegex(UncertaintyError, "stored/mean_wpar_gc_ms"):
                    self.run_fixture()

    def test_corrected_profile_rejects_failed_replay(self):
        with self.assertRaisesRegex(UncertaintyError, "stored-wind replay fuel failed"):
            self.run_fixture(bad_replay=True)
        with self.assertRaisesRegex(UncertaintyError, "stored-wind replay cannot build profile"):
            self.run_fixture(missing_profile=True)

    def test_nonfinite_scenario_cannot_enter_an_aggregate(self):
        with self.assertRaisesRegex(UncertaintyError, "no valid denominator"):
            self.run_fixture(unexplained_drift=float("nan"))

    def test_corrected_profile_cannot_change_other_nominal_conventions(self):
        for key, value in (("load_factor", 0.72), ("reserve_kg", 1000.0),
                           ("real_tas_mode", "gs"), ("cruise_alt_offset_ft", 1000.0),
                           ("ground_definition", "a1000t70")):
            with self.subTest(key=key):
                config = copy.deepcopy(self.config)
                config["scenarios"][0][key] = value
                self.scenarios_path.write_text(json.dumps(config))
                with self.assertRaisesRegex(UncertaintyError, "corrected-wind nominal requires"):
                    self.run_fixture()
        self.scenarios_path.write_text(json.dumps(self.config))
        self.manifest.data["configuration"]["ground"]["definition"] = "a1000t70"
        with self.assertRaisesRegex(UncertaintyError, "ground definition differs"):
            self.run_fixture()

    def test_corrected_profile_preserves_smoke_and_failure_population_flags(self):
        result = self.run_fixture(limit=1)
        self.assertTrue(result["sample_truncated_for_smoke_test"])
        self.assertFalse(result["population_expansion_complete"])
        self.assertEqual(result["stored_wind_replay"]["checked_flights"], 1)
        self.tables["ground"] = self.tables["ground"].iloc[:1]
        result = self.run_fixture()
        self.assertFalse(result["population_expansion_complete"])
        self.assertEqual(result["common_population_failures"], {"missing_input_key": 1})
        for entry in [result["frozen_same_sample_reference"], *result["scenarios"].values()]:
            self.assertEqual(entry["sample_flights"], 1)
            self.assertEqual(entry["failed_flights"], 1)
        with self.assertRaisesRegex(UncertaintyError, "no valid denominator"):
            self.run_fixture(fail_scenario=True)

    def test_corrected_profile_keeps_input_verification(self):
        result = self.run_fixture(verify=True)
        self.assertTrue(result["source_manifest_verified"])
        self.assertEqual(self.manifest.verify_set.call_count, 4)
        self.manifest.verify_file.assert_called_once()
        self.manifest.verify_set.side_effect = UncertaintyError("changed source")
        with self.assertRaisesRegex(UncertaintyError, "changed source"):
            self.run_fixture(verify=True)
        with self.assertRaisesRegex(UncertaintyError, "unknown sensitivity reference profile"):
            self.run_fixture(profile="automatic-fallback")

    def test_allocation_metadata_survives_both_profiles_without_changing_metrics(self):
        sample = json.loads(self.sample_path.read_text())
        design = {
            "allocation": "minimum-plus-proportional-remaining-capacity",
            "minimum_per_stratum": 2,
            "target_sample_rows": 2,
            "rounding": "integer largest remainders, lexicographic stratum ties",
            "within_stratum": "simple random sampling without replacement",
            "uses_sensitivity_outcomes": False,
        }
        for profile in ("frozen-release", "corrected-wind"):
            with self.subTest(profile=profile):
                sample.pop("sampling_design", None)
                sample["per_stratum"] = 2
                self.sample_path.write_text(json.dumps(sample))
                equal = self.run_fixture(profile=profile, wind_change=0.0)
                self.assertEqual(equal["sample_design"]["allocation"], {
                    "allocation": "historical-equal-cap", "per_stratum": 2,
                })
                sample.pop("per_stratum")
                sample["sampling_design"] = design
                self.sample_path.write_text(json.dumps(sample))
                balanced = self.run_fixture(profile=profile, wind_change=0.0)
                self.assertEqual(balanced["sample_design"]["allocation"], design)
                self.assertEqual(balanced["scenarios"], equal["scenarios"])
                self.assertEqual(balanced["sample_design"]["weight_sum"], 6.0)

    def test_one_scenario_failure_removes_the_same_flight_from_both_references(self):
        self.tables["decomposition"].loc[1, "co2_kg_v0"] = 121.0
        result = self.run_fixture(fail_scenario="one")
        self.assertEqual(result["common_population_failures"], {"load_minus_0.10:decomposition": 1})
        self.assertFalse(result["population_expansion_complete"])
        self.assertEqual(result["stored_wind_replay"]["checked_flights"], 1)
        for entry in [result["frozen_same_sample_reference"], *result["scenarios"].values()]:
            self.assertEqual(entry["sample_flights"], 1)
            self.assertEqual(entry["failed_flights"], 1)
            self.assertEqual(entry["expanded_population_weight"], 3.0)


if __name__ == "__main__":
    unittest.main()
