from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "pipeline"), str(ROOT / "ingest"),
                str(ROOT / "lab"), str(ROOT)]

import decompose  # noqa: E402
from combined_mass import (CombinedMassError, load_combined_mass_design,  # noqa: E402
                           requested_mass_fraction,
                           validate_combined_mass_design)
from emissions import _estimate_fuel_scalar, estimate_fuel  # noqa: E402


class CombinedMassDesignTests(unittest.TestCase):
    def setUp(self):
        self.path = ROOT / "combined-mass-sensitivity-design.json"
        self.design = __import__("json").loads(self.path.read_text())

    def test_tracked_design_and_source_cells_validate(self):
        loaded = load_combined_mass_design(self.path)
        self.assertEqual(len(loaded["pattern"]), 77)
        self.assertEqual(loaded["registered_samples"], {
            "5e918a8e421431856ea06d7b921855d84efcde1f1e871bda3c59043b035d0a96": 20260901,
            "fcb3fe218f75b968989db993b403ccb2351634e5bfbc8cc6d0c45da6b5633c34": 20260902,
            "10d2f9789170cbafa86615e7586273a6a03a5bf3ac1b09f290d7d1755dbea2f4": 20260903,
        })
        self.assertAlmostEqual(loaded["exact_release_coverage"], 0.9397646753)
        self.assertEqual([row["id"] for row in loaded["config"]["scenarios"]],
                         ["nominal", "prc_cell_mean_alignment"])

    def test_design_rejects_changed_source_or_weakened_nonclaims(self):
        for mutate, message in (
            (lambda value: value["source"].__setitem__("result_sha256", "0" * 64),
             "result hash"),
            (lambda value: value["output_policy"].__setitem__(
                "site_or_release_change", True), "output policy"),
            (lambda value: value["cell_definition"].__setitem__(
                "unsupported_cell_policy", "interpolate"), "unsupported-cell"),
            (lambda value: value["scenarios"][1].__setitem__(
                "combined_mass_pattern_scale", 0.5), "ids or scales"),
            (lambda value: value["sampling"].__setitem__(
                "rows_per_run", 3000), "sampling population"),
        ):
            broken = copy.deepcopy(self.design)
            mutate(broken)
            with self.subTest(message=message):
                with self.assertRaisesRegex(CombinedMassError, message):
                    validate_combined_mass_design(broken, self.path)

    def test_lookup_uses_left_closed_bands_and_never_extrapolates(self):
        loaded = load_combined_mass_design(self.path)
        short, supported, cell = requested_mass_fraction("a20n", 150.0, loaded)
        self.assertTrue(supported)
        self.assertEqual(cell, "A20N|150-500")
        self.assertLess(short, 0.0)
        below_edge = requested_mass_fraction("A20N", 499.999, loaded)
        at_edge = requested_mass_fraction("A20N", 500.0, loaded)
        self.assertEqual(below_edge[2], "A20N|150-500")
        self.assertEqual(at_edge[2], "A20N|500-1000")
        value, supported, cell = requested_mass_fraction("A20N", 4500.0, loaded)
        self.assertEqual((value, supported, cell), (0.0, False, "A20N|4000+"))
        with self.assertRaisesRegex(CombinedMassError, "outside"):
            requested_mass_fraction("A20N", 149.999, loaded)


def synthetic_flight():
    points = [
        SimpleNamespace(
            t=float(i * 60), lat=40.0, lon=8.0 + i * 0.04,
            alt=30000.0, gs=430.0, ias=250.0,
        )
        for i in range(21)
    ]
    return SimpleNamespace(typecode="A320", points=points)


class CombinedMassFuelTests(unittest.TestCase):
    def test_zero_hook_is_exact_and_signed_mass_changes_are_physical(self):
        flight = synthetic_flight()
        nominal = estimate_fuel(flight)
        explicit_zero = estimate_fuel(flight, mass_adjustment_frac_mtow=0.0)
        self.assertTrue(nominal.ok)
        self.assertEqual(nominal, explicit_zero)
        lighter = estimate_fuel(flight, mass_adjustment_frac_mtow=-0.02)
        heavier = estimate_fuel(flight, mass_adjustment_frac_mtow=0.02)
        self.assertTrue(lighter.ok and heavier.ok)
        self.assertLess(lighter.init_mass_kg, nominal.init_mass_kg)
        self.assertLess(lighter.co2_kg, nominal.co2_kg)
        self.assertGreater(heavier.init_mass_kg, nominal.init_mass_kg)
        self.assertGreater(heavier.co2_kg, nominal.co2_kg)
        self.assertAlmostEqual(heavier.mass_adjustment_kg, 0.02 * heavier.mtow_kg)
        failed = estimate_fuel(flight, mass_adjustment_frac_mtow=-1.0)
        self.assertFalse(failed.ok)
        self.assertEqual(failed.reason, "combined mass adjustment below OEW")

    def test_scalar_reference_accepts_the_same_combined_mass_hook(self):
        flight = synthetic_flight()
        nominal = _estimate_fuel_scalar(flight, iters=4)
        lighter = _estimate_fuel_scalar(
            flight, iters=4, mass_adjustment_frac_mtow=-0.02)
        self.assertTrue(nominal.ok and lighter.ok)
        self.assertLess(lighter.init_mass_kg, nominal.init_mass_kg)
        self.assertLess(lighter.co2_kg, nominal.co2_kg)
        self.assertAlmostEqual(lighter.mass_adjustment_kg, -0.02 * lighter.mtow_kg)

    def test_decomposition_pairs_the_same_mass_fraction_across_baselines(self):
        fraction = -0.04

        def fuel(_profile, **kwargs):
            requested = kwargs["mass_adjustment_frac_mtow"]
            return SimpleNamespace(
                ok=True, co2_kg=100.0 + requested,
                init_mass_kg=60000.0 + requested * 80000.0,
                mtow_kg=80000.0, mass_capped_at_mtow=False)

        with patch.object(decompose, "openap_model", return_value="a320"), \
             patch.object(decompose, "_get_ac", return_value={}), \
             patch.object(decompose, "optimal_cruise_alt_ft", return_value=30000.0), \
             patch.object(decompose, "_cruise_tas_kt", return_value=430.0), \
             patch.object(decompose, "mean_along_track_wind_ms", return_value=0.0), \
             patch.object(decompose, "mean_wind_along_track", return_value=0.0), \
             patch.object(decompose, "_build_profile", return_value=object()), \
             patch.object(decompose, "estimate_fuel", side_effect=fuel) as estimate, \
             patch.object(decompose, "_cruise_state", return_value=(None, None)), \
             patch.object(decompose, "enroute_dist_ratio", return_value=(1.0, 100.0)):
            result = decompose.decompose_flight(
                "A320", 120.0, 300.0, 330.0,
                np.array([40.0, 41.0]), np.array([8.0, 9.0]), 0, object(),
                mass_adjustment_frac_mtow=fraction,
                include_mass_diagnostics=True)

        self.assertIsNotNone(result)
        self.assertEqual(estimate.call_count, 2)
        self.assertTrue(all(
            call.kwargs["mass_adjustment_frac_mtow"] == fraction
            for call in estimate.call_args_list))
        diagnostic = result["combined_mass_diagnostics"]
        self.assertEqual(diagnostic["requested_fraction_mtow"], fraction)
        self.assertEqual(diagnostic["ideal_init_mass_kg"], 56800.0)
        self.assertEqual(diagnostic["hybrid_init_mass_kg"], 56800.0)


if __name__ == "__main__":
    unittest.main()
