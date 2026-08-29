from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

            def fail_late():
                site_build.OUT.write_text("<!doctype html>partial")
                raise RuntimeError("injected late failure")

            with patch.object(site_build, "OUT", destination / "index.html"), \
                    patch.object(site_build, "OUT_METH",
                                 destination / "methodology.html"), \
                    patch.object(site_build, "_build_site_tree", fail_late), \
                    self.assertRaisesRegex(RuntimeError, "injected late failure"):
                site_build.main()
            self.assertEqual(self.snapshot(destination), before)
            self.assertFalse(list(parent.glob(".site.build-*")))

    def test_complete_generation_replaces_stale_files(self):
        with tempfile.TemporaryDirectory(prefix="co2gap-site-promote-") as raw:
            parent = Path(raw)
            destination = parent / "site"
            destination.mkdir()
            (destination / "obsolete.html").write_text("stale")

            def build_fixture():
                for relative in site_build.GENERATED_SITE_FILES:
                    content = ("<!doctype html>fixture" if relative.endswith(".html")
                               else "fixture")
                    (site_build.OUT.parent / relative).write_text(content)

            with patch.object(site_build, "OUT", destination / "index.html"), \
                    patch.object(site_build, "OUT_METH",
                                 destination / "methodology.html"), \
                    patch.object(site_build, "_build_site_tree", build_fixture):
                site_build.main()
            self.assertFalse((destination / "obsolete.html").exists())
            self.assertEqual(
                {path.name for path in destination.iterdir()},
                site_build.GENERATED_SITE_FILES | site_build.STATIC_SITE_FILES)


if __name__ == "__main__":
    unittest.main()
