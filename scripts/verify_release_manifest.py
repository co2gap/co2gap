#!/usr/bin/env python3
"""Verify immutable release inputs and, optionally, frozen outputs."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

import track_quality  # noqa: E402
from release_manifest import ReleaseManifest  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest", type=Path)
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--include-artifacts", action="store_true")
    args = ap.parse_args()
    root = args.root.resolve()
    m = ReleaseManifest.load(args.manifest)
    m.verify_track_quality(track_quality)
    for role, rel in (("calibration", "data/calibration_ecac.json"),
                      ("airports", "data/airports_ecac.csv"),
                      ("coverage", "data/coverage_ecac.json"),
                      ("icao_fuel_table", "data/icao_fuel_table.json"),
                      ("anchored_cruise_ff", "data/anchored_cruise_ff.json")):
        m.verify_file(role, root / rel)
        print(f"OK {role}")
    for role, rel in (("flights", "data/flights_ecac"),
                      ("calibration_flights", "data/flights_ecac"),
                      ("era5", "data/era5_ecac")):
        m.verify_set(role, root / rel)
        print(f"OK {role}")
    if args.include_artifacts:
        for role, rel in (("decomposition", "data/decomposition_ecac"),
                          ("phase", "data/decomposition_ecac_phase"),
                          ("ground", "data/ground_share_ecac")):
            m.require_exact_output_days(root / rel, role)
            m.verify_set(role, root / rel, artifact=True)
            print(f"OK {role}")
    print(f"release {m.release_id}: verified")


if __name__ == "__main__":
    main()
