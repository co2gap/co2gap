from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "lab"))

import headline_check  # noqa: E402
import site_build  # noqa: E402


class HeadlineTests(unittest.TestCase):
    def test_exact_snapshot_passes_and_any_change_fails(self):
        path = ROOT / "release-headlines.json"
        snapshot = json.loads(path.read_text())
        values = snapshot["values"]
        headline_check.verify_release_headlines(
            values, path, release_id=snapshot["release_id"])
        changed = dict(values)
        changed["kea_pct"] += 1e-12
        with self.assertRaisesRegex(headline_check.HeadlineError, "kea_pct"):
            headline_check.verify_release_headlines(
                changed, path, release_id=snapshot["release_id"])


class SiteBuildTests(unittest.TestCase):
    @staticmethod
    def snapshot(root: Path) -> dict[str, str]:
        return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in root.rglob("*") if path.is_file()}

    def test_missing_coverage_audit_is_fatal(self):
        with tempfile.TemporaryDirectory(prefix="co2gap-coverage-") as raw, \
                patch.object(site_build, "COVERAGE", Path(raw) / "missing.json"), \
                self.assertRaisesRegex(SystemExit, "audit di copertura assente"):
            site_build.coverage_note(["2026-01-01"])

    def test_late_build_failure_preserves_previous_generation(self):
        with tempfile.TemporaryDirectory(prefix="co2gap-site-atomic-") as raw:
            parent = Path(raw)
            destination = parent / "site"
            destination.mkdir()
            (destination / "old.html").write_text("old generation")
            before = self.snapshot(destination)

            def fail_late(*args, **kwargs):
                site_build.OUT.write_text("<!doctype html>partial")
                raise RuntimeError("injected late failure")

            with patch.object(site_build, "OUT", destination / "index.html"), \
                    patch.object(site_build, "OUT_METH",
                                 destination / "methodology.html"), \
                    patch.object(site_build, "_build_site_tree", fail_late), \
                    self.assertRaisesRegex(RuntimeError, "injected late failure"):
                site_build.main(site_build.EXPLORATORY_PROFILE)
            self.assertEqual(self.snapshot(destination), before)
            self.assertFalse(list(parent.glob(".site.build-*")))

    def test_complete_generation_replaces_stale_files(self):
        with tempfile.TemporaryDirectory(prefix="co2gap-site-promote-") as raw:
            parent = Path(raw)
            destination = parent / "site"
            destination.mkdir()
            (destination / "obsolete.html").write_text("stale")

            def build_fixture(*args, **kwargs):
                for relative in site_build.GENERATED_SITE_FILES:
                    content = ("<!doctype html>fixture" if relative.endswith(".html")
                               else "fixture")
                    (site_build.OUT.parent / relative).write_text(content)

            with patch.object(site_build, "OUT", destination / "index.html"), \
                    patch.object(site_build, "OUT_METH",
                                 destination / "methodology.html"), \
                    patch.object(site_build, "_build_site_tree", build_fixture):
                site_build.main(site_build.EXPLORATORY_PROFILE)
            self.assertFalse((destination / "obsolete.html").exists())
            self.assertEqual(
                {path.name for path in destination.iterdir()},
                site_build.GENERATED_SITE_FILES | site_build.STATIC_SITE_FILES)


class SiteProfileTests(unittest.TestCase):
    def test_release_profile_requires_explicit_inputs_before_staging(self):
        empty = {name: "" for name in site_build.RELEASE_REQUIRED_ENV}
        with patch.dict(os.environ, empty), \
                patch.object(site_build, "_prepare_site_stage") as prepare, \
                self.assertRaisesRegex(SystemExit, "release profile requires explicit"):
            site_build.main(site_build.RELEASE_PROFILE)
        prepare.assert_not_called()

    def test_phase_import_failure_is_fatal_in_release(self):
        df = pd.DataFrame({"day": ["2026-01-01"], "flight_id": [1]})
        with patch.object(site_build, "_phase_api", side_effect=ImportError("boom")), \
                self.assertRaisesRegex(RuntimeError, "release profile requires.*boom"):
            site_build.phase_attribution(df, release_required=True)

    def test_phase_import_failure_is_a_loud_exploratory_fallback(self):
        df = pd.DataFrame({"day": ["2026-01-01"], "flight_id": [1]})
        output = io.StringIO()
        with patch.object(site_build, "_phase_api", side_effect=ImportError("boom")), \
                redirect_stderr(output):
            result = site_build.phase_attribution(df, release_required=False)
        self.assertIsNone(result)
        self.assertIn("EXPLORATORY FALLBACK", output.getvalue())
        self.assertIn("boom", output.getvalue())

    def test_partial_phase_is_fatal_only_in_release(self):
        df = pd.DataFrame({
            "day": ["2026-01-01", "2026-01-01"],
            "flight_id": [1, 2],
        })
        phase = pd.DataFrame({"day": ["2026-01-01"], "flight_id": [1]})
        api = (lambda path: phase, None, None, None)
        with patch.object(site_build, "_phase_api", return_value=api), \
                self.assertRaisesRegex(RuntimeError, "1 missing"):
            site_build.phase_attribution(df, release_required=True)

        output = io.StringIO()
        with patch.object(site_build, "_phase_api", return_value=api), \
                redirect_stderr(output):
            result = site_build.phase_attribution(df, release_required=False)
        self.assertIsNone(result)
        self.assertIn("1 missing", output.getvalue())


if __name__ == "__main__":
    unittest.main()
