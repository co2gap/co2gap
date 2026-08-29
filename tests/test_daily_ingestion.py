from __future__ import annotations

import importlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

from store import validate_day_pair  # noqa: E402


def _load_run_daily():
    """Import orchestration without loading OpenAP for these I/O-only tests."""
    source = types.ModuleType("source")

    class BBox:
        def __init__(self, lat_min, lat_max, lon_min, lon_max):
            self.lat_min, self.lat_max = lat_min, lat_max
            self.lon_min, self.lon_max = lon_min, lon_max

        def contains(self, lat, lon):
            return True

    source.BBox = BBox
    source._MultiFileReader = object
    source._decode_member = lambda raw: None
    source.decode_failures = lambda: 0

    trajectories = types.ModuleType("trajectories")
    trajectories.flights_from_trace = lambda obj, box: []
    trajectories.haversine_km = lambda *args: 0.0
    trajectories.mcp_summary = lambda points: {}

    emissions = types.ModuleType("emissions")
    emissions.openap_model = lambda typecode: None
    emissions.estimate_fuel = lambda *args, **kwargs: None

    flightproc = types.ModuleType("flightproc")
    flightproc.process_flight = lambda *args: ([], {})

    airports = types.ModuleType("airports")
    airports.Airports = object

    stubs = {
        "source": source,
        "trajectories": trajectories,
        "emissions": emissions,
        "flightproc": flightproc,
        "airports": airports,
    }
    sys.modules.pop("run_daily", None)
    with patch.dict(sys.modules, stubs), patch.dict(os.environ, {"ADSB_ROOT": str(ROOT)}):
        return importlib.import_module("run_daily")


run_daily = _load_run_daily()


