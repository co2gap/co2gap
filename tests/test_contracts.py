from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

from release_manifest import ManifestError, ReleaseManifest  # noqa: E402
from store import (SOURCE_CONTRACT_KEY, DayWriter,  # noqa: E402
                   _validate_ingestion_source, validate_day_pair)


class ParquetContractTests(unittest.TestCase):
    @staticmethod
    def source():
        tag = "v2026.01.01-planes-readsb-prod-0"
        line = f"{tag}.tar.aa\t10\thttps://example.invalid/aa\n"
        return {
            "dump_tag": tag,
            "asset_manifest": {
                "schema_version": 1,
                "file": f"{tag}.assets.tsv",
                "sha256": hashlib.sha256(line.encode()).hexdigest(),
                "assets": [{"name": f"{tag}.tar.aa", "bytes": 10,
                            "url": "https://example.invalid/aa"}],
            },
            "ingestion": {
                "dump_bytes": 10, "bytes_consumed": 10,
                "dump_coverage": 1.0, "minimum_dump_coverage": 0.9,
                "tar_complete": True,
            },
        }

    def test_readable_but_partial_day_is_not_complete(self):
        with tempfile.TemporaryDirectory(prefix="co2gap-parquet-") as raw:
            root = Path(raw)
            writer = DayWriter(root, "2026-01-01", source=self.source())
            point = SimpleNamespace(
                t=1.0, lat=45.0, lon=9.0, alt=1000.0,
                gs=200.0, ias=190.0, vs_rep=0.0)
            for _ in range(2):
                writer.add({"day": "2026-01-01"}, [point])
            writer.flush()
            flights = root / "2026-01-01/flights.parquet"
            contract = pq.read_metadata(flights).metadata[SOURCE_CONTRACT_KEY]
            truncated = pq.read_table(flights).slice(0, 1)
            truncated = truncated.replace_schema_metadata({SOURCE_CONTRACT_KEY: contract})
            pq.write_table(truncated, flights)
            self.assertEqual(pq.read_metadata(flights).num_rows, 1)
            with self.assertRaisesRegex(ValueError, "row count differs"):
                validate_day_pair(flights.parent)

    def test_source_contract_rejects_forged_manifest_and_lower_threshold(self):
        forged = self.source()
        forged["asset_manifest"]["assets"][0]["bytes"] = 9
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            _validate_ingestion_source(forged)

        weak = self.source()
        weak["ingestion"].update({
            "bytes_consumed": 1,
            "dump_coverage": 0.1,
            "minimum_dump_coverage": 0.1,
        })
        with self.assertRaisesRegex(ValueError, "threshold differs"):
            _validate_ingestion_source(weak)


class ReleasePerimeterTests(unittest.TestCase):
    def manifest(self, path: Path) -> ReleaseManifest:
        return ReleaseManifest(path, {
            "schema_version": 1,
            "release": {"id": "test", "days": ["2026-01-01"]},
            "artifacts": {"phase": {"kind": "daily-parquet"}},
        })

    def test_missing_phase_day_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="co2gap-phase-") as raw:
            root = Path(raw)
            with self.assertRaisesRegex(ManifestError, "1 missing"):
                self.manifest(root / "manifest.json").require_exact_output_days(
                    root, "phase")

    def test_extra_output_day_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="co2gap-perimeter-") as raw:
            root = Path(raw)
            (root / "2026-01-01.parquet").touch()
            (root / "2026-01-02.parquet").touch()
            with self.assertRaisesRegex(ManifestError, "1 day.*outside release"):
                self.manifest(root / "manifest.json").require_no_extra_output_days(
                    root, "phase")


if __name__ == "__main__":
    unittest.main()
