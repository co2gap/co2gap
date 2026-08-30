import copy
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from lab.opensky_day_audit import (
    PRIMARY_MATCH_COLUMNS,
    UncertaintyError,
    extract_state_vectors,
    match_flights,
    normalize_source_flights,
    partition_state_vectors,
    select_state_vector_objects,
    source_track_quality,
    validate_design,
    validate_result,
)


ROOT = Path(__file__).resolve().parents[1]


def _design():
    return json.loads((ROOT / "opensky-day-audit-design.json").read_text())


def _primary(**updates):
    row = {
        "opaque_primary_id": "primary-a",
        "dep_ts": 1_000, "arr_ts": 3_000,
        "o_lat": 45.0, "o_lon": 7.0,
        "d_lat": 45.0, "d_lon": 10.0,
    }
    row.update(updates)
    return row


def _source(**updates):
    row = {
        "source_id": "source-a", "icao24": "abcdef",
        "dep_ts": 1_010, "arr_ts": 3_010,
        "o_lat": 45.01, "o_lon": 7.01,
        "d_lat": 45.01, "d_lon": 10.01,
    }
    row.update(updates)
    return row


class OpenSkyDesignTests(unittest.TestCase):
    def test_registered_design_validates(self):
        result = validate_design(_design(), ROOT / "release-manifest.json")
        self.assertEqual(result["day"], "2026-03-01")
        self.assertEqual(result["population_rows"], 10_290)

    def test_non_closing_partition_is_rejected(self):
        broken = _design()
        broken["by_failure_mask"][0]["population_rows"] -= 1
        with self.assertRaisesRegex(UncertaintyError, "do not close"):
            validate_design(broken, ROOT / "release-manifest.json")

    def test_matching_configuration_is_positive(self):
        broken = _design()
        broken["matching"]["maximum_score"] = 0
        with self.assertRaisesRegex(UncertaintyError, "maximum_score"):
            validate_design(broken, ROOT / "release-manifest.json")

    def test_tracked_result_closes_on_design(self):
        result = json.loads((ROOT / "opensky-day-audit-result.json").read_text())
        measured = validate_result(
            result, _design(), ROOT / "opensky-day-audit-design.json")
        self.assertEqual(measured["matches"], 8_726)
        self.assertEqual(measured["pass_outcomes"], 7_615)
        self.assertEqual(measured["reject_outcomes"], 829)

    def test_non_closing_result_is_rejected(self):
        result = json.loads((ROOT / "opensky-day-audit-result.json").read_text())
        result["by_failure_mask"][0]["matching"]["matched"] -= 1
        with self.assertRaisesRegex(UncertaintyError, "matching does not close"):
            validate_result(
                result, _design(), ROOT / "opensky-day-audit-design.json")


class OpenSkyNormalisationTests(unittest.TestCase):
    def test_takeoff_landing_info_precedes_fallback(self):
        frame = pd.DataFrame([{
            "icao24": "ABCDEF", "firstSeen": 900, "lastSeen": 3_100,
            "track": [
                {"time": 900, "latitude": 44.0, "longitude": 6.0},
                {"time": 3_100, "latitude": 46.0, "longitude": 11.0},
            ],
            "takeoffLandingInfo": {
                "takeoffTime": 1_000,
                "takeoffLatitude": 45.0,
                "takeoffLongitude": 7.0,
                "landingTime": 3_000,
                "landingLatitude": 45.0,
                "landingLongitude": 10.0,
            },
        }])
        result = normalize_source_flights([frame]).iloc[0]
        self.assertEqual(result.dep_ts, 1_000)
        self.assertEqual(result.arr_ts, 3_000)
        self.assertEqual(result.o_lat, 45.0)
        self.assertEqual(result.d_lon, 10.0)

    def test_incomplete_info_falls_back_to_flight_times_and_track_ends(self):
        frame = pd.DataFrame([{
            "icao24": "abcdef", "firstSeen": 900, "lastSeen": 3_100,
            "track": [
                {"time": 3_100, "latitude": 46.0, "longitude": 11.0},
                {"time": 900, "latitude": 44.0, "longitude": 6.0},
            ],
            "takeoffLandingInfo": {
                "takeoffTime": 1_000, "takeoffLatitude": None,
                "takeoffLongitude": None, "landingTime": None,
                "landingLatitude": None, "landingLongitude": None,
            },
        }])
        result = normalize_source_flights([frame]).iloc[0]
        self.assertEqual((result.dep_ts, result.arr_ts), (900, 3_100))
        self.assertEqual((result.o_lat, result.o_lon), (44.0, 6.0))
        self.assertEqual((result.d_lat, result.d_lon), (46.0, 11.0))


