#!/usr/bin/env python3
"""Blinded, provider-neutral flight identity matching for targeted validation.

Version 1 is preserved in ``targeted-validation-design.json``.  This module
implements the explicitly post-pilot v2 identity protocol without receiving a
failure mask, primary quality diagnostic, release key or model outcome.

All source, match and per-flight outputs are private.  The command-line runner
therefore refuses to write its result inside the repository.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "lab"))
sys.path.insert(0, str(ROOT))

from release_manifest import sha256_file  # noqa: E402


PRIMARY_COLUMNS = (
    "sample_id", "day", "typecode", "dep_ts", "arr_ts",
    "o_lat", "o_lon", "d_lat", "d_lon",
)
SOURCE_COLUMNS = (
    "source_id", "icao24", "typecode", "dep_ts", "arr_ts",
    "o_lat", "o_lon", "d_lat", "d_lon",
)
TRACK_COLUMNS = ("source_id", "time", "lat", "lon")


class TargetedMatchError(ValueError):
    """A v2 matching contract or private input is inconsistent."""


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(Path(path).read_text())
    except Exception as exc:
        raise TargetedMatchError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TargetedMatchError(f"{path} must contain a JSON object")
    return value


def _positive(value, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TargetedMatchError(f"{label} must be a positive number") from exc
    if not math.isfinite(number) or number <= 0:
        raise TargetedMatchError(f"{label} must be a positive number")
    return number


def _require_private_output(path: Path) -> None:
    try:
        Path(path).resolve().relative_to(ROOT.resolve())
    except ValueError:
        return
    raise TargetedMatchError(
        f"per-flight v2 matches must remain outside the repository: {path}")


def _require_file_hash(base: Path, path_value, digest, label: str) -> Path:
    if not isinstance(path_value, str) or not path_value.strip():
        raise TargetedMatchError(f"v2 design lacks {label} path")
    path = base / path_value
    if (not path.is_file() or not isinstance(digest, str)
            or len(digest) != 64 or set(digest) - set("0123456789abcdef")
            or sha256_file(path) != digest):
        raise TargetedMatchError(f"v2 design {label} differs from its hash")
    return path


def validate_design(design: dict, design_path: Path) -> dict:
    """Validate chronology, immutable v1 references and exact v2 rules."""
    if (design.get("schema_version"), design.get("kind")) != (
            1, "co2gap-targeted-matching-v2-design"):
        raise TargetedMatchError("unknown targeted matching v2 design")
    if design.get("publication_status") != "aggregate_preregistration":
        raise TargetedMatchError("v2 design must remain an aggregate preregistration")
    if design.get("analysis_status") != (
            "pilot_informed_protocol_frozen_before_requested_source_outcomes"):
        raise TargetedMatchError("v2 chronology is not frozen honestly")

    base = Path(design_path).resolve().parent
    old = design.get("supersedes")
    if (not isinstance(old, dict) or old.get("component") != "matching_only"
            or old.get("v1_files_unchanged") is not True
            or old.get("v1_outcomes_must_not_be_relabelled_v2") is not True):
        raise TargetedMatchError("v2 must preserve and narrowly supersede v1")
    old_design_path = _require_file_hash(
        base, old.get("design_path"), old.get("design_sha256"), "v1 design")
    old_registration_path = _require_file_hash(
        base, old.get("registration_path"), old.get("registration_sha256"),
        "v1 registration")
    old_design = _load_json(old_design_path)
    old_registration = _load_json(old_registration_path)
    if old_design.get("matching", {}).get("required_profile") != (
            "targeted_mutual_nearest_v1"):
        raise TargetedMatchError("v2 does not reference the expected v1 profile")

    sample = design.get("sample_contract")
    if (not isinstance(sample, dict)
            or sample.get("sample_rows") != old_registration.get("sample_rows")
            or sample.get("private_sample_sha256")
            != old_registration.get("private_artifact_sha256", {}).get("sample")
            or sample.get("blinded_match_list_sha256")
            != old_registration.get("private_artifact_sha256", {}).get("match_list")
            or sample.get("allocation_unchanged") is not True
            or sample.get("minimum_support_unchanged") is not True):
        raise TargetedMatchError("v2 changes or misidentifies the frozen sample")

    chronology = design.get("chronology")
    if (not isinstance(chronology, dict)
            or chronology.get("source_emissions_outcomes_computed") is not False
            or chronology.get("matching_received_gate_status") is not False
            or chronology.get("matching_received_primary_track_quality") is not False
            or int(chronology.get("pilot_rows_in_frozen_tranche", 0)) <= 0
            or int(chronology.get("pilot_rows_on_adversarial_day", 0)) <= 0):
        raise TargetedMatchError("v2 pilot chronology is incomplete")

    blind = design.get("blinding")
    if (not isinstance(blind, dict)
            or blind.get("primary_matching_fields") != list(PRIMARY_COLUMNS)
            or not isinstance(blind.get("prohibited_primary_fields"), list)
            or len(blind["prohibited_primary_fields"])
            != len(set(blind["prohibited_primary_fields"]))
            or set(PRIMARY_COLUMNS).intersection(blind["prohibited_primary_fields"])
            or "failure_mask" not in blind["prohibited_primary_fields"]
            or blind.get("failure_mask_joined_after_matching_and_source_quality")
            is not True):
        raise TargetedMatchError("v2 blinding contract is invalid")

    source = design.get("source_contract")
    if (not isinstance(source, dict)
            or source.get("flight_columns") != list(SOURCE_COLUMNS)
            or source.get("track_columns") != list(TRACK_COLUMNS)
            or source.get("provider_flight_key_required") is not True
            or source.get("source_typecode_required") is not True):
        raise TargetedMatchError("v2 source contract is invalid")

    matching = design.get("matching")
    if (not isinstance(matching, dict)
            or matching.get("required_profile")
            != "targeted_identity_mutual_nearest_v2"
            or matching.get("typecode_rule")
            != "exact_normalized_icao_designator"
            or matching.get("unknown_typecode_policy") != "reject"
            or matching.get("score") != (
                "abs(dep_delta)/900 + abs(arr_delta)/900 + origin_km/25 + "
                "destination_km/25")
            or matching.get("assignment") != (
                "one-to-one mutual nearest neighbour after type and source-track "
                "identity guards")
            or matching.get("manual_overrides") is not False
            or matching.get("candidate_deletion_test_required") is not True):
        raise TargetedMatchError("v2 matching protocol is invalid")
    for field in (
            "candidate_max_departure_delta_s",
            "candidate_max_arrival_delta_s",
            "candidate_max_origin_distance_km",
            "candidate_max_destination_distance_km",
            "track_endpoint_time_window_s",
            "track_endpoint_max_distance_km",
            "maximum_score", "runner_up_max_ratio", "tie_epsilon"):
        _positive(matching.get(field), f"matching.{field}")
    if float(matching["runner_up_max_ratio"]) >= 1:
        raise TargetedMatchError("v2 runner-up ratio must be below one")

    implementation = design.get("implementation_contract")
    if not isinstance(implementation, dict):
        raise TargetedMatchError("v2 design lacks its implementation contract")
    _require_file_hash(
        base, implementation.get("module_path"),
        implementation.get("module_sha256"), "matcher implementation")
    _require_file_hash(
        base, implementation.get("adversarial_test_path"),
        implementation.get("adversarial_test_sha256"), "adversarial tests")

    unchanged = design.get("unchanged_components")
    if (not isinstance(unchanged, dict)
            or unchanged.get("source_quality_profile")
            != old_design.get("source_quality", {}).get("required_profile")
            or unchanged.get("processing_profile")
            != old_design.get("proxy_model", {}).get("required_profile")
            or unchanged.get("minimum_measured_rows")
            != old_design.get("minimum_measured_rows")
            or unchanged.get("pool_rejected_masks") is not False):
        raise TargetedMatchError("v2 changes a component outside matching")
    claims = design.get("claims")
    if (not isinstance(claims, dict)
            or claims.get("is_original_v1_preregistration") is not False
            or claims.get("is_post_pilot") is not True
            or any(claims.get(name) is not False for name in (
                "changes_sample_or_weights", "corrects_release_headline",
                "bounds_release_headline", "proves_receiver_independence",
                "proves_zero_identity_error"))):
        raise TargetedMatchError("v2 claims are invalid")
    return {
        "sample_rows": int(sample["sample_rows"]),
        "profile": str(matching["required_profile"]),
    }


def _haversine_km(lat1, lon1, lat2, lon2):
    lat1 = np.asarray(lat1, dtype=float)
    lon1 = np.asarray(lon1, dtype=float)
    lat2 = np.asarray(lat2, dtype=float)
    lon2 = np.asarray(lon2, dtype=float)
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = p2 - p1
    dlambda = np.radians(lon2 - lon1)
    a = (np.sin(dphi / 2.0) ** 2
         + np.cos(p1) * np.cos(p2) * np.sin(dlambda / 2.0) ** 2)
    return 2.0 * 6371.0088 * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def _normal_type(value) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip().upper()


def _require_exact_columns(frame: pd.DataFrame, required: tuple[str, ...],
                           label: str) -> None:
    missing = sorted(set(required) - set(frame.columns))
    extra = sorted(set(frame.columns) - set(required))
    if missing or extra:
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("undeclared " + ", ".join(extra))
        raise TargetedMatchError(f"{label} columns differ: {'; '.join(detail)}")


def _normalise_inputs(primary: pd.DataFrame, source: pd.DataFrame,
                      track: pd.DataFrame, design: dict):
    _require_exact_columns(primary, PRIMARY_COLUMNS, "blinded primary")
    _require_exact_columns(source, SOURCE_COLUMNS, "source flights")
    _require_exact_columns(track, TRACK_COLUMNS, "source track")
    prohibited = set(design["blinding"]["prohibited_primary_fields"])
    disclosed = sorted(prohibited.intersection(primary.columns))
    if disclosed:
        raise TargetedMatchError(
            "matching received prohibited primary fields: " + ", ".join(disclosed))
    if primary.empty or source.empty or track.empty:
        raise TargetedMatchError("v2 matching inputs must be non-empty")

    primary = primary.copy()
    source = source.copy()
    track = track.copy()
    if (primary.sample_id.isna().any() or source.source_id.isna().any()
            or track.source_id.isna().any()):
        raise TargetedMatchError("v2 matching identifiers cannot be null")
    primary["sample_id"] = primary.sample_id.astype(str)
    source["source_id"] = source.source_id.astype(str)
    track["source_id"] = track.source_id.astype(str)
    if primary.sample_id.duplicated().any() or source.source_id.duplicated().any():
        raise TargetedMatchError("v2 matching identifiers must be unique")
    if any(len(value) != 24 or set(value) - set("0123456789abcdef")
           for value in primary.sample_id):
        raise TargetedMatchError("blinded primary sample ids are invalid")
    if any(not value.strip() for value in source.source_id):
        raise TargetedMatchError("source ids must be non-empty")
    icao = source.icao24.astype(str).str.strip().str.lower()
    if any(len(value) != 6 or set(value) - set("0123456789abcdef")
           for value in icao):
        raise TargetedMatchError("source icao24 values are invalid")
    source["icao24"] = icao

    primary["typecode"] = primary.typecode.map(_normal_type)
    source["typecode"] = source.typecode.map(_normal_type)
    if (primary.typecode == "").any() or (source.typecode == "").any():
        raise TargetedMatchError("v2 rejects missing aircraft typecodes")
    for frame, names, label in (
            (primary, ("dep_ts", "arr_ts", "o_lat", "o_lon", "d_lat", "d_lon"),
             "blinded primary"),
            (source, ("dep_ts", "arr_ts", "o_lat", "o_lon", "d_lat", "d_lon"),
             "source flights"),
            (track, ("time", "lat", "lon"), "source track")):
        for name in names:
            frame[name] = pd.to_numeric(frame[name], errors="coerce")
        if not np.isfinite(frame[list(names)].to_numpy(float)).all():
            raise TargetedMatchError(f"{label} has non-finite matching values")
    if ((primary.arr_ts <= primary.dep_ts).any()
            or (source.arr_ts <= source.dep_ts).any()):
        raise TargetedMatchError("v2 matching flights must have positive duration")
    for frame, prefix in ((primary, ""), (source, "")):
        if (not frame.o_lat.between(-90, 90).all()
                or not frame.d_lat.between(-90, 90).all()
                or not frame.o_lon.between(-180, 180).all()
                or not frame.d_lon.between(-180, 180).all()):
            raise TargetedMatchError("v2 matching endpoint coordinates are invalid")
    if (not track.lat.between(-90, 90).all()
            or not track.lon.between(-180, 180).all()):
        raise TargetedMatchError("source track coordinates are invalid")
    unknown = sorted(set(track.source_id) - set(source.source_id))
    if unknown:
        raise TargetedMatchError("source track names an unknown source flight")
    bounds = track[["source_id", "time"]].merge(
        source[["source_id", "dep_ts", "arr_ts"]],
        on="source_id", how="left", validate="many_to_one")
    before = float(design["matching"]["candidate_max_departure_delta_s"])
    after = float(design["matching"]["candidate_max_arrival_delta_s"])
    if ((bounds.time < bounds.dep_ts - before).any()
            or (bounds.time > bounds.arr_ts + after).any()):
        raise TargetedMatchError(
            "source track exceeds its provider flight and candidate padding")
    track = track.sort_values(["source_id", "time", "lat", "lon"]).drop_duplicates(
        ["source_id", "time", "lat", "lon"], keep="last")
    return primary.reset_index(drop=True), source.reset_index(drop=True), track


def _endpoint_guard(points: pd.DataFrame, *, timestamp: float, lat: float,
                    lon: float, window_s: float) -> tuple[float, float] | None:
    selected = points.loc[(points.time - timestamp).abs() <= window_s]
    if selected.empty:
        return None
    distances = _haversine_km(
        lat, lon, selected.lat.to_numpy(float), selected.lon.to_numpy(float))
    times = np.abs(selected.time.to_numpy(float) - timestamp)
    order = np.lexsort((times, distances))
    best = int(order[0])
    return float(distances[best]), float(times[best])


def match_flights_v2(primary: pd.DataFrame, source: pd.DataFrame,
                     track: pd.DataFrame, design: dict) -> pd.DataFrame:
    """Match blinded flights after type and two-ended source-track guards."""
    primary, source, track = _normalise_inputs(primary, source, track, design)
    config = design["matching"]
    dep_limit = float(config["candidate_max_departure_delta_s"])
    arr_limit = float(config["candidate_max_arrival_delta_s"])
    o_limit = float(config["candidate_max_origin_distance_km"])
    d_limit = float(config["candidate_max_destination_distance_km"])
    endpoint_window = float(config["track_endpoint_time_window_s"])
    endpoint_limit = float(config["track_endpoint_max_distance_km"])
    maximum_score = float(config["maximum_score"])
    ratio = float(config["runner_up_max_ratio"])
    epsilon = float(config["tie_epsilon"])

    grouped_track = {
        str(source_id): values.sort_values("time").reset_index(drop=True)
        for source_id, values in track.groupby("source_id", sort=False)
    }
    order = np.argsort(source.dep_ts.to_numpy(float), kind="stable")
    dep_sorted = source.dep_ts.to_numpy(float)[order]
    candidates = []
    anchor_counts = Counter()
    type_counts = Counter()
    track_counts = Counter()
    identity_counts = Counter()
    score_counts = Counter()

    for pi, row in enumerate(primary.itertuples(index=False)):
        lo = bisect.bisect_left(dep_sorted, float(row.dep_ts) - dep_limit)
        hi = bisect.bisect_right(dep_sorted, float(row.dep_ts) + dep_limit)
        indices = order[lo:hi]
        if len(indices) == 0:
            continue
        possible = source.iloc[indices]
        dep_delta = np.abs(possible.dep_ts.to_numpy(float) - float(row.dep_ts))
        arr_delta = np.abs(possible.arr_ts.to_numpy(float) - float(row.arr_ts))
        origin = _haversine_km(
            float(row.o_lat), float(row.o_lon),
            possible.o_lat.to_numpy(float), possible.o_lon.to_numpy(float))
        destination = _haversine_km(
            float(row.d_lat), float(row.d_lon),
            possible.d_lat.to_numpy(float), possible.d_lon.to_numpy(float))
        anchor_ok = ((arr_delta <= arr_limit) & (origin <= o_limit)
                     & (destination <= d_limit))
        for local in np.flatnonzero(anchor_ok):
            si = int(indices[local])
            candidate = source.iloc[si]
            anchor_counts[pi] += 1
            if str(candidate.typecode) != str(row.typecode):
                continue
            type_counts[pi] += 1
            points = grouped_track.get(str(candidate.source_id))
            if points is None:
                continue
            track_counts[pi] += 1
            origin_guard = _endpoint_guard(
                points, timestamp=float(row.dep_ts), lat=float(row.o_lat),
                lon=float(row.o_lon), window_s=endpoint_window)
            destination_guard = _endpoint_guard(
                points, timestamp=float(row.arr_ts), lat=float(row.d_lat),
                lon=float(row.d_lon), window_s=endpoint_window)
            if (origin_guard is None or destination_guard is None
                    or origin_guard[0] > endpoint_limit
                    or destination_guard[0] > endpoint_limit):
                continue
            identity_counts[pi] += 1
            score = (
                dep_delta[local] / 900.0 + arr_delta[local] / 900.0
                + origin[local] / 25.0 + destination[local] / 25.0)
            if score > maximum_score + epsilon:
                continue
            score_counts[pi] += 1
            candidates.append({
                "pi": int(pi), "si": int(si), "score": float(score),
                "departure_time_delta_s": float(dep_delta[local]),
                "arrival_time_delta_s": float(arr_delta[local]),
                "origin_distance_km": float(origin[local]),
                "destination_distance_km": float(destination[local]),
                "identity_origin_distance_km": float(origin_guard[0]),
                "identity_origin_time_delta_s": float(origin_guard[1]),
                "identity_destination_distance_km": float(destination_guard[0]),
                "identity_destination_time_delta_s": float(destination_guard[1]),
            })

    cand = pd.DataFrame(candidates)
    by_primary = defaultdict(list)
    by_source = defaultdict(list)
    if not cand.empty:
        for index, value in cand.iterrows():
            by_primary[int(value.pi)].append(
                (float(value.score), int(value.si), int(index)))
            by_source[int(value.si)].append(
                (float(value.score), int(value.pi), int(index)))
        for values in by_primary.values():
            values.sort()
        for values in by_source.values():
            values.sort()

    def margin(values: list[tuple[float, int, int]]) -> bool:
        if len(values) <= 1:
            return True
        best, second = values[0][0], values[1][0]
        return best + epsilon < second and best <= ratio * second + epsilon

    accepted = {}
    for pi, values in by_primary.items():
        best = values[0]
        si = best[1]
        source_values = by_source[si]
        if source_values[0][1] != pi:
            continue
        if margin(values) and margin(source_values):
            accepted[pi] = (best[2], values, source_values)

    output = []
    for pi, row in enumerate(primary.itertuples(index=False)):
        base = {"sample_id": str(row.sample_id)}
        if pi in accepted:
            ci, primary_values, source_values = accepted[pi]
            selected = cand.loc[ci]
            source_row = source.iloc[int(selected.si)]
            output.append({
                **base,
                "match_status": "matched",
                "source_id": str(source_row.source_id),
                "icao24": str(source_row.icao24),
                "source_typecode": str(source_row.typecode),
                "candidate_count_primary": int(len(primary_values)),
                "candidate_count_source": int(len(source_values)),
                "primary_runner_up_score": (
                    float(primary_values[1][0]) if len(primary_values) > 1 else None),
                "source_runner_up_score": (
                    float(source_values[1][0]) if len(source_values) > 1 else None),
                **{
                    name: float(selected[name])
                    for name in (
                        "score", "departure_time_delta_s",
                        "arrival_time_delta_s", "origin_distance_km",
                        "destination_distance_km",
                        "identity_origin_distance_km",
                        "identity_origin_time_delta_s",
                        "identity_destination_distance_km",
                        "identity_destination_time_delta_s",
                    )
                },
            })
            continue

        values = by_primary.get(pi, [])
        if not anchor_counts[pi]:
            status = "no_candidate"
        elif not type_counts[pi]:
            status = "type_mismatch"
        elif not track_counts[pi]:
            status = "track_missing"
        elif not identity_counts[pi]:
            status = "track_inconsistent"
        elif not score_counts[pi]:
            status = "score_exceeded"
        elif not margin(values):
            status = "ambiguous_primary"
        else:
            si = values[0][1]
            source_values = by_source[si]
            status = (
                "ambiguous_source"
                if source_values[0][1] == pi and not margin(source_values)
                else "not_mutual_nearest")
        output.append({**base, "match_status": status})

    result = pd.DataFrame(output)
    if len(result) != len(primary) or result.sample_id.duplicated().any():
        raise TargetedMatchError("v2 matching result does not close")
    matched = result[result.match_status == "matched"]
    if not matched.empty and matched.source_id.duplicated().any():
        raise TargetedMatchError("v2 matching result is not one-to-one")
    return result


def _read_frame(path: Path) -> pd.DataFrame:
    suffix = Path(path).suffix.lower()
    if suffix == ".json":
        try:
            value = json.loads(Path(path).read_text())
        except Exception as exc:
            raise TargetedMatchError(f"cannot read JSON table {path}: {exc}") from exc
        rows = value.get("rows") if isinstance(value, dict) else value
        if not isinstance(rows, list):
            raise TargetedMatchError(f"JSON table {path} has no rows")
        return pd.DataFrame(rows)
    if suffix in {".parquet", ".pq"}:
        return pd.read_parquet(path)
    if suffix == ".csv":
        # Source/provider identifiers are opaque strings.  Letting pandas infer
        # types turns legitimate values such as ``true`` into booleans and can
        # silently disconnect a flight from its track.
        return pd.read_csv(path, dtype=str)
    raise TargetedMatchError(f"unsupported private table format: {path}")


def _json_records(frame: pd.DataFrame) -> list[dict]:
    integer_fields = {"candidate_count_primary", "candidate_count_source"}
    records = []
    for raw in frame.to_dict(orient="records"):
        record = {}
        for key, value in raw.items():
            if value is None or pd.isna(value):
                record[key] = None
            elif key in integer_fields:
                record[key] = int(value)
            else:
                record[key] = value
        records.append(record)
    return records


def _atomic_json(path: Path, value: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def run(*, design_path: Path, primary_path: Path, source_path: Path,
        track_path: Path, output: Path) -> dict:
    _require_private_output(output)
    design = _load_json(design_path)
    validate_design(design, design_path)
    result = match_flights_v2(
        _read_frame(primary_path), _read_frame(source_path),
        _read_frame(track_path), design)
    counts = {
        str(status): int(count)
        for status, count in result.match_status.value_counts().sort_index().items()
    }
    value = {
        "schema_version": 1,
        "kind": "co2gap-targeted-private-matches-v2",
        "publication_status": "private_per_flight",
        "design_sha256": sha256_file(design_path),
        "input_sha256": {
            "primary": sha256_file(primary_path),
            "source_flights": sha256_file(source_path),
            "source_track": sha256_file(track_path),
        },
        "matching_profile": design["matching"]["required_profile"],
        "status_counts": counts,
        "rows": _json_records(result),
    }
    _atomic_json(output, value)
    return value


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--design", type=Path, default=ROOT / "targeted-matching-v2-design.json")
    parser.add_argument("--primary", type=Path, required=True)
    parser.add_argument("--source-flights", type=Path, required=True)
    parser.add_argument("--source-track", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        value = run(
            design_path=args.design, primary_path=args.primary,
            source_path=args.source_flights, track_path=args.source_track,
            output=args.out)
    except (TargetedMatchError, OSError, ValueError) as exc:
        print(f"targeted matching v2: {exc}", file=sys.stderr)
        return 1
    print(
        f"targeted matching v2: {sum(value['status_counts'].values()):,} rows; "
        + ", ".join(
            f"{name}={count}" for name, count in value["status_counts"].items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
