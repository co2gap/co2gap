from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "lab"), str(ROOT / "pipeline"), str(ROOT)]

from tow_validation import (TowValidationError, add_cells, analyse,  # noqa: E402
                            compare_sources, prepare_co2gap, prepare_prc,
                            require_openap_version, require_outside_repository,
                            validate_design, verify_external_file)
from release_manifest import ReleaseManifest  # noqa: E402


LIMITS = {
    "A320": {"model": "a320", "oew_kg": 40_000.0, "mtow_kg": 80_000.0},
    "B738": {"model": "b738", "oew_kg": 40_000.0, "mtow_kg": 80_000.0},
}
EDGES = [150.0, 500.0, 1000.0, 1500.0, 2500.0, 4000.0, float("inf")]


def prc_rows(n=4):
    return pd.DataFrame({
        "flight_id": np.arange(n),
        "date": ["2022-01-01", "2022-01-02", "2022-01-01", "2022-01-02"][:n],
        "aircraft_type": ["A320"] * n,
        "flown_distance": [200.0] * n,
        "tow": [56_000.0, 57_000.0, 58_000.0, 59_000.0][:n],
    })


def model_rows(n=4):
    return pd.DataFrame({
        "flight_id": np.arange(n),
        "day": ["2026-01-01"] * n,
        "typecode": ["A320"] * n,
        "origin_icao": ["LIRF"] * n,
        "dest_icao": ["EGLL"] * n,
        "gc_km": [350.0] * n,
        "flown_km": [370.0] * n,
        "coverage_frac": [0.95] * n,
        "flown_ge_09gc": [True] * n,
        "init_mass_kg": [60_000.0] * n,
        "load_factor": [0.82] * n,
        "reserve_kg": [2000.0] * n,
    })


class DesignTests(unittest.TestCase):
    def setUp(self):
        self.design_path = ROOT / "tow-validation-design.json"
        self.manifest_path = ROOT / "release-manifest.json"
        self.design = json.loads(self.design_path.read_text())

    def test_tracked_design_matches_frozen_release(self):
        result = validate_design(self.design, self.design_path, self.manifest_path)
        self.assertEqual(result["minimum_rows"], 100)
        self.assertEqual(result["manifest"].release_id, "2026-09-01")
        self.assertEqual(result["distance_edges"][-1], float("inf"))

    def test_design_rejects_overclaim_or_changed_manifest(self):
        broken = copy.deepcopy(self.design)
        broken["output_policy"]["site_or_release_change"] = True
        with self.assertRaisesRegex(TowValidationError, "output policy"):
            validate_design(broken, self.design_path, self.manifest_path)
        broken = copy.deepcopy(self.design)
        broken["status"] = "edited_after_results"
        with self.assertRaisesRegex(TowValidationError, "not frozen"):
            validate_design(broken, self.design_path, self.manifest_path)
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "release-manifest.json"
            manifest.write_text(self.manifest_path.read_text() + "\n")
            with self.assertRaisesRegex(TowValidationError, "differs"):
                validate_design(self.design, self.design_path, manifest)

    def test_external_file_must_be_outside_repo_and_match_identity(self):
        with self.assertRaisesRegex(TowValidationError, "outside every git worktree"):
            require_outside_repository(ROOT / "private.csv", may_not_exist=True)
        with self.assertRaisesRegex(TowValidationError, "outside every git worktree"):
            require_outside_repository(
                ROOT.parent / "adsb-co2" / "private.csv", may_not_exist=True)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "flight_list.csv"
            path.write_bytes(b"abc")
            with self.assertRaisesRegex(TowValidationError, "size"):
                verify_external_file(path, {"name": "flight_list.csv", "bytes": 4,
                                            "md5": "00000000000000000000000000000000"})

    def test_wrong_openap_version_fails(self):
        manifest = ReleaseManifest.load(self.manifest_path)
        with patch("tow_validation.package_version", return_value="2.4"):
            with self.assertRaisesRegex(TowValidationError, "requires OpenAP 2.6.0"):
                require_openap_version(manifest)


class FrozenResultTests(unittest.TestCase):
    def test_tracked_aggregate_result_matches_design_and_implementation(self):
        result = json.loads((ROOT / "tow-validation-result.json").read_text())
        digest = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["design_sha256"],
                         digest(ROOT / "tow-validation-design.json"))
        self.assertEqual(result["input_validation"]["implementation_sha256"],
                         digest(ROOT / "lab/tow_validation.py"))
        self.assertEqual(result["input_validation"]["requirements_lock_sha256"],
                         digest(ROOT / "requirements-lab.lock"))
        self.assertTrue(result["input_validation"]["release_flight_input_verified"])
        self.assertEqual(result["comparison"]["retained_cells"], 77)
        self.assertAlmostEqual(result["comparison"]["release_row_coverage"],
                               0.9397646753)
        self.assertAlmostEqual(
            result["comparison"]["primary"]["co2gap_minus_prc_pp_mtow"],
            1.2088403021)
        for cell in result["comparison"]["cells"]:
            self.assertGreaterEqual(cell["co2gap_rows"], 100)
            self.assertGreaterEqual(cell["prc_rows"], 100)

    def test_tracked_result_contains_no_flight_or_date_rows(self):
        text = (ROOT / "tow-validation-result.json").read_text()
        for forbidden in ('"flight_id"', '"date_utc"', "2022-01-01", "2026-01-01"):
            self.assertNotIn(forbidden, text)