class DailyIngestionTests(unittest.TestCase):
    DAY_TAG = "v2026.01.01-planes-readsb-prod-0"
    DAY = "2026-01-01"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="co2gap-daily-")
        self.root = Path(self.temp.name)
        self.raw = self.root / "data/raw"
        self.out = self.root / "data/flights"
        self.raw.mkdir(parents=True)
        self.out.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def write_assets(self, sizes=(10,)):
        lines = []
        for index, size in enumerate(sizes):
            suffix = f"a{chr(ord('a') + index)}"
            name = f"{self.DAY_TAG}.tar.{suffix}"
            (self.raw / name).write_bytes(b"x" * size)
            lines.append(f"{name}\t{size}\thttps://example.invalid/{suffix}\n")
        (self.raw / f"{self.DAY_TAG}.assets.tsv").write_text("".join(lines))

    @staticmethod
    def process_result(batch):
        point = SimpleNamespace(
            t=1.0, lat=45.0, lon=9.0, alt=1000.0,
            gs=200.0, ias=190.0, vs_rep=0.0)
        return [({"day": None}, [point])], 0

    def fake_members(self, consumed: int, complete: bool):
        def iterator(parts, progress, completed):
            progress[0] = SimpleNamespace(n_bytes=consumed)
            yield b"member"
            completed[0] = complete
        return iterator

    def run_with(self, consumed: int, complete: bool):
        with patch.object(run_daily, "ROOT", self.root), \
                patch.object(run_daily, "OUT_DIR", self.out), \
                patch.object(run_daily, "_init", lambda *args: None), \
                patch.object(run_daily, "_process_batch", self.process_result), \
                patch.object(run_daily, "_iter_raw_members",
                             self.fake_members(consumed, complete)):
            return run_daily.run("2026.01.01", workers=1)

    def test_declared_asset_missing_is_fatal_before_staging(self):
        self.write_assets((10, 10))
        (self.raw / f"{self.DAY_TAG}.tar.ab").unlink()
        with patch.object(run_daily, "ROOT", self.root), \
                self.assertRaisesRegex(SystemExit, "1 missing"):
            run_daily._asset_manifest(self.DAY_TAG)
        self.assertFalse(list((self.root / "data").glob(".flights.*.build-*")))

    def test_low_coverage_exits_and_never_promotes(self):
        self.write_assets()
        with self.assertRaises(SystemExit) as caught:
            self.run_with(consumed=8, complete=True)
        self.assertNotEqual(caught.exception.code, 0)
        self.assertFalse((self.out / self.DAY).exists())
        self.assertFalse(list((self.root / "data").glob(".flights.*.build-*")))

    def test_non_normal_tar_end_exits_even_at_full_byte_count(self):
        self.write_assets()
        with self.assertRaises(SystemExit):
            self.run_with(consumed=10, complete=False)
        self.assertFalse((self.out / self.DAY).exists())

    def test_complete_day_is_promoted_with_manifest_and_coverage(self):
        self.write_assets()
        summary = self.run_with(consumed=10, complete=True)
        self.assertEqual(summary["dump_coverage"], 1.0)
        contract = validate_day_pair(self.out / self.DAY)
        source = contract["source"]
        self.assertEqual(source["asset_manifest"]["assets"][0]["bytes"], 10)
        self.assertEqual(source["ingestion"]["dump_coverage"], 1.0)
        self.assertTrue(source["ingestion"]["tar_complete"])

    def test_exact_coverage_boundary_is_accepted(self):
        self.write_assets()
        summary = self.run_with(consumed=9, complete=True)
        self.assertEqual(summary["dump_coverage"], 0.9)
        self.assertTrue((self.out / self.DAY / "flights.parquet").is_file())

    def test_current_valid_day_is_idempotent_without_raw_assets(self):
        self.write_assets()
        self.run_with(consumed=10, complete=True)
        for path in self.raw.iterdir():
            path.unlink()
        with patch.object(run_daily, "ROOT", self.root), \
                patch.object(run_daily, "OUT_DIR", self.out):
            summary = run_daily.run("2026.01.01", workers=1)
        self.assertTrue(summary["already_complete"])

    def test_exact_pair_guard_rejects_an_extra_file(self):
        self.write_assets()
        self.run_with(consumed=10, complete=True)
        day = self.out / self.DAY
        (day / "partial.tmp").write_text("unexpected")
        with self.assertRaisesRegex(ValueError, "extra.*partial.tmp"):
            validate_day_pair(day)

    def test_promotion_refuses_a_concurrent_final_day(self):
        stage = self.root / "stage-day"
        final = self.out / self.DAY
        stage.mkdir()
        final.mkdir()
        (stage / "new").write_text("new")
        (final / "old").write_text("old")
        with self.assertRaisesRegex(RuntimeError, "refusing to overwrite"):
            run_daily._promote_day(stage, final)
        self.assertEqual((stage / "new").read_text(), "new")
        self.assertEqual((final / "old").read_text(), "old")

    def test_invalid_existing_day_is_quarantined_not_deleted(self):
        existing = self.out / self.DAY
        existing.mkdir()
        (existing / "evidence.txt").write_text("keep me")
        with patch.object(run_daily, "OUT_DIR", self.out):
            self.assertIsNone(run_daily._quarantine_invalid_day(existing))
        self.assertFalse(existing.exists())
        preserved = list((self.root / "data/flights.quarantine").glob("*/evidence.txt"))
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].read_text(), "keep me")

    def test_quarantine_survives_a_following_ingestion_failure(self):
        existing = self.out / self.DAY
        existing.mkdir()
        (existing / "evidence.txt").write_text("keep me")
        with patch.object(run_daily, "ROOT", self.root), \
                patch.object(run_daily, "OUT_DIR", self.out), \
                self.assertRaisesRegex(SystemExit, "asset manifest missing"):
            run_daily.run("2026.01.01", workers=1)
        preserved = list((self.root / "data/flights.quarantine").glob("*/evidence.txt"))
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].read_text(), "keep me")


if __name__ == "__main__":
    unittest.main()
