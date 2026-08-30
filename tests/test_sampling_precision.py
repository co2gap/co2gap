from __future__ import annotations

import copy
import itertools
import math
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lab"))
from sampling_precision import (FROZEN, METRICS, PairedSamplingPrecision,
                                SamplingPrecisionError, ratio_points_gradient)


def make_sample(cells):
    rows = []
    for name, population, ids in cells:
        rows.extend({"day": "2026-01-01", "flight_id": fid, "stratum": name,
                     "population_n": population, "sample_n": len(ids),
                     "weight": population / len(ids)} for fid in ids)
    return {"rows": rows, "sample_rows": len(rows), "strata": len(cells),
            "population_rows": sum(c[1] for c in cells)}


def run(sample, nominal, changed, frozen=None):
    engine = PairedSamplingPrecision(sample, ["nominal", "changed"], "nominal")
    totals = {sid: np.zeros(3) for sid in ("nominal", "changed", FROZEN)}
    for row in sample["rows"]:
        fid = row["flight_id"]
        values = {"nominal": nominal[fid], "changed": changed[fid]}
        freeze = nominal[fid] if frozen is None else frozen[fid]
        engine.add((row["day"], fid), values, freeze)
        for sid, value in {**values, FROZEN: freeze}.items():
            totals[sid] += row["weight"] * np.asarray(value)
    result, audit = engine.finish(totals)
    return result, audit


