from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "pipeline"), str(ROOT / "ingest"),
                str(ROOT / "lab"), str(ROOT)]

from release_manifest import sha256_file  # noqa: E402
from uncertainty import (  # noqa: E402
    UncertaintyError,
    cli as uncertainty_cli,
    targeted_validation_result,
    verify_registered_targeted_artifacts,
    write_targeted_validation_artifacts,
)


class TargetedValidationTests(unittest.TestCase):
    @staticmethod
    def inputs(root: Path) -> tuple[Path, Path, Path]:
        manifest_path = root / "release-manifest.json"
        manifest_path.write_text(json.dumps({
            "schema_version": 1,
            "release": {"id": "test-release", "days": ["2026-01-01"]},
        }, sort_keys=True))
        rows = []
        match_rows = []
        definitions = (
            ("0000", True, "pass-a", 4, 2, 2),
            ("0000", True, "pass-b", 4, 2, 2),
            ("0100", False, "coverage", 4, 3, 3),
            ("1000", False, "endpoint", 4, 3, 3),
            ("1100", False, "both", 4, 2, 2),
        )
        flight_id = 0
        for mask, passed, stratum, population_n, sample_n, count in definitions:
            for _ in range(count):
                sample_id = f"{flight_id + 1:024x}"
                rows.append({
                    "sample_id": sample_id,
                    "day": "2026-01-01",
                    "flight_id": flight_id,
                    "failure_mask": mask,
                    "gate_pass": passed,
                    "stratum": stratum,
                    "distance_band": "300_500",
                    "coverage_band": "ge099" if passed else "lt090",
                    "type_group": "A320",
                    "population_n": population_n,
                    "sample_n": sample_n,
                    "weight": population_n / sample_n,
                })
                match_rows.append({
                    "sample_id": sample_id,
                    "day": "2026-01-01",
                    "typecode": "A320",
                    "dep_ts": 1767225600 + flight_id * 3600,
                    "arr_ts": 1767227400 + flight_id * 3600,
                    "o_lat": 41.8,
                    "o_lon": 12.2,
                    "d_lat": 45.6,
                    "d_lon": 8.7,
                })
                flight_id += 1
        sample_path = root / "parent-sample.json"
        sample_path.write_text(json.dumps({
            "schema_version": 1,
            "kind": "co2gap-selection-validation-sample",
            "publication_status": "private_per_flight",
            "analysis_status": "awaiting_independent_outcomes",
            "release_id": "test-release",
            "sample_rows": len(rows),
            "rows": rows,
        }, sort_keys=True))
        match_path = root / "parent-match.json"
        match_path.write_text(json.dumps({
            "schema_version": 1,
            "kind": "co2gap-selection-validation-match-list",
            "publication_status": "private_per_flight",
            "release_id": "test-release",
            "sample_sha256": sha256_file(sample_path),
            "gate_status_disclosed": False,
            "primary_track_quality_disclosed": False,
            "rows": match_rows,
        }, sort_keys=True))
        parent_design = {
            "schema_version": 1,
            "kind": "co2gap-selection-validation-design",
            "publication_status": "aggregate_preregistration",
            "analysis_status": "awaiting_independent_outcomes",
            "release_id": "test-release",
            "release_manifest_sha256": sha256_file(manifest_path),
            "population_boundary": "synthetic test",
            "population_rows": 20,
            "sample_rows": 12,
            "strata": 5,
            "maximum_weight": 2.0,
            "kish_effective_sample_size": 400.0 / (104.0 / 3.0),
            "parameters": {
                "seed": 1,
                "minimum_per_stratum": 2,
                "target_sample_rows": 12,
                "top_types": 1,
                "common_types": ["A320"],
            },
            "by_failure_mask": [
                {"failure_mask": "0000", "gate_pass": True,
                 "population_rows": 8, "sample_rows": 4},
                {"failure_mask": "0100", "gate_pass": False,
                 "population_rows": 4, "sample_rows": 3},
                {"failure_mask": "1000", "gate_pass": False,
                 "population_rows": 4, "sample_rows": 3},
                {"failure_mask": "1100", "gate_pass": False,
                 "population_rows": 4, "sample_rows": 2},
            ],
            "private_artifact_sha256": {
                "sample": sha256_file(sample_path),
                "match_list": sha256_file(match_path),
            },
            "outcome_contract": "lab/selection-validation-outcomes.schema.json",
            "outcome_contract_sha256": sha256_file(
                ROOT / "lab/selection-validation-outcomes.schema.json"),
            "primary_headline_bias_bounded": False,
            "privacy": "synthetic aggregate",
        }
        parent_design_path = root / "parent-design.json"
        parent_design_path.write_text(json.dumps(parent_design, sort_keys=True))
        design = json.loads((ROOT / "targeted-validation-design.json").read_text())
        design["release_id"] = "test-release"
        design["parent_contract"].update({
            "design_path": "parent-design.json",
            "design_sha256": sha256_file(parent_design_path),
            "private_sample_sha256": sha256_file(sample_path),
            "blinded_match_list_sha256": sha256_file(match_path),
            "canonical_sample_rows": 12,
        })
        targets = {"0000": 3, "0100": 3, "1000": 3, "1100": 2}
        for row in design["target_allocation"]:
            row["target_rows"] = targets[row["failure_mask"]]
        design["target_rows"] = 11
        design["minimum_measured_rows"] = {
            "0000": 2, "0100": 2, "1000": 2, "1100": 1,
        }
        design["outcome_contract"] = {
            "path": str(ROOT / "lab/targeted-validation-outcomes.schema.json"),
            "sha256": sha256_file(
                ROOT / "lab/targeted-validation-outcomes.schema.json"),
        }
        design_path = root / "targeted-design.json"
        design_path.write_text(json.dumps(design, sort_keys=True))
        return design_path, sample_path, match_path

    @staticmethod
    def outcomes(design_path: Path, sample_path: Path,
                 match_path: Path, *, incomplete: bool = False) -> dict:
        sample = json.loads(sample_path.read_text())
        by_mask = {
            row["sample_id"]: row["failure_mask"] for row in sample["rows"]}
        proxy = {
            "0000": (110.0, 100.0, 105.0),
            "0100": (120.0, 100.0, 110.0),
            "1000": (105.0, 100.0, 102.0),
            "1100": (130.0, 100.0, 120.0),
        }
        rows = []
        missing_coverage = 0
        for sample_id, mask in by_mask.items():
            if incomplete and mask == "0100" and missing_coverage < 2:
                rows.append({
                    "sample_id": sample_id,
                    "status": "not_found",
                    "reason": "synthetic missing source row",
                })
                missing_coverage += 1
                continue
            real, ideal, hybrid = proxy[mask]
            rows.append({
                "sample_id": sample_id,
                "status": "measured",
                "real_co2_kg": real,
                "ideal_co2_kg": ideal,
                "hybrid_co2_kg": hybrid,
                "match_candidate_count": 1,
                "match_mutual_nearest": True,
                "primary_runner_up_score": None,
                "source_runner_up_score": None,
                "departure_time_delta_s": 0.0,
                "arrival_time_delta_s": 0.0,
                "origin_distance_km": 0.0,
                "destination_distance_km": 0.0,
                "proxy_coverage_fraction": 1.0,
                "source_unique_points": 20,
                "source_flown_distance_km": 300.0,
                "source_gc_distance_km": 300.0,
                "source_max_segment_speed_kt": 500.0,
                "proxy_quality_pass": True,
            })
        return {
            "schema_version": 1,
            "kind": "co2gap-targeted-heldout-outcomes",
            "publication_status": "private_per_flight",
            "targeted_design_sha256": sha256_file(design_path),
            "sample_sha256": sha256_file(sample_path),
            "match_list_sha256": sha256_file(match_path),
            "source": {
                "name": "synthetic held-out source",
                "version": "test-v1",
                "method_reference": "synthetic test protocol",
                "quality_rule": "synthetic complete trajectory",
                "licence_or_permission_reference": "synthetic test permission",
                "receiver_provenance_statement": "overlap unverified for test",
                "matching_profile": "targeted_mutual_nearest_v1",
                "source_quality_profile": "targeted_source_quality_v1",
                "processing_profile": "ground_speed_proxy_v1",
                "source_relation": "separate_source_receiver_overlap_unverified",
                "primary_trajectory_used": False,
                "matching_received_gate_status": False,
                "matching_received_primary_track_quality": False,
                "protocol_frozen_before_source_access": True,
            },
            "rows": rows,
        }

    def test_targeted_tranche_is_deterministic_blinded_and_weighted(self):
        with tempfile.TemporaryDirectory(
                prefix="co2gap-targeted-validation-") as raw:
            root = Path(raw)
            design_path, parent_sample, parent_match = self.inputs(root)
            outputs = []
            for suffix in ("a", "b"):
                sample_path = root / f"sample-{suffix}.json"
                match_path = root / f"match-{suffix}.json"
                registration_path = root / f"registration-{suffix}.json"
                values = write_targeted_validation_artifacts(
                    design_path=design_path,
                    parent_sample_path=parent_sample,
                    parent_match_path=parent_match,
                    output=sample_path, match_output=match_path,
                    registration_output=registration_path)
                outputs.append((sample_path, match_path, registration_path, values))
            self.assertEqual(outputs[0][0].read_bytes(), outputs[1][0].read_bytes())
            self.assertEqual(outputs[0][1].read_bytes(), outputs[1][1].read_bytes())
            self.assertEqual(outputs[0][2].read_bytes(), outputs[1][2].read_bytes())
            sample_value, match_value, registration = outputs[0][3]
            self.assertEqual(sample_value["sample_rows"], 11)
            self.assertEqual(
                set(match_value["rows"][0]),
                {"sample_id", "day", "typecode", "dep_ts", "arr_ts",
                 "o_lat", "o_lon", "d_lat", "d_lon"})
            self.assertNotIn("failure_mask", match_value["rows"][0])
            self.assertEqual(
                registration["by_failure_mask"][0]["targeted_weight_sum"], 8.0)
            verify_registered_targeted_artifacts(
                registration, sample_path=outputs[0][0], match_path=outputs[0][1])
            with self.assertRaisesRegex(UncertaintyError, "cannot overwrite"):
                write_targeted_validation_artifacts(
                    design_path=design_path,
                    parent_sample_path=parent_sample,
                    parent_match_path=parent_match,
                    output=parent_sample,
                    match_output=root / "unsafe-match.json",
                    registration_output=root / "unsafe-registration.json")

    def test_complete_targeted_result_and_blocking_guardians(self):
        with tempfile.TemporaryDirectory(
                prefix="co2gap-targeted-validation-") as raw:
            root = Path(raw)
            design_path, parent_sample, parent_match = self.inputs(root)
            sample_path = root / "sample.json"
            match_path = root / "match.json"
            registration_path = root / "registration.json"
            write_targeted_validation_artifacts(
                design_path=design_path,
                parent_sample_path=parent_sample,
                parent_match_path=parent_match,
                output=sample_path, match_output=match_path,
                registration_output=registration_path)
            outcomes_path = root / "outcomes.json"
            complete = self.outcomes(design_path, sample_path, match_path)
            outcomes_path.write_text(json.dumps(complete, sort_keys=True))
            result = targeted_validation_result(
                design_path=design_path, registration_path=registration_path,
                sample_path=sample_path, match_path=match_path,
                outcomes_path=outcomes_path)
            self.assertEqual(
                result["analysis_status"], "complete_targeted_diagnostic")
            contrasts = {
                row["failure_mask"]: row for row in result["conditional_contrasts"]}
            self.assertAlmostEqual(
                contrasts["0100"][
                    "weighted_conditional_gap_pct_minus_0000_percentage_points"]
                ["gap_total_pct"], 10.0)
            self.assertFalse(result["primary_headline_bias_bounded"])

            incomplete = self.outcomes(
                design_path, sample_path, match_path, incomplete=True)
            outcomes_path.write_text(json.dumps(incomplete, sort_keys=True))
            blocked = targeted_validation_result(
                design_path=design_path, registration_path=registration_path,
                sample_path=sample_path, match_path=match_path,
                outcomes_path=outcomes_path)
            self.assertEqual(
                blocked["analysis_status"], "blocked_insufficient_mask_support")
            self.assertNotIn("conditional_contrasts", blocked)

            broken = self.outcomes(design_path, sample_path, match_path)
            broken["source"]["matching_received_gate_status"] = True
            outcomes_path.write_text(json.dumps(broken, sort_keys=True))
            with self.assertRaisesRegex(UncertaintyError, "gate_status"):
                targeted_validation_result(
                    design_path=design_path,
                    registration_path=registration_path,
                    sample_path=sample_path, match_path=match_path,
                    outcomes_path=outcomes_path)

            broken = self.outcomes(design_path, sample_path, match_path)
            broken["rows"][0].update({
                "departure_time_delta_s": 2700.0,
                "arrival_time_delta_s": 2700.0,
                "origin_distance_km": 100.0,
                "destination_distance_km": 100.0,
            })
            outcomes_path.write_text(json.dumps(broken, sort_keys=True))
            with self.assertRaisesRegex(UncertaintyError, "matching score"):
                targeted_validation_result(
                    design_path=design_path,
                    registration_path=registration_path,
                    sample_path=sample_path, match_path=match_path,
                    outcomes_path=outcomes_path)

            broken = self.outcomes(design_path, sample_path, match_path)
            broken["rows"][0]["proxy_coverage_fraction"] = 0.84
            outcomes_path.write_text(json.dumps(broken, sort_keys=True))
            with self.assertRaisesRegex(UncertaintyError, "source-quality"):
                targeted_validation_result(
                    design_path=design_path,
                    registration_path=registration_path,
                    sample_path=sample_path, match_path=match_path,
                    outcomes_path=outcomes_path)

            non_singleton = self.outcomes(design_path, sample_path, match_path)
            non_singleton["rows"][0].update({
                "match_candidate_count": 2,
                "departure_time_delta_s": 450.0,
                "primary_runner_up_score": 1.0,
                "source_runner_up_score": 1.0,
            })
            outcomes_path.write_text(json.dumps(non_singleton, sort_keys=True))
            accepted = targeted_validation_result(
                design_path=design_path, registration_path=registration_path,
                sample_path=sample_path, match_path=match_path,
                outcomes_path=outcomes_path)
            self.assertEqual(
                accepted["analysis_status"], "complete_targeted_diagnostic")

            non_singleton["rows"][0]["primary_runner_up_score"] = 0.6
            outcomes_path.write_text(json.dumps(non_singleton, sort_keys=True))
            with self.assertRaisesRegex(UncertaintyError, "runner-up"):
                targeted_validation_result(
                    design_path=design_path,
                    registration_path=registration_path,
                    sample_path=sample_path, match_path=match_path,
                    outcomes_path=outcomes_path)

    def test_cli_renders_contract_failure_without_traceback(self):
        with tempfile.TemporaryDirectory(
                prefix="co2gap-targeted-validation-") as raw:
            root = Path(raw)
            design_path, parent_sample, parent_match = self.inputs(root)
            sample_path = root / "sample.json"
            match_path = root / "match.json"
            registration_path = root / "registration.json"
            write_targeted_validation_artifacts(
                design_path=design_path,
                parent_sample_path=parent_sample,
                parent_match_path=parent_match,
                output=sample_path, match_output=match_path,
                registration_output=registration_path)
            outcomes = self.outcomes(design_path, sample_path, match_path)
            outcomes["sample_sha256"] = "0" * 64
            outcomes_path = root / "outcomes.json"
            outcomes_path.write_text(json.dumps(outcomes, sort_keys=True))
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                status = uncertainty_cli([
                    "targeted-validation",
                    "--design", str(design_path),
                    "--registration", str(registration_path),
                    "--sample", str(sample_path),
                    "--match-list", str(match_path),
                    "--outcomes", str(outcomes_path),
                    "--out", str(root / "result.json"),
                ])
            self.assertEqual(status, 1)
            self.assertIn("private artifact hashes do not close", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
