#!/usr/bin/env python3
"""Build a release manifest from an already frozen, verified workspace.

This is a release-author operation, not part of the nightly accumulation.  It
prints progress on stderr and writes the complete JSON only after all full-file
checksums have been calculated.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT))

import track_quality  # noqa: E402
from release_manifest import role_paths, set_checksum, sha256_file  # noqa: E402
from wind.era5 import LEVELS  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402


def file_entry(path: Path) -> dict:
    return {"kind": "file", "sha256": sha256_file(path), "bytes": path.stat().st_size}


def set_entry(root: Path, kind: str, days: list[str]) -> dict:
    print(f"hashing {root} ({len(days)} day(s))", file=sys.stderr, flush=True)
    return {"kind": kind, **set_checksum(root, role_paths(kind, days))}


def artifact_keys(root: Path, days: list[str]) -> set[tuple[str, int]]:
    out = set()
    for day in days:
        table = pq.read_table(root / f"{day}.parquet", columns=["day", "flight_id"])
        frame = table.to_pandas()
        out.update((str(d), int(fid)) for d, fid in zip(frame.day, frame.flight_id))
    return out


def missing_entries(keys: set[tuple[str, int]]) -> list[dict]:
    entries = []
    for day, fid in sorted(keys):
        reason = ("no usable speed samples; documented in KNOWN-ISSUES.md §2"
                  if (day, fid) == ("2026-06-09", 11038)
                  else "missing when the release manifest was signed; requires review")
        entries.append({"day": day, "flight_id": fid, "reason": reason})
    return entries


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--release-id", required=True)
    ap.add_argument("--days-from", type=Path, required=True)
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--code-commit", default=None)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    root = args.root.resolve()

    days = sorted(p.stem for p in args.days_from.glob("*.parquet"))
    if not days:
        raise SystemExit(f"no release days in {args.days_from}")
    # The next 00:00 field is an input whenever a flight can cross midnight.
    era5_days = sorted(set(days) | {
        (date.fromisoformat(d) + timedelta(days=1)).isoformat() for d in days
    })
    calibration_days = sorted(
        p.name for p in (root / "data/flights_ecac").iterdir() if p.is_dir())
    commit = args.code_commit or subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()

    inputs = {
        "flights": set_entry(root / "data/flights_ecac", "flight-parquet-pairs", days),
        "calibration_flights": set_entry(
            root / "data/flights_ecac", "flight-parquet-pairs", calibration_days),
        "era5": set_entry(root / "data/era5_ecac", "daily-netcdf", era5_days),
        "calibration": file_entry(root / "data/calibration_ecac.json"),
        "airports": file_entry(root / "data/airports_ecac.csv"),
        "coverage": file_entry(root / "data/coverage_ecac.json"),
        "icao_fuel_table": file_entry(root / "data/icao_fuel_table.json"),
        "anchored_cruise_ff": file_entry(root / "data/anchored_cruise_ff.json"),
    }
    artifacts = {
        "decomposition": set_entry(root / "data/decomposition_ecac", "daily-parquet", days),
        "phase": set_entry(root / "data/decomposition_ecac_phase", "daily-parquet", days),
        "ground": set_entry(root / "data/ground_share_ecac", "daily-parquet", days),
    }
    dec_keys = artifact_keys(root / "data/decomposition_ecac", days)
    phase_keys = artifact_keys(root / "data/decomposition_ecac_phase", days)
    ground_keys = artifact_keys(root / "data/ground_share_ecac", days)
    if phase_keys - dec_keys:
        raise SystemExit("phase artefact contains keys absent from decomposition")
    manifest = {
        "schema_version": 1,
        "release": {
            "id": args.release_id,
            "code_commit": commit,
            "days": days,
            "era5_days": era5_days,
            "calibration_days": calibration_days,
        },
        "configuration": {
            "geographic_box": {"lat_min": 27.0, "lat_max": 72.0,
                               "lon_min": -32.0, "lon_max": 45.0},
            "ground": {"definition": "a3000t70", "altitude_below_ft": 3000.0,
                       "tas_below_kt": 70.0},
            "era5": {"area_nwse": [72.0, -32.0, 27.0, 45.0],
                     "grid_degrees": [0.25, 0.25], "pressure_levels_hpa": LEVELS,
                     "variables": ["u", "v"], "hours_utc": list(range(24))},
            "openap_version": importlib.metadata.version("openap"),
            "track_quality": {
                "gap_threshold_s": track_quality.GAP_THRESHOLD_S,
                "coverage_min_fraction": track_quality.COV_MIN,
                "flown_min_fraction": track_quality.FLOWN_MIN_FRAC,
                "great_circle_min_km": track_quality.GC_MIN_KM,
            },
        },
        "checksum_algorithm": (
            "sha256 over sorted records relative_path NUL byte_size NUL "
            "file_sha256 LF; individual files use sha256"
        ),
        "inputs": inputs,
        "artifacts": artifacts,
        "exceptions": {
            "phase_missing_keys": missing_entries(dec_keys - phase_keys),
            "ground_missing_keys": missing_entries(dec_keys - ground_keys),
        },
    }
    args.out.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n")
    print(f"wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
