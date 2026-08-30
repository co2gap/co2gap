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
from release_manifest import ReleaseManifest, sha256_file  # noqa: E402
from uncertainty import (UncertaintyError, _metrics, _require_nested_counts,  # noqa: E402
                         block_resample_days,
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