class OpenSkyMatchingTests(unittest.TestCase):
    def test_unique_mutual_nearest_match_is_accepted(self):
        primary = pd.DataFrame([_primary()])
        source = pd.DataFrame([_source()])
        result = match_flights(primary, source, _design()).iloc[0]
        self.assertEqual(result.match_status, "matched")
        self.assertEqual(result.source_id, "source-a")

    def test_primary_gate_information_is_rejected_before_matching(self):
        primary = pd.DataFrame([{**_primary(), "failure_mask": "0000"}])
        with self.assertRaisesRegex(UncertaintyError, "prohibited"):
            match_flights(primary, pd.DataFrame([_source()]), _design())

    def test_close_runner_up_is_ambiguous(self):
        primary = pd.DataFrame([_primary()])
        source = pd.DataFrame([
            _source(source_id="source-a"),
            _source(source_id="source-b", icao24="abcdee", dep_ts=1_011, arr_ts=3_011),
        ])
        result = match_flights(primary, source, _design()).iloc[0]
        self.assertEqual(result.match_status, "ambiguous_primary")

    def test_one_source_cannot_match_two_primary_rows(self):
        primary = pd.DataFrame([
            _primary(opaque_primary_id="primary-a"),
            _primary(opaque_primary_id="primary-b", dep_ts=1_500, arr_ts=3_500),
        ], columns=list(PRIMARY_MATCH_COLUMNS))
        result = match_flights(primary, pd.DataFrame([_source()]), _design())
        self.assertEqual((result.match_status == "matched").sum(), 1)


