#!/usr/bin/env python3
"""Verify immutable release inputs and, optionally, frozen outputs."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

import track_quality  # noqa: E402
from release_manifest import ReleaseManifest  # noqa: E402


def report_code_commit(m, root: Path) -> None:
    """Say what the recorded commit is, and how stale it is. Never fatal.

    A manifest generated before the build names a code state that did not
    produce the artefacts. The 2026-09-01 manifest did exactly that - eleven
    commits and 269 lines of lab/site_build.py behind the tip the release is
    dated to - and nothing said so until an audit went looking. This makes the
    distance visible at verification time. It warns rather than fails, because
    a manifest is evidence about the past and refusing to read it would destroy
    its only use.
    """
    rel = m.data.get("release", {})
    commit = rel.get("code_commit")
    if not commit:
        print("!! no code_commit recorded: the code state is not identified")
        return
    captured = rel.get("code_commit_captured_at")
    clean = rel.get("code_commit_tree_clean")
    extra = []
    if captured:
        extra.append(f"captured {captured}")
    if clean is False:
        extra.append("TREE WAS DIRTY")
    elif clean is True:
        extra.append("tree clean")
    suffix = f"  ({', '.join(extra)})" if extra else \
        "  (no capture time recorded: pre-dates this field)"

    def git(*a):
        return subprocess.run(("git", "-C", str(root)) + a, capture_output=True,
                              text=True).stdout.strip()

    if git("cat-file", "-t", commit) != "commit":
        print(f"!! code_commit {commit} does not resolve in this repository{suffix}")
        return
    when = git("show", "-s", "--format=%ci", commit)
    behind = git("rev-list", "--count", f"{commit}..HEAD")
    print(f"OK code_commit {commit[:7]} ({when}){suffix}")
    if behind and behind != "0":
        print(f"!! it is {behind} commits behind HEAD. If the release was built "
              "after those commits, this field names a code state that did not "
              "produce these artefacts.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest", type=Path)
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--include-artifacts", action="store_true")
    args = ap.parse_args()
    root = args.root.resolve()
    m = ReleaseManifest.load(args.manifest)
    m.verify_track_quality(track_quality)
    report_code_commit(m, root)
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
