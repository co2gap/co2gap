from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "lab"))

import anchor_refs  # noqa: E402
from store import DayWriter  # noqa: E402


class AnchorDescriptionTests(unittest.TestCase):
    def test_target_segment_and_fallback_match_the_method_description(self):
        self.assertEqual(anchor_refs._pick_segment([500, 1000, 1500, 2000, 3000]),
                         (1500, 2000))
        self.assertEqual(anchor_refs._pick_segment([500, 1000, 1500]),
                         (1000, 1500))
        self.assertIn("1500-2000", anchor_refs.ANCHOR_METHOD)
        self.assertIn("fallback", anchor_refs.ANCHOR_METHOD)


class BackfillPathTests(unittest.TestCase):
    def test_pi_backfill_uses_the_configured_flight_directory_everywhere(self):
        script = (ROOT / "scripts/backfill.sh").read_text()
        self.assertEqual(script.count("$ROOT/data/flights"), 1)  # default only
        self.assertIn('local iso="$1" d="$FLIGHTS_DIR/$1"', script)
        self.assertIn("$FLIGHTS_DIR/$ISO/flights.parquet", script)
        self.assertEqual(script.count('rm -rf "$FLIGHTS_DIR/$ISO"'), 2)
        self.assertIn('export ADSB_FLIGHTS_DIR="$FLIGHTS_DIR"', script)

    def test_pi_backfill_recognises_a_valid_day_in_the_configured_directory(self):
        with tempfile.TemporaryDirectory(prefix="co2gap-backfill-path-") as raw:
            root = Path(raw)
            alternate = root / "alternate-flights"
            writer = DayWriter(alternate, "2026-01-01")
            point = SimpleNamespace(
                t=1.0, lat=45.0, lon=9.0, alt=1000.0,
                gs=200.0, ias=190.0, vs_rep=0.0)
            writer.add({"day": "2026-01-01"}, [point])
            writer.flush()

            (root / "venv/bin").mkdir(parents=True)
            (root / "venv/bin/python").symlink_to(sys.executable)
            (root / "pipeline").symlink_to(ROOT / "pipeline", target_is_directory=True)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            (fake_bin / "flock").write_text("#!/bin/sh\nexit 0\n")
            (fake_bin / "flock").chmod(0o755)
            (fake_bin / "date").write_text(
                "#!/bin/sh\n"
                "case \" $* \" in\n"
                "  *' -d '*) echo 2025-12-31;;\n"
                "  *' +%s '*) echo 1000;;\n"
                "  *) echo '2026-01-01 00:00:00';;\n"
                "esac\n")
            (fake_bin / "date").chmod(0o755)
            env = dict(os.environ)
            env.update({"ADSB_ROOT": str(root),
                        "ADSB_FLIGHTS_DIR": str(alternate),
                        "PATH": f"{fake_bin}:/usr/bin:/bin"})
            result = subprocess.run(
                ["/bin/bash", str(ROOT / "scripts/backfill.sh"),
                 "2026-01-01", "2026-01-01"],
                env=env, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(f"out={alternate}", result.stdout)
            self.assertIn("1 saltati", result.stdout)
            self.assertFalse((root / "data/flights").exists())


if __name__ == "__main__":
    unittest.main()