class OpenSkySourceQualityTests(unittest.TestCase):
    def _points(self):
        times = np.arange(1_000, 2_201, 120, dtype=float)
        return pd.DataFrame({
            "time": times + 5,
            "lastPosUpdate": times,
            "icao24": ["abcdef"] * len(times),
            "lat": np.full(len(times), 45.0),
            "lon": np.linspace(7.0, 10.0, len(times)),
            "velocity": np.full(len(times), 220.0),
            "baroAltitude": np.linspace(0.0, 10_000.0, len(times)),
            "geoAltitude": np.linspace(0.0, 10_000.0, len(times)),
            "vertRate": np.zeros(len(times)),
            "onGround": np.zeros(len(times), dtype=bool),
        })

    def _source_row(self):
        return {
            "source_dep_ts": 1_000, "source_arr_ts": 2_200,
            "source_o_lat": 45.0, "source_o_lon": 7.0,
            "source_d_lat": 45.0, "source_d_lon": 10.0,
        }

    def test_good_track_passes_and_units_are_converted(self):
        model, quality = source_track_quality(
            self._points(), self._source_row(), _design())
        self.assertTrue(quality["quality_pass"])
        self.assertEqual(quality["unique_points"], 11)
        self.assertAlmostEqual(model.gs_kt.iloc[0], 427.6458, places=3)
        self.assertAlmostEqual(model.alt_ft.iloc[-1], 32_808.39895, places=3)

    def test_long_gap_guardian_fires(self):
        points = self._points().drop(index=range(2, 9)).reset_index(drop=True)
        _, quality = source_track_quality(points, self._source_row(), _design())
        self.assertFalse(quality["quality_pass"])
        self.assertIn("coverage_below_threshold", quality["failure_reasons"])

    def test_stale_duplicate_keeps_latest_snapshot(self):
        points = self._points()
        duplicate = points.iloc[[3]].copy()
        duplicate["time"] += 100
        duplicate["velocity"] = 230.0
        model, quality = source_track_quality(
            pd.concat([points, duplicate], ignore_index=True),
            self._source_row(), _design())
        self.assertEqual(quality["unique_points"], 11)
        row = model[model.t == points.lastPosUpdate.iloc[3]].iloc[0]
        self.assertAlmostEqual(row.gs_kt, 230.0 * 1.9438444924406048)

    def test_minute_median_rejects_a_single_coordinate_contaminant(self):
        times = np.arange(1_000, 1_661, 10, dtype=float)
        points = pd.DataFrame({
            "time": times + 1, "lastPosUpdate": times,
            "icao24": ["abcdef"] * len(times),
            "lat": np.full(len(times), 45.0),
            "lon": np.linspace(7.0, 10.0, len(times)),
            "velocity": np.full(len(times), 220.0),
            "baroAltitude": np.linspace(0.0, 10_000.0, len(times)),
            "geoAltitude": np.linspace(0.0, 10_000.0, len(times)),
            "vertRate": np.zeros(len(times)),
            "onGround": np.zeros(len(times), dtype=bool),
        })
        contaminant = points.iloc[[20]].copy()
        contaminant["time"] += 0.5
        contaminant["lastPosUpdate"] += 0.5
        contaminant["lat"] = 32.0
        contaminant["lon"] = 44.0
        model, quality = source_track_quality(
            pd.concat([points, contaminant], ignore_index=True),
            {
                "source_dep_ts": 1_000, "source_arr_ts": 1_660,
                "source_o_lat": 45.0, "source_o_lon": 7.0,
                "source_d_lat": 45.0, "source_d_lon": 10.0,
            }, _design())
        self.assertTrue(quality["quality_pass"])
        self.assertLess(quality["maximum_segment_speed_kt"], 1_200)
        self.assertLess(model.lon.max(), 11.0)

    def test_streaming_extraction_keeps_only_matched_aircraft_and_window(self):
        points = self._points()
        outside = points.copy()
        outside["icao24"] = "fedcba"
        all_points = pd.concat([points, outside], ignore_index=True)
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            source = tmp / "source.parquet"
            output = tmp / "selected.parquet"
            matches = tmp / "matches.json"
            pq.write_table(pa.Table.from_pandas(all_points, preserve_index=False), source)
            matches.write_text(json.dumps({
                "schema_version": 1,
                "kind": "co2gap-opensky-day-match-result",
                "design_sha256": __import__("hashlib").sha256(
                    (ROOT / "opensky-day-audit-design.json").read_bytes()).hexdigest(),
                "rows": [{
                    "opaque_primary_id": "primary-a",
                    "match_status": "matched", "source_id": "source-a",
                    "icao24": "abcdef", "source_dep_ts": 1_000,
                    "source_arr_ts": 2_200,
                }],
            }))
            result = extract_state_vectors(
                design_path=ROOT / "opensky-day-audit-design.json",
                matches_path=matches, source_paths=[str(source)], output=output,
                batch_size=4)
            extracted = pq.read_table(output).to_pandas()
            self.assertEqual(result["rows"], 11)
            self.assertEqual(set(extracted.icao24), {"abcdef"})
            self.assertTrue(output.with_suffix(".contract.json").is_file())

            partitions = tmp / "partitions"
            contract = partition_state_vectors(
                design_path=ROOT / "opensky-day-audit-design.json",
                matches_path=matches, state_vectors_path=output,
                output_dir=partitions, buckets=4, batch_size=4)
            self.assertEqual(contract["dense_input_rows"], 11)
            self.assertEqual(contract["rows_after_batch_deduplication"], 11)
            self.assertTrue((partitions / "contract.json").is_file())

    def test_partition_refuses_to_replace_existing_output(self):
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(UncertaintyError, "refusing to replace"):
                partition_state_vectors(
                    design_path=ROOT / "opensky-day-audit-design.json",
                    matches_path=Path(raw) / "unused.json",
                    state_vectors_path=Path(raw) / "unused.parquet",
                    output_dir=Path(raw), buckets=4)

    def test_exact_state_vector_day_is_selected_from_a_wider_prefix(self):
        epoch = int(_design()["day_epoch"])
        paths = [
            f"root/hour={epoch + hour * 3600}/part.parquet"
            for hour in range(24)
        ] + [f"root/hour={epoch + 24 * 3600}/next-day.parquet"]
        selected = select_state_vector_objects(paths, _design())
        self.assertEqual(len(selected), 24)
        self.assertNotIn("next-day.parquet", "\n".join(selected))

    def test_missing_state_vector_hour_is_rejected(self):
        epoch = int(_design()["day_epoch"])
        paths = [
            f"root/hour={epoch + hour * 3600}/part.parquet"
            for hour in range(23)
        ]
        with self.assertRaisesRegex(UncertaintyError, "incomplete"):
            select_state_vector_objects(paths, _design())


if __name__ == "__main__":
    unittest.main()