class EligibilityTests(unittest.TestCase):
    def setUp(self):
        self.design = json.loads((ROOT / "tow-validation-design.json").read_text())

    def test_prc_funnel_keeps_only_registered_scope(self):
        frame = pd.DataFrame({
            "flight_id": range(7),
            "date": ["2022-01-01", "bad", "2021-01-01", "2022-01-01",
                     "2022-01-01", "2022-01-01", "2022-01-01"],
            "aircraft_type": ["A320", "A320", "A320", "ZZZZ", "A320", "A320", "A320"],
            "flown_distance": [200, 200, 200, 200, 50, 200, 200],
            "tow": [60_000, 60_000, 60_000, 60_000, 60_000, 39_000, 81_000],
        })
        kept, funnel = prepare_prc(frame, LIMITS)
        self.assertEqual(kept["flight_id"].tolist(), [0])
        self.assertEqual(funnel["eligible_rows"], 1)
        self.assertEqual(funnel["unsupported_aircraft_type"], 1)
        self.assertEqual(funnel["mass_below_openap_oew"], 1)
        self.assertEqual(funnel["mass_above_allowed_openap_mtow"], 1)
        tolerant, _ = prepare_prc(frame, LIMITS, mtow_factor=1.02)
        self.assertEqual(tolerant["flight_id"].tolist(), [0, 6])

    def test_duplicate_prc_and_model_keys_fail(self):
        frame = prc_rows()
        frame.loc[1, "flight_id"] = frame.loc[0, "flight_id"]
        with self.assertRaisesRegex(TowValidationError, "not unique"):
            prepare_prc(frame, LIMITS)
        frame = model_rows()
        frame.loc[1, "flight_id"] = frame.loc[0, "flight_id"]
        with self.assertRaisesRegex(TowValidationError, "not unique"):
            prepare_co2gap(frame, LIMITS, self.design)

    def test_co2gap_gate_and_frozen_assumptions_are_enforced(self):
        frame = model_rows()
        frame.loc[1, "coverage_frac"] = 0.8
        frame.loc[2, "origin_icao"] = None
        frame.loc[3, "flown_ge_09gc"] = False
        kept, funnel = prepare_co2gap(frame, LIMITS, self.design)
        self.assertEqual(kept["flight_id"].tolist(), [0])
        self.assertEqual(funnel["quality_gate_failure"], 3)
        frame = model_rows()
        frame.loc[0, "reserve_kg"] = 1000
        with self.assertRaisesRegex(TowValidationError, "mass assumptions"):
            prepare_co2gap(frame, LIMITS, self.design)
        frame.loc[0, "reserve_kg"] = np.nan
        with self.assertRaisesRegex(TowValidationError, "mass assumptions"):
            prepare_co2gap(frame, LIMITS, self.design)


class ComparisonTests(unittest.TestCase):
    @staticmethod
    def frames(include_unmatched=False):
        rows = []
        for cell_type, distance, fraction in (("A320", 300.0, 0.7),
                                               ("A320", 700.0, 0.9)):
            for i in range(100):
                rows.append({"flight_id": len(rows),
                             "date_utc": pd.Timestamp(f"2022-01-{i % 10 + 1:02d}", tz="UTC"),
                             "aircraft_type": cell_type, "distance_km": distance,
                             "mass_fraction": fraction})
        prc = add_cells(pd.DataFrame(rows), EDGES)
        rows = []
        for cell_type, distance, fraction in (("A320", 300.0, 0.8),
                                               ("A320", 700.0, 0.9)):
            for i in range(100):
                rows.append({"flight_id": len(rows),
                             "date_utc": pd.Timestamp("2026-01-01", tz="UTC"),
                             "aircraft_type": cell_type, "distance_km": distance,
                             "mass_fraction": fraction})
        if include_unmatched:
            for i in range(100):
                rows.append({"flight_id": len(rows),
                             "date_utc": pd.Timestamp("2026-01-01", tz="UTC"),
                             "aircraft_type": "B738", "distance_km": 1200.0,
                             "mass_fraction": 0.8})
        model = add_cells(pd.DataFrame(rows), EDGES)
        return prc, model

    def test_poststratified_primary_has_known_value(self):
        prc, model = self.frames()
        result = compare_sources(prc, model, minimum_rows=100,
                                 minimum_coverage=0.8,
                                 bootstrap_replicates=200, bootstrap_seed=42)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["retained_cells"], 2)
        self.assertAlmostEqual(
            result["primary"]["co2gap_minus_prc_pp_mtow"], 5.0)
        self.assertAlmostEqual(result["release_row_coverage"], 1.0)
        self.assertEqual(result["day_block_sampling_precision"]["replicates"], 200)

    def test_coverage_guard_is_measured_and_can_fail(self):
        prc, model = self.frames(include_unmatched=True)
        result = compare_sources(prc, model, minimum_rows=100,
                                 minimum_coverage=0.8)
        self.assertEqual(result["status"], "incomplete")
        self.assertAlmostEqual(result["release_row_coverage"], 2 / 3)

    def test_result_is_aggregate_and_bootstrap_is_reproducible(self):
        prc, model = self.frames()
        first = compare_sources(prc, model, minimum_rows=100,
                                minimum_coverage=0.8,
                                bootstrap_replicates=200, bootstrap_seed=9)
        second = compare_sources(prc.sample(frac=1, random_state=1),
                                 model.sample(frac=1, random_state=2),
                                 minimum_rows=100, minimum_coverage=0.8,
                                 bootstrap_replicates=200, bootstrap_seed=9)
        self.assertEqual(first, second)
        public = json.dumps(first)
        for forbidden in ('"flight_id"', "2022-01-01", "2026-01-01"):
            self.assertNotIn(forbidden, public)


if __name__ == "__main__":
    unittest.main()
