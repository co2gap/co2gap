from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from scripts import release_assets


ROOT = Path(__file__).resolve().parents[1]


class ReleaseAssetTests(unittest.TestCase):
    TAG = "v2026.01.01-planes-readsb-prod-0"

    def test_only_a_confirmed_404_means_absent(self):
        error = urllib.error.HTTPError(
            release_assets.api_url(self.TAG), 404, "missing", {}, None)
        with patch.dict(os.environ, {"ADSB_ASSET_API_JSON": ""}), \
                patch("urllib.request.urlopen", side_effect=error), \
                self.assertRaises(SystemExit) as caught:
            release_assets.fetch_release(self.TAG)
        self.assertEqual(caught.exception.code, 44)

    def test_timeout_is_retryable_error_not_absence(self):
        with patch.dict(os.environ, {"ADSB_ASSET_API_JSON": ""}), \
                patch("urllib.request.urlopen",
                      side_effect=urllib.error.URLError(TimeoutError("timed out"))), \
                self.assertRaises(RuntimeError):
            release_assets.fetch_release(self.TAG)

    def test_successful_but_truncated_part_is_rejected_and_removed(self):
        with tempfile.TemporaryDirectory(prefix="co2gap-download-") as raw:
            root = Path(raw)
            (root / "data/raw").mkdir(parents=True)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            asset_python = fake_bin / "asset-python"
            asset_python.write_text(
                "#!/bin/sh\n"
                f"printf '{self.TAG}.tar.aa\\t10\\thttps://example.invalid/aa\\n'\n")
            asset_python.chmod(0o755)
            curl = fake_bin / "curl"
            curl.write_text(
                "#!/bin/sh\n"
                "out=\n"
                "while [ $# -gt 0 ]; do\n"
                "  if [ \"$1\" = -o ]; then out=$2; shift 2; else shift; fi\n"
                "done\n"
                "printf abc > \"$out\"\n")
            curl.chmod(0o755)
            env = dict(os.environ)
            env.update({
                "ADSB_ROOT": str(root),
                "ADSB_ASSET_PY": str(asset_python),
                "PATH": f"{fake_bin}:/usr/bin:/bin",
            })
            result = subprocess.run(
                ["/bin/bash", str(ROOT / "scripts/dl_day_fast.sh"), "2026.01.01"],
                env=env, text=True, capture_output=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("ha 3 byte, attesi 10", result.stdout)
            self.assertFalse((root / "data/raw" / f"{self.TAG}.tar.aa").exists())
            self.assertFalse((root / "data/raw" / f"{self.TAG}.assets.tsv").exists())

    def test_success_persists_exact_asset_manifest_for_ingestion(self):
        with tempfile.TemporaryDirectory(prefix="co2gap-download-manifest-") as raw:
            root = Path(raw)
            (root / "data/raw").mkdir(parents=True)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            expected = (
                f"{self.TAG}.tar.aa\t3\thttps://example.invalid/aa\n"
                f"{self.TAG}.tar.ab\t3\thttps://example.invalid/ab\n")
            asset_python = fake_bin / "asset-python"
            asset_python.write_text("#!/bin/sh\nprintf '" + expected + "'\n")
            asset_python.chmod(0o755)
            curl = fake_bin / "curl"
            curl.write_text(
                "#!/bin/sh\n"
                "out=\n"
                "while [ $# -gt 0 ]; do\n"
                "  if [ \"$1\" = -o ]; then out=$2; shift 2; else shift; fi\n"
                "done\n"
                "printf abc > \"$out\"\n")
            curl.chmod(0o755)
            env = dict(os.environ)
            env.update({
                "ADSB_ROOT": str(root),
                "ADSB_ASSET_PY": str(asset_python),
                "PATH": f"{fake_bin}:/usr/bin:/bin",
            })
            result = subprocess.run(
                ["/bin/bash", str(ROOT / "scripts/dl_day_fast.sh"), "2026.01.01"],
                env=env, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            manifest = root / "data/raw" / f"{self.TAG}.assets.tsv"
            self.assertEqual(manifest.read_text(), expected)
            self.assertEqual((root / "data/raw" / f"{self.TAG}.tar.aa").read_bytes(), b"abc")
            self.assertEqual((root / "data/raw" / f"{self.TAG}.tar.ab").read_bytes(), b"abc")


if __name__ == "__main__":
    unittest.main()
