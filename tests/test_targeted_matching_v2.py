from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "lab"), str(ROOT)]

from targeted_match import (  # noqa: E402
    TargetedMatchError,
    cli,
    match_flights_v2,
    validate_design,
)


class TargetedMatchingV2Tests(unittest.TestCase):
    @staticmethod
    def design() -> dict:
        return json.loads((ROOT / "targeted-matching-v2-design.json").read_text())

    @staticmethod
    def primary() -> pd.DataFrame:
        return pd.DataFrame([{
            "sample_id": "1" * 24,
            "day": "2026-03-01",
            "typecode": "A320",
            "dep_ts": 1_772_380_000,
            "arr_ts": 1_772_383_600,
            "o_lat": 37.9364,
            "o_lon": 23.9445,
            "d_lat": 41.2753,
            "d_lon": 28.7519,
        }])

    @staticmethod
    def source(*, source_id: str = "true", typecode: str = "A320",
               dep_offset: int = 60, arr_offset: int = 60,
               d_lat: float = 41.2753, d_lon: float = 28.7519) -> dict:
        return {
            "source_id": source_id,
            "icao24": "4bb001" if source_id == "true" else "4bb002",
            "typecode": typecode,
            "dep_ts": 1_772_380_000 + dep_offset,
            "arr_ts": 1_772_383_600 + arr_offset,
            "o_lat": 37.9364,
            "o_lon": 23.9445,
            "d_lat": d_lat,
            "d_lon": d_lon,
        }

    @staticmethod
    def true_track(source_id: str = "true") -> list[dict]:
        return [
            {"source_id": source_id, "time": 1_772_380_010,
             "lat": 37.9365, "lon": 23.9446},
            {"source_id": source_id, "time": 1_772_381_800,
             "lat": 39.5, "lon": 26.1},
            {"source_id": source_id, "time": 1_772_383_590,
             "lat": 41.2752, "lon": 28.7518},
        ]

    @staticmethod
    def decoy_source() -> dict:
        # The v1 anchor score is below 8 despite a 29-minute departure offset
        # and a nearby destination.  Its source track proves it is another leg.
        return TargetedMatchingV2Tests.source(
            source_id="decoy", typecode="A320",
            dep_offset=-1740, arr_offset=-600,
            d_lat=41.0000, d_lon=29.3000)

    @staticmethod
    def decoy_track() -> list[dict]:
        return [
            {"source_id": "decoy", "time": 1_772_379_900,
             "lat": 40.2, "lon": 26.7},
            {"source_id": "decoy", "time": 1_772_380_100,
             "lat": 40.4, "lon": 27.0},
            {"source_id": "decoy", "time": 1_772_383_500,
             "lat": 40.9, "lon": 29.3},
        ]

    def test_tracked_design_is_post_pilot_and_narrowly_versioned(self):
        self.assertEqual(
            validate_design(
                self.design(), ROOT / "targeted-matching-v2-design.json"),
            {"sample_rows": 2279,
             "profile": "targeted_identity_mutual_nearest_v2"})
        broken = self.design()
        broken["claims"]["is_original_v1_preregistration"] = True
        with self.assertRaisesRegex(TargetedMatchError, "claims"):
            validate_design(broken, ROOT / "targeted-matching-v2-design.json")

    def test_true_candidate_passes_both_identity_endpoints(self):
        result = match_flights_v2(
            self.primary(), pd.DataFrame([self.source()]),
            pd.DataFrame(self.true_track()), self.design())
        row = result.iloc[0]
        self.assertEqual(row.match_status, "matched")
        self.assertEqual(row.source_id, "true")
        self.assertLess(row.identity_origin_distance_km, 0.1)
        self.assertLess(row.identity_destination_distance_km, 0.1)
        self.assertEqual(row.source_typecode, "A320")

    def test_exact_type_is_an_identity_guard_not_a_score_bonus(self):
        result = match_flights_v2(
            self.primary(), pd.DataFrame([self.source(typecode="A20N")]),
            pd.DataFrame(self.true_track()), self.design())
        self.assertEqual(result.iloc[0].match_status, "type_mismatch")

    def test_deleting_true_candidate_does_not_promote_plausible_decoy(self):
        source = pd.DataFrame([self.source(), self.decoy_source()])
        track = pd.DataFrame(self.true_track() + self.decoy_track())
        complete = match_flights_v2(self.primary(), source, track, self.design())
        self.assertEqual(complete.iloc[0].match_status, "matched")
        self.assertEqual(complete.iloc[0].source_id, "true")

        deleted = match_flights_v2(
            self.primary(), pd.DataFrame([self.decoy_source()]),
            pd.DataFrame(self.decoy_track()), self.design())
        self.assertEqual(deleted.iloc[0].match_status, "track_inconsistent")

    def test_identity_consistent_tie_remains_ambiguous(self):
        second = self.source(source_id="second")
        source = pd.DataFrame([self.source(), second])
        track = pd.DataFrame(self.true_track() + self.true_track("second"))
        result = match_flights_v2(self.primary(), source, track, self.design())
        self.assertEqual(result.iloc[0].match_status, "ambiguous_primary")

    def test_missing_exact_type_track_is_not_called_inconsistent(self):
        missing = self.source(source_id="missing")
        unrelated = self.source(source_id="unrelated", typecode="B738")
        result = match_flights_v2(
            self.primary(), pd.DataFrame([missing, unrelated]),
            pd.DataFrame(self.true_track("unrelated")), self.design())
        self.assertEqual(result.iloc[0].match_status, "track_missing")

    def test_blinding_and_source_contract_reject_extra_fields(self):
        primary = self.primary()
        primary["failure_mask"] = "0100"
        with self.assertRaisesRegex(TargetedMatchError, "columns differ"):
            match_flights_v2(
                primary, pd.DataFrame([self.source()]),
                pd.DataFrame(self.true_track()), self.design())

        track = pd.DataFrame(self.true_track())
        track["altitude"] = 30000
        with self.assertRaisesRegex(TargetedMatchError, "undeclared altitude"):
            match_flights_v2(
                self.primary(), pd.DataFrame([self.source()]), track, self.design())

        outside = pd.DataFrame(self.true_track())
        outside.loc[0, "time"] = self.source()["dep_ts"] - 2701
        with self.assertRaisesRegex(TargetedMatchError, "candidate padding"):
            match_flights_v2(
                self.primary(), pd.DataFrame([self.source()]),
                outside, self.design())

    def test_cli_writes_private_closed_output(self):
        with tempfile.TemporaryDirectory(prefix="co2gap-targeted-v2-") as raw:
            root = Path(raw)
            primary_path = root / "primary.json"
            source_path = root / "source.csv"
            track_path = root / "track.parquet"
            output = root / "matches.json"
            primary_path.write_text(json.dumps({
                "rows": self.primary().to_dict(orient="records")}, sort_keys=True))
            pd.DataFrame([self.source()]).to_csv(source_path, index=False)
            pd.DataFrame(self.true_track()).to_parquet(track_path, index=False)
            status = cli([
                "--design", str(ROOT / "targeted-matching-v2-design.json"),
                "--primary", str(primary_path),
                "--source-flights", str(source_path),
                "--source-track", str(track_path),
                "--out", str(output),
            ])
            self.assertEqual(status, 0)
            value = json.loads(output.read_text())
            self.assertEqual(value["status_counts"], {"matched": 1})
            self.assertEqual(value["rows"][0]["source_id"], "true")
            self.assertEqual(len(value["input_sha256"]), 3)

            with self.assertRaisesRegex(TargetedMatchError, "outside the repository"):
                from targeted_match import run
                run(
                    design_path=ROOT / "targeted-matching-v2-design.json",
                    primary_path=primary_path, source_path=source_path,
                    track_path=track_path, output=ROOT / "private-matches.json")

    def test_design_hash_references_are_guarded(self):
        broken = copy.deepcopy(self.design())
        broken["supersedes"]["design_sha256"] = "0" * 64
        with self.assertRaisesRegex(TargetedMatchError, "v1 design differs"):
            validate_design(broken, ROOT / "targeted-matching-v2-design.json")


if __name__ == "__main__":
    unittest.main()