class SamplingPrecisionTests(unittest.TestCase):
    def setUp(self):
        self.sample = make_sample([("a", 4, [0, 1])])
        self.nominal = {0: [100, 100, 110], 1: [140, 100, 120]}
        self.changed = {0: [110, 100, 115], 1: [180, 100, 123]}

    def test_known_paired_variance_includes_fpc_and_cancellation(self):
        result, _ = run(self.sample, self.nominal, self.changed)
        changed = result["by_scenario"]["changed"]
        metric = changed["delta_from_nominal"]["metrics"]["gap_total_pct"]
        self.assertAlmostEqual(metric["estimate"], 25)
        self.assertAlmostEqual(metric["standard_error_pp"] ** 2, 112.5)
        self.assertAlmostEqual(metric["largest_stratum_variance_fraction"], 1)
        naive = sum(result["by_scenario"][sid]["level"]["metrics"]["gap_total_pct"]["standard_error_pp"]**2
                    for sid in ("changed", "nominal"))
        self.assertAlmostEqual(naive, 812.5)
        self.assertLess(metric["standard_error_pp"] ** 2, naive)
        cov = np.asarray(changed["delta_from_nominal"]["covariance_pp2"])
        self.assertAlmostEqual(cov[0, 0], cov[1, 1] + cov[2, 2] + 2 * cov[1, 2])
        self.assertGreaterEqual(np.linalg.eigvalsh(cov).min(), -1e-12)

    def test_identical_scenarios_and_reference_have_exact_zero_contrast_error(self):
        result, _ = run(self.sample, self.nominal, self.nominal)
        for entry in [result["by_scenario"]["changed"]["delta_from_nominal"],
                      result["by_scenario"]["nominal"]["delta_from_nominal"],
                      result["nominal_minus_frozen"]]:
            self.assertEqual(entry["covariance_pp2"], np.zeros((3, 3)).tolist())
            for metric in entry["metrics"].values():
                self.assertEqual(metric["estimate"], 0)
                self.assertEqual(metric["standard_error_pp"], 0)
                self.assertIsNone(metric["relative_standard_error"])
                self.assertIsNone(metric["largest_stratum_variance_fraction"])

    def test_censuses_including_singletons_have_zero_sampling_variance(self):
        sample = make_sample([("a", 1, [0]), ("b", 1, [1])])
        result, _ = run(sample, self.nominal, self.changed)
        self.assertEqual(result["census_strata"], 2)
        self.assertEqual(result["by_scenario"]["changed"]["delta_from_nominal"]["covariance_pp2"],
                         np.zeros((3, 3)).tolist())

    def test_jacobian_matches_finite_differences_with_random_denominator(self):
        totals = np.array([1300.0, 1100.0, 1150.0])
        _, gradient = ratio_points_gradient(totals)
        for j in range(3):
            step = np.zeros(3)
            step[j] = 0.01
            plus = ratio_points_gradient(totals + step)[0]
            minus = ratio_points_gradient(totals - step)[0]
            np.testing.assert_allclose((plus - minus) / 0.02, gradient[:, j], rtol=1e-8, atol=1e-10)

    def test_exhaustive_two_strata_sampling_matches_exact_variance_for_fixed_denominator(self):
        nominal = {i: [100 + i * i, 100, 110 + i] for i in range(7)}
        changed = {i: [nominal[i][0] + (i + 1)**2, 100, 112 + i] for i in range(7)}
        estimates, variance_estimates = [], []
        for first in itertools.combinations(range(3), 2):
            for second in itertools.combinations(range(3, 7), 2):
                sample = make_sample([("a", 3, first), ("b", 4, second)])
                result, _ = run(sample, nominal, changed)
                metric = result["by_scenario"]["changed"]["delta_from_nominal"]["metrics"]["gap_total_pct"]
                estimates.append(metric["estimate"])
                variance_estimates.append(metric["standard_error_pp"] ** 2)
        self.assertEqual(len(estimates), 18)
        self.assertAlmostEqual(np.var(estimates), np.mean(variance_estimates), places=10)

    def test_independent_influence_reconstruction_and_scale_invariance(self):
        self.nominal[1][1] = 130
        self.changed[1][1] = 150
        result, audit = run(self.sample, self.nominal, self.changed)
        # Independently expand d(A/B) = dA/B - A*dB/B^2, with paired rows.
        totals = np.asarray(audit["runner_totals_kg"]).reshape(3, 3)
        projections = []
        for fid in (0, 1):
            values = []
            for index, source in enumerate((self.nominal, self.changed)):
                r, i, h = source[fid]
                R, I, H = totals[index]
                values.append(100 * np.array([r / I - R * i / I**2,
                                              h / I - H * i / I**2,
                                              (r - h) / I - (R - H) * i / I**2]))
            projections.append(values[1] - values[0])
        expected = 4 * (4 - 2) / 2 * np.cov(np.array(projections), rowvar=False, ddof=1)
        actual = result["by_scenario"]["changed"]["delta_from_nominal"]["covariance_pp2"]
        np.testing.assert_allclose(actual, expected, rtol=1e-12, atol=1e-12)
        for scale in (1e-6, 1e6):
            scaled, _ = run(self.sample, {k: np.asarray(v)*scale for k, v in self.nominal.items()},
                            {k: np.asarray(v)*scale for k, v in self.changed.items()})
            np.testing.assert_allclose(scaled["by_scenario"]["changed"]["delta_from_nominal"]["covariance_pp2"],
                                       actual, rtol=1e-12, atol=1e-12)

    def test_invalid_designs_fail_instead_of_silently_dropping_cells(self):
        bad_samples = [make_sample([("a", 2, [0])])]
        for field, value in (("weight", 3), ("sample_n", True), ("population_n", 0),
                             ("sample_n", 5), ("stratum", ""), ("flight_id", True)):
            altered = copy.deepcopy(self.sample)
            altered["rows"][0][field] = value
            bad_samples.append(altered)
        for field in ("sample_rows", "population_rows", "strata"):
            altered = copy.deepcopy(self.sample)
            altered[field] += 1
            bad_samples.append(altered)
        altered = copy.deepcopy(self.sample)
        altered["rows"][1] = altered["rows"][0]
        bad_samples.append(altered)
        for sample in bad_samples:
            with self.subTest(sample=sample):
                with self.assertRaises(SamplingPrecisionError):
                    PairedSamplingPrecision(sample, ["nominal"], "nominal")

    def test_missing_duplicate_unpaired_or_invalid_values_fail(self):
        engine = PairedSamplingPrecision(self.sample, ["nominal", "changed"], "nominal")
        with self.assertRaisesRegex(SamplingPrecisionError, "incomplete"):
            engine.finish({})
        with self.assertRaisesRegex(SamplingPrecisionError, "every paired"):
            engine.add(("2026-01-01", 0), {"nominal": self.nominal[0]}, self.nominal[0])
        for value in (float("nan"), float("inf"), 0.0, -1.0):
            with self.assertRaises(SamplingPrecisionError):
                engine.add(("2026-01-01", 0), {"nominal": [value, 100, 110], "changed": self.changed[0]}, self.nominal[0])
        engine.add(("2026-01-01", 0), {"nominal": self.nominal[0], "changed": self.changed[0]}, self.nominal[0])
        with self.assertRaisesRegex(SamplingPrecisionError, "repeated"):
            engine.add(("2026-01-01", 0), {"nominal": self.nominal[0], "changed": self.changed[0]}, self.nominal[0])
        with self.assertRaisesRegex(SamplingPrecisionError, "unknown"):
            engine.add(("2026-01-01", 99), {}, [])

    def test_malformed_scenario_sets_are_rejected(self):
        for ids, nominal in (([], "nominal"), (["nominal", "nominal"], "nominal"),
                             (["nominal", FROZEN], "nominal"), (["changed"], "nominal")):
            with self.subTest(ids=ids, nominal=nominal):
                with self.assertRaisesRegex(SamplingPrecisionError, "scenario ids/nominal"):
                    PairedSamplingPrecision(self.sample, ids, nominal)

    def test_wrong_totals_and_invalid_denominators_fail(self):
        for values in ([1, 0, 1], [1, float("inf"), 1], [1, -1, 1], [1, 1]):
            with self.assertRaises(SamplingPrecisionError):
                ratio_points_gradient(values)
        engine = PairedSamplingPrecision(self.sample, ["nominal"], "nominal")
        for fid in (0, 1):
            engine.add(("2026-01-01", fid), {"nominal": self.nominal[fid]}, self.nominal[fid])
        with self.assertRaisesRegex(SamplingPrecisionError, "missing reference"):
            engine.finish({})
        with self.assertRaisesRegex(SamplingPrecisionError, "reconstruct"):
            engine.finish({"nominal": [100, 100, 100], FROZEN: [100, 100, 100]})

    def test_public_result_has_no_keys_cells_or_intervals(self):
        result, audit = run(self.sample, self.nominal, self.changed)
        import json
        public = json.dumps(result)
        for forbidden in ("flight_id", "2026-01-01", '"cells"', '"lower"', '"upper"', '"p_value"'):
            self.assertNotIn(forbidden, public)
        self.assertIn("PRIVATE", audit["privacy"])
        self.assertEqual(len(audit["cells"]), 1)
        self.assertNotIn("flight_id", json.dumps(audit))


if __name__ == "__main__":
    unittest.main()
