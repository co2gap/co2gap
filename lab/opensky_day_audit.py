#!/usr/bin/env python3
"""Blinded one-day OpenSky audit of the release quality-gate selection.

The public design is frozen in ``opensky-day-audit-design.json``.  All files
containing a flight key, aircraft address, time, endpoint or state vector must
remain outside the repository.  The final command emits aggregate diagnostics
only; it does not alter the frozen release or claim a release-wide error bound.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
for _part in ("pipeline", "ingest", "lab", "wind"):
    sys.path.insert(0, str(ROOT / _part))
sys.path.insert(0, str(ROOT))

from release_manifest import ReleaseManifest, sha256_file  # noqa: E402
from uncertainty import (  # noqa: E402
    SELECTION_COLUMNS,
    UncertaintyError,
    _atomic_json,
    selection_flags,
)


PRIMARY_MATCH_COLUMNS = (
    "opaque_primary_id", "dep_ts", "arr_ts",
    "o_lat", "o_lon", "d_lat", "d_lon",
)
SOURCE_MATCH_COLUMNS = (
    "source_id", "icao24", "dep_ts", "arr_ts",
    "o_lat", "o_lon", "d_lat", "d_lon",
)
STATE_VECTOR_COLUMNS = (
    "time", "lastPosUpdate", "icao24", "lat", "lon", "velocity",
    "baroAltitude", "geoAltitude", "vertRate", "onGround",
)


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(Path(path).read_text())
    except Exception as exc:
        raise UncertaintyError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise UncertaintyError(f"{path} must contain a JSON object")
    return value


def _private_output(path: Path, label: str) -> None:
    resolved = Path(path).resolve()
    try:
        resolved.relative_to(ROOT.resolve())
    except ValueError:
        return
    raise UncertaintyError(
        f"{label} contains per-flight/source data and must be outside the repository: {path}")


def _finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _haversine_km(lat1, lon1, lat2, lon2):
    lat1 = np.asarray(lat1, dtype=float)
    lon1 = np.asarray(lon1, dtype=float)
    lat2 = np.asarray(lat2, dtype=float)
    lon2 = np.asarray(lon2, dtype=float)
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = p2 - p1
    dlambda = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2.0) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlambda / 2.0) ** 2
    return 2.0 * 6371.0088 * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def validate_design(design: dict, manifest_path: Path) -> dict:
    """Validate the frozen aggregate design and its release boundary."""
    if design.get("schema_version") != 1:
        raise UncertaintyError("OpenSky audit design schema_version must be 1")
    if design.get("kind") != "co2gap-opensky-day-selection-audit-design":
        raise UncertaintyError("unexpected OpenSky audit design kind")
    manifest = ReleaseManifest.load(manifest_path)
    if design.get("release_id") != manifest.release_id:
        raise UncertaintyError("OpenSky design release differs from manifest")
    if design.get("release_manifest_sha256") != sha256_file(manifest.path):
        raise UncertaintyError("OpenSky design manifest hash differs from current manifest")
    manifest.require_day(str(design.get("day")))
    total = int(design.get("population_rows", -1))
    passed = int(design.get("gate_pass_rows", -1))
    rejected = int(design.get("gate_rejected_rows", -1))
    if total <= 0 or passed + rejected != total:
        raise UncertaintyError("OpenSky design population totals do not close")
    masks = design.get("by_failure_mask")
    if not isinstance(masks, list) or not masks:
        raise UncertaintyError("OpenSky design has no failure-mask partition")
    names = [row.get("failure_mask") for row in masks if isinstance(row, dict)]
    if len(names) != len(masks) or names != sorted(set(names)):
        raise UncertaintyError("OpenSky design failure masks must be sorted and unique")
    if sum(int(row.get("population_rows", -1)) for row in masks) != total:
        raise UncertaintyError("OpenSky design failure-mask rows do not close")
    if any(row.get("gate_pass") is not (row.get("failure_mask") == "0000")
           for row in masks):
        raise UncertaintyError("OpenSky design gate-pass labels differ from masks")
    blinding = design.get("blinding", {})
    if blinding.get("matching_allowed_primary_fields") != list(PRIMARY_MATCH_COLUMNS):
        raise UncertaintyError("OpenSky design matching whitelist differs from code")
    quality = design.get("source_quality", {})
    required_numbers = (
        "minimum_unique_points", "gap_threshold_s", "coverage_min_fraction",
        "flown_min_fraction_of_source_gc", "great_circle_min_km",
        "maximum_segment_speed_kt",
    )
    for name in required_numbers:
        value = _finite(quality.get(name))
        if value is None or value <= 0:
            raise UncertaintyError(f"OpenSky design source_quality.{name} must be positive")
    matching = design.get("matching", {})
    for name in (
        "candidate_max_departure_delta_s", "candidate_max_arrival_delta_s",
        "candidate_max_origin_distance_km", "candidate_max_destination_distance_km",
        "maximum_score", "tie_epsilon",
    ):
        value = _finite(matching.get(name))
        if value is None or value <= 0:
            raise UncertaintyError(f"OpenSky design matching.{name} must be positive")
    return {
        "release_id": manifest.release_id,
        "day": str(design["day"]),
        "population_rows": total,
        "design_sha256": sha256_file(ROOT / "opensky-day-audit-design.json")
        if (ROOT / "opensky-day-audit-design.json").is_file() else None,
    }


def _failure_mask(flags: pd.DataFrame) -> pd.Series:
    return flags.apply(
        lambda row: "".join("0" if bool(value) else "1" for value in row), axis=1)


def prepare_primary_artifacts(*, design_path: Path, manifest_path: Path,
                              flights_dir: Path, decomposition_dir: Path,
                              ground_dir: Path,
                              match_output: Path, private_output: Path) -> tuple[dict, dict]:
    """Freeze a blinded matching frame and a separate post-match key."""
    import track_quality

    _private_output(match_output, "OpenSky blinded match frame")
    _private_output(private_output, "OpenSky primary audit key")
    design = _load_json(design_path)
    validate_design(design, manifest_path)
    manifest = ReleaseManifest.load(manifest_path)
    manifest.verify_track_quality(track_quality)
    day = str(design["day"])
    columns = list(dict.fromkeys(SELECTION_COLUMNS + (
        "dep_ts", "arr_ts", "o_lat", "o_lon", "d_lat", "d_lon", "typecode",
    )))
    flights_path = Path(flights_dir) / day / "flights.parquet"
    decomposition_path = Path(decomposition_dir) / f"{day}.parquet"
    ground_path = Path(ground_dir) / f"{day}.parquet"
    try:
        flights = pq.read_table(flights_path, columns=columns).to_pandas()
        decomposition = pq.read_table(
            decomposition_path,
            columns=["flight_id", "co2_kg_v0", "ideal_gc_co2_kg", "hybrid_co2_kg"],
        ).to_pandas()
        ground = pq.read_table(
            ground_path, columns=["flight_id", "share_a3000t70"]).to_pandas()
    except Exception as exc:
        raise UncertaintyError(f"cannot read OpenSky primary day: {exc}") from exc
    if (flights.flight_id.duplicated().any()
            or decomposition.flight_id.duplicated().any()
            or ground.flight_id.duplicated().any()):
        raise UncertaintyError("OpenSky primary day has duplicate flight ids")
    if set(ground.flight_id.astype(int)) != set(flights.flight_id.astype(int)):
        raise UncertaintyError("OpenSky primary day ground-share keys do not close")
    shares = pd.to_numeric(ground.share_a3000t70, errors="coerce").to_numpy(float)
    if not np.isfinite(shares).all() or (shares < 0).any() or (shares > 1).any():
        raise UncertaintyError("OpenSky primary day contains invalid ground shares")
    flags = selection_flags(
        flights, coverage_min=track_quality.COV_MIN,
        gc_min_km=track_quality.GC_MIN_KM)
    flights = flights.copy()
    flights["failure_mask"] = _failure_mask(flags)
    flights["gate_pass"] = flags.all(axis=1).to_numpy(bool)
    expected = set(flights.loc[flights.gate_pass, "flight_id"].astype(int))
    actual = set(decomposition.flight_id.astype(int))
    if expected != actual:
        raise UncertaintyError(
            f"OpenSky day gate differs from decomposition: "
            f"{len(expected - actual)} missing, {len(actual - expected)} extra")
    observed = flights.groupby(["failure_mask", "gate_pass"], sort=True).size()
    registered = {
        (str(row["failure_mask"]), bool(row["gate_pass"])): int(row["population_rows"])
        for row in design["by_failure_mask"]
    }
    if observed.to_dict() != registered or len(flights) != int(design["population_rows"]):
        raise UncertaintyError("OpenSky day population differs from pre-registration")
    design_hash = sha256_file(design_path)
    flights["opaque_primary_id"] = [
        hashlib.sha256(f"{design_hash}\0{day}\0{int(fid)}".encode()).hexdigest()[:24]
        for fid in flights.flight_id
    ]
    if flights.opaque_primary_id.duplicated().any():
        raise UncertaintyError("opaque primary id collision")
    numeric = flights[["dep_ts", "arr_ts", "o_lat", "o_lon", "d_lat", "d_lon"]].apply(
        pd.to_numeric, errors="coerce").to_numpy(float)
    if not np.isfinite(numeric).all():
        raise UncertaintyError("primary matching frame contains non-finite values")
    joined = flights.merge(
        decomposition, on="flight_id", how="left", validate="one_to_one",
        suffixes=("", "_decomposition")).merge(
            ground, on="flight_id", how="left", validate="one_to_one")
    match_rows = flights[list(PRIMARY_MATCH_COLUMNS)].sort_values(
        "opaque_primary_id").to_dict("records")
    match_value = {
        "schema_version": 1,
        "kind": "co2gap-opensky-day-blinded-primary-frame",
        "publication_status": "private_per_flight",
        "design_sha256": design_hash,
        "day": day,
        "gate_status_disclosed": False,
        "primary_track_quality_disclosed": False,
        "columns": list(PRIMARY_MATCH_COLUMNS),
        "rows": match_rows,
    }
    _atomic_json(match_output, match_value)
    private_rows = []
    for row in joined.sort_values("opaque_primary_id").itertuples(index=False):
        private_rows.append({
            "opaque_primary_id": str(row.opaque_primary_id),
            "flight_id": int(row.flight_id),
            "typecode": str(row.typecode),
            "failure_mask": str(row.failure_mask),
            "gate_pass": bool(row.gate_pass),
            "primary_first_pass_co2_kg": float(row.co2_kg_v0),
            "primary_airborne_co2_kg": float(
                row.co2_kg_v0 * (1.0 - row.share_a3000t70)),
            "primary_ideal_co2_kg": (
                float(row.ideal_gc_co2_kg) if pd.notna(row.ideal_gc_co2_kg) else None),
            "primary_hybrid_co2_kg": (
                float(row.hybrid_co2_kg) if pd.notna(row.hybrid_co2_kg) else None),
        })
    private_value = {
        "schema_version": 1,
        "kind": "co2gap-opensky-day-private-primary-key",
        "publication_status": "private_per_flight",
        "design_sha256": design_hash,
        "blinded_frame_sha256": sha256_file(match_output),
        "day": day,
        "rows": private_rows,
    }
    _atomic_json(private_output, private_value)
    return match_value, private_value


def _nested(value) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "as_py"):
        converted = value.as_py()
        return converted if isinstance(converted, dict) else {}
    return {}


def _track_endpoints(value) -> tuple[tuple[float, float] | None,
                                     tuple[float, float] | None]:
    if value is None:
        return None, None
    points = value.tolist() if isinstance(value, np.ndarray) else list(value)
    valid = []
    for point in points:
        point = _nested(point)
        t = _finite(point.get("time"))
        lat = _finite(point.get("latitude"))
        lon = _finite(point.get("longitude"))
        if (t is not None and lat is not None and lon is not None
                and -90 <= lat <= 90 and -180 <= lon <= 180):
            valid.append((t, lat, lon))
    if not valid:
        return None, None
    valid.sort()
    return (valid[0][1], valid[0][2]), (valid[-1][1], valid[-1][2])


def normalize_source_flights(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Turn OpenSky flight rows into source-only matching anchors."""
    required = {
        "icao24", "firstSeen", "lastSeen", "track", "takeoffLandingInfo",
    }
    rows = []
    for frame in frames:
        missing = sorted(required - set(frame.columns))
        if missing:
            raise UncertaintyError(f"OpenSky flights lack columns: {', '.join(missing)}")
        for raw in frame.itertuples(index=False):
            data = raw._asdict()
            info = _nested(data.get("takeoffLandingInfo"))
            dep = _finite(info.get("takeoffTime"))
            arr = _finite(info.get("landingTime"))
            if dep is None or arr is None:
                dep, arr = _finite(data.get("firstSeen")), _finite(data.get("lastSeen"))
            first, last = _track_endpoints(data.get("track"))
            o_lat = _finite(info.get("takeoffLatitude"))
            o_lon = _finite(info.get("takeoffLongitude"))
            d_lat = _finite(info.get("landingLatitude"))
            d_lon = _finite(info.get("landingLongitude"))
            if o_lat is None or o_lon is None:
                o_lat, o_lon = first if first is not None else (None, None)
            if d_lat is None or d_lon is None:
                d_lat, d_lon = last if last is not None else (None, None)
            icao = str(data.get("icao24") or "").strip().lower()
            valid = (
                len(icao) == 6 and dep is not None and arr is not None and arr > dep
                and all(value is not None for value in (o_lat, o_lon, d_lat, d_lon))
                and -90 <= o_lat <= 90 and -180 <= o_lon <= 180
                and -90 <= d_lat <= 90 and -180 <= d_lon <= 180
            )
            if not valid:
                continue
            fingerprint = (
                f"{icao}\0{int(dep)}\0{int(arr)}\0{o_lat:.6f}\0{o_lon:.6f}"
                f"\0{d_lat:.6f}\0{d_lon:.6f}")
            rows.append({
                "source_id": hashlib.sha256(fingerprint.encode()).hexdigest()[:24],
                "icao24": icao,
                "dep_ts": int(dep), "arr_ts": int(arr),
                "o_lat": float(o_lat), "o_lon": float(o_lon),
                "d_lat": float(d_lat), "d_lon": float(d_lon),
            })
    if not rows:
        raise UncertaintyError("OpenSky flights contain no valid matching anchors")
    source = pd.DataFrame(rows).drop_duplicates()
    conflicting = source.groupby("source_id", sort=False).size()
    if (conflicting > 1).any():
        raise UncertaintyError("OpenSky source id collision or conflicting duplicate")
    return source.sort_values(["dep_ts", "source_id"]).reset_index(drop=True)


def _require_blinded_primary(frame: pd.DataFrame, design: dict) -> None:
    required = set(PRIMARY_MATCH_COLUMNS)
    missing = sorted(required - set(frame.columns))
    if missing:
        raise UncertaintyError(f"blinded primary frame lacks: {', '.join(missing)}")
    prohibited = set(design["blinding"]["matching_prohibited_primary_fields"])
    leaked = sorted(prohibited & set(frame.columns))
    if leaked:
        raise UncertaintyError(
            f"matching received prohibited primary field(s): {', '.join(leaked)}")
    extra = sorted(set(frame.columns) - required)
    if extra:
        raise UncertaintyError(
            f"matching received undeclared primary field(s): {', '.join(extra)}")


def match_flights(primary: pd.DataFrame, source: pd.DataFrame,
                  design: dict) -> pd.DataFrame:
    """Apply the frozen source-only candidate score and mutual-nearest rule."""
    _require_blinded_primary(primary, design)
    missing = sorted(set(SOURCE_MATCH_COLUMNS) - set(source.columns))
    if missing:
        raise UncertaintyError(f"source match frame lacks: {', '.join(missing)}")
    if primary.opaque_primary_id.duplicated().any() or source.source_id.duplicated().any():
        raise UncertaintyError("matching ids must be unique")
    config = design["matching"]
    dep_limit = float(config["candidate_max_departure_delta_s"])
    arr_limit = float(config["candidate_max_arrival_delta_s"])
    o_limit = float(config["candidate_max_origin_distance_km"])
    d_limit = float(config["candidate_max_destination_distance_km"])
    max_score = float(config["maximum_score"])
    epsilon = float(config["tie_epsilon"])
    source = source.reset_index(drop=True)
    order = np.argsort(source.dep_ts.to_numpy(float), kind="stable")
    dep_sorted = source.dep_ts.to_numpy(float)[order]
    candidates = []
    raw_counts = Counter()
    for pi, row in enumerate(primary.itertuples(index=False)):
        lo = bisect.bisect_left(dep_sorted, float(row.dep_ts) - dep_limit)
        hi = bisect.bisect_right(dep_sorted, float(row.dep_ts) + dep_limit)
        indices = order[lo:hi]
        if len(indices) == 0:
            continue
        cand = source.iloc[indices]
        dep_delta = np.abs(cand.dep_ts.to_numpy(float) - float(row.dep_ts))
        arr_delta = np.abs(cand.arr_ts.to_numpy(float) - float(row.arr_ts))
        origin = _haversine_km(
            float(row.o_lat), float(row.o_lon),
            cand.o_lat.to_numpy(float), cand.o_lon.to_numpy(float))
        destination = _haversine_km(
            float(row.d_lat), float(row.d_lon),
            cand.d_lat.to_numpy(float), cand.d_lon.to_numpy(float))
        keep = ((arr_delta <= arr_limit) & (origin <= o_limit)
                & (destination <= d_limit))
        for local in np.flatnonzero(keep):
            si = int(indices[local])
            score = (
                dep_delta[local] / 900.0 + arr_delta[local] / 900.0
                + origin[local] / 25.0 + destination[local] / 25.0)
            raw_counts[pi] += 1
            if score <= max_score + epsilon:
                candidates.append({
                    "pi": pi, "si": si, "score": float(score),
                    "departure_time_delta_s": float(dep_delta[local]),
                    "arrival_time_delta_s": float(arr_delta[local]),
                    "origin_distance_km": float(origin[local]),
                    "destination_distance_km": float(destination[local]),
                })
    cand = pd.DataFrame(candidates)
    by_primary = defaultdict(list)
    by_source = defaultdict(list)
    if not cand.empty:
        for index, row in cand.iterrows():
            by_primary[int(row.pi)].append((float(row.score), int(row.si), int(index)))
            by_source[int(row.si)].append((float(row.score), int(row.pi), int(index)))
        for values in by_primary.values():
            values.sort()
        for values in by_source.values():
            values.sort()
    ratio = 0.75

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
        if not margin(values) or not margin(source_values):
            continue
        accepted[pi] = (best[2], len(values), len(source_values))
    output = []
    for pi, row in enumerate(primary.itertuples(index=False)):
        base = {"opaque_primary_id": str(row.opaque_primary_id)}
        if pi in accepted:
            ci, primary_n, source_n = accepted[pi]
            c = cand.loc[ci]
            s = source.iloc[int(c.si)]
            output.append({
                **base, "match_status": "matched",
                "source_id": str(s.source_id), "icao24": str(s.icao24),
                "source_dep_ts": int(s.dep_ts), "source_arr_ts": int(s.arr_ts),
                "source_o_lat": float(s.o_lat), "source_o_lon": float(s.o_lon),
                "source_d_lat": float(s.d_lat), "source_d_lon": float(s.d_lon),
                "candidate_count_primary": int(primary_n),
                "candidate_count_source": int(source_n),
                "score": float(c.score),
                "departure_time_delta_s": float(c.departure_time_delta_s),
                "arrival_time_delta_s": float(c.arrival_time_delta_s),
                "origin_distance_km": float(c.origin_distance_km),
                "destination_distance_km": float(c.destination_distance_km),
            })
            continue
        values = by_primary.get(pi, [])
        if not values:
            status = "score_exceeded" if raw_counts[pi] else "no_candidate"
        elif not margin(values):
            status = "ambiguous_primary"
        else:
            si = values[0][1]
            source_values = by_source[si]
            status = (
                "ambiguous_source" if source_values[0][1] == pi and not margin(source_values)
                else "not_mutual_nearest")
        output.append({**base, "match_status": status})
    result = pd.DataFrame(output)
    if len(result) != len(primary) or result.opaque_primary_id.duplicated().any():
        raise UncertaintyError("matching result does not close on blinded primary frame")
    matched = result[result.match_status == "matched"]
    if not matched.empty and matched["source_id"].duplicated().any():
        raise UncertaintyError("matching result is not one-to-one")
    return result


def read_source_flights(paths: list[Path]) -> tuple[pd.DataFrame, list[dict]]:
    columns = [
        "icao24", "firstSeen", "lastSeen", "track", "takeoffLandingInfo",
    ]
    frames, manifest = [], []
    for path in paths:
        path = Path(path)
        try:
            frames.append(pq.read_table(path, columns=columns).to_pandas())
        except Exception as exc:
            raise UncertaintyError(f"cannot read OpenSky flights {path}: {exc}") from exc
        manifest.append({
            "name": path.name, "bytes": path.stat().st_size,
            "sha256": sha256_file(path), "rows": int(len(frames[-1])),
        })
    return normalize_source_flights(frames), manifest


def write_matches(*, design_path: Path, primary_path: Path,
                  source_paths: list[Path], output: Path) -> dict:
    _private_output(output, "OpenSky match result")
    design = _load_json(design_path)
    primary_value = _load_json(primary_path)
    if primary_value.get("design_sha256") != sha256_file(design_path):
        raise UncertaintyError("blinded frame does not belong to OpenSky design")
    primary = pd.DataFrame(primary_value.get("rows", []))
    source, source_manifest = read_source_flights(source_paths)
    result = match_flights(primary, source, design)
    value = {
        "schema_version": 1,
        "kind": "co2gap-opensky-day-match-result",
        "publication_status": "private_per_flight",
        "design_sha256": sha256_file(design_path),
        "blinded_frame_sha256": sha256_file(primary_path),
        "source_files": source_manifest,
        "source_valid_flights": int(len(source)),
        "match_status_counts": {
            str(name): int(count)
            for name, count in result.match_status.value_counts().sort_index().items()
        },
        "rows": result.sort_values("opaque_primary_id").replace({np.nan: None}).to_dict("records"),
    }
    _atomic_json(output, value)
    return value


def _merge_windows(windows: list[tuple[float, float]]) -> list[tuple[float, float]]:
    result = []
    for start, end in sorted(windows):
        if not result or start > result[-1][1]:
            result.append([start, end])
        else:
            result[-1][1] = max(result[-1][1], end)
    return [(float(start), float(end)) for start, end in result]


def _inside_windows(times: np.ndarray, windows: list[tuple[float, float]]) -> np.ndarray:
    keep = np.zeros(len(times), dtype=bool)
    for start, end in windows:
        keep |= (times >= start) & (times <= end)
    return keep


def extract_state_vectors(*, design_path: Path, matches_path: Path,
                          source_paths: list[str], output: Path,
                          filesystem=None, batch_size: int = 500_000) -> dict:
    """Stream only matched aircraft/time windows to a private local parquet."""
    _private_output(output, "OpenSky extracted state vectors")
    design = _load_json(design_path)
    matches_value = _load_json(matches_path)
    if matches_value.get("design_sha256") != sha256_file(design_path):
        raise UncertaintyError("matches do not belong to OpenSky design")
    matched = pd.DataFrame([
        row for row in matches_value.get("rows", [])
        if row.get("match_status") == "matched"
    ])
    if matched.empty:
        raise UncertaintyError("no OpenSky matches available for state-vector extraction")
    padding = float(design["state_vector_extraction"]["matched_window_padding_s"])
    windows = defaultdict(list)
    for row in matched.itertuples(index=False):
        windows[str(row.icao24)].append(
            (float(row.source_dep_ts) - padding, float(row.source_arr_ts) + padding))
    windows = {key: _merge_windows(value) for key, value in windows.items()}
    addresses = pa.array(sorted(windows), type=pa.string())
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(f".{output.name}.tmp")
    writer = None
    total = 0
    source_manifest = []
    try:
        for raw_path in source_paths:
            path = str(raw_path)
            handle = filesystem.open_input_file(path) if filesystem is not None else path
            pf = pq.ParquetFile(handle)
            source_bytes = (
                int(filesystem.get_file_info(path).size)
                if filesystem is not None else int(Path(path).stat().st_size))
            source_manifest.append({
                "path": path, "bytes": source_bytes,
                "rows": int(pf.metadata.num_rows),
            })
            for batch in pf.iter_batches(
                    columns=list(STATE_VECTOR_COLUMNS), batch_size=batch_size):
                address_mask = pc.is_in(
                    batch.column(batch.schema.get_field_index("icao24")),
                    value_set=addresses)
                selected = pa.Table.from_batches([batch]).filter(address_mask)
                if selected.num_rows == 0:
                    continue
                frame = selected.to_pandas()
                point_time = pd.to_numeric(frame.lastPosUpdate, errors="coerce").to_numpy(float)
                keep = np.zeros(len(frame), dtype=bool)
                for icao, indices in frame.groupby("icao24", sort=False).groups.items():
                    loc = np.asarray(list(indices), dtype=int)
                    keep[loc] = _inside_windows(point_time[loc], windows[str(icao)])
                frame = frame.loc[keep]
                if frame.empty:
                    continue
                table = pa.Table.from_pandas(frame, preserve_index=False)
                if writer is None:
                    writer = pq.ParquetWriter(tmp, table.schema, compression="zstd")
                writer.write_table(table)
                total += len(frame)
            print(
                f"state vectors: {Path(path).parent.name}, "
                f"{total:,} matched rows cumulative", flush=True)
    finally:
        if writer is not None:
            writer.close()
    if writer is None or total == 0:
        if tmp.exists():
            tmp.unlink()
        raise UncertaintyError("state-vector extraction returned no rows")
    tmp.replace(output)
    contract = {
        "schema_version": 1,
        "kind": "co2gap-opensky-day-extracted-state-vectors",
        "publication_status": "private_per_flight",
        "design_sha256": sha256_file(design_path),
        "matches_sha256": sha256_file(matches_path),
        "source_objects": source_manifest,
        "rows": int(total),
        "parquet_sha256": sha256_file(output),
    }
    _atomic_json(output.with_suffix(".contract.json"), contract)
    return contract


def partition_state_vectors(*, design_path: Path, matches_path: Path,
                            state_vectors_path: Path, output_dir: Path,
                            buckets: int = 64,
                            batch_size: int = 500_000) -> dict:
    """Assign points to non-overlapping source flights and hash partitions.

    The extracted file is deliberately dense.  Hash partitioning keeps the
    audit bounded in memory without publishing a per-flight artefact.
    """
    _private_output(output_dir, "OpenSky partitioned state vectors")
    if buckets < 2:
        raise UncertaintyError("OpenSky state-vector partitions must be at least 2")
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise UncertaintyError(
            f"OpenSky partition output already exists; refusing to replace it: {output_dir}")
    staging = output_dir.with_name(f".{output_dir.name}.staging-{os.getpid()}")
    if staging.exists():
        raise UncertaintyError(f"OpenSky partition staging already exists: {staging}")
    staging.mkdir(parents=True)
    design = _load_json(design_path)
    matches_value = _load_json(matches_path)
    if matches_value.get("design_sha256") != sha256_file(design_path):
        raise UncertaintyError("matches do not belong to OpenSky design")
    matched = pd.DataFrame([
        row for row in matches_value.get("rows", [])
        if row.get("match_status") == "matched"
    ])
    if matched.empty:
        raise UncertaintyError("no OpenSky matches available for partitioning")
    intervals = defaultdict(list)
    for row in matched.sort_values(["icao24", "source_dep_ts"]).itertuples(index=False):
        current = (
            float(row.source_dep_ts), float(row.source_arr_ts), str(row.source_id))
        previous = intervals[str(row.icao24)][-1] if intervals[str(row.icao24)] else None
        if previous is not None and current[0] <= previous[1]:
            raise UncertaintyError(
                f"overlapping matched source-flight windows for {row.icao24}")
        intervals[str(row.icao24)].append(current)
    writers = {}
    input_rows = assigned_rows = written_rows = 0
    try:
        pf = pq.ParquetFile(state_vectors_path)
        for batch in pf.iter_batches(batch_size=batch_size):
            frame = pa.Table.from_batches([batch]).to_pandas()
            input_rows += len(frame)
            pieces = []
            for icao, indices in frame.groupby("icao24", sort=False).groups.items():
                windows = intervals.get(str(icao), [])
                if not windows:
                    continue
                group = frame.loc[indices]
                times = pd.to_numeric(group.lastPosUpdate, errors="coerce").to_numpy(float)
                for start, end, source_id in windows:
                    keep = np.isfinite(times) & (times >= start) & (times <= end)
                    if keep.any():
                        piece = group.iloc[np.flatnonzero(keep)].copy()
                        piece["source_id"] = source_id
                        pieces.append(piece)
            if pieces:
                assigned = pd.concat(pieces, ignore_index=True)
                assigned_rows += len(assigned)
                assigned = assigned.sort_values(
                    ["source_id", "lastPosUpdate", "time"]).drop_duplicates(
                        ["source_id", "lastPosUpdate"], keep="last")
                assigned["_bucket"] = assigned.source_id.map(
                    lambda value: int(
                        hashlib.sha256(str(value).encode()).hexdigest()[:16], 16)
                    % buckets)
                for bucket, group in assigned.groupby("_bucket", sort=False):
                    table = pa.Table.from_pandas(
                        group.drop(columns="_bucket"), preserve_index=False)
                    bucket = int(bucket)
                    if bucket not in writers:
                        path = staging / f"bucket-{bucket:03d}.parquet"
                        writers[bucket] = pq.ParquetWriter(
                            path, table.schema, compression="zstd")
                    writers[bucket].write_table(table)
                    written_rows += len(group)
            if input_rows // 5_000_000 != (input_rows - len(frame)) // 5_000_000:
                print(
                    f"partition: {input_rows:,} dense rows read; "
                    f"{written_rows:,} batch-deduplicated rows written", flush=True)
    finally:
        for writer in writers.values():
            writer.close()
    if not writers:
        raise UncertaintyError("OpenSky partitioning produced no state vectors")
    bucket_files = sorted(staging.glob("bucket-*.parquet"))
    manifest = []
    for path in bucket_files:
        parquet = pq.ParquetFile(path)
        manifest.append({
            "name": path.name, "bytes": path.stat().st_size,
            "rows": int(parquet.metadata.num_rows), "sha256": sha256_file(path),
        })
    contract = {
        "schema_version": 1,
        "kind": "co2gap-opensky-day-partitioned-state-vectors",
        "publication_status": "private_per_flight",
        "design_sha256": sha256_file(design_path),
        "matches_sha256": sha256_file(matches_path),
        "dense_state_vectors_sha256": sha256_file(state_vectors_path),
        "buckets_requested": int(buckets),
        "bucket_files": len(bucket_files),
        "dense_input_rows": int(input_rows),
        "window_assigned_rows_before_batch_deduplication": int(assigned_rows),
        "rows_after_batch_deduplication": int(written_rows),
        "files": manifest,
        "cross_batch_deduplication": (
            "Deferred to source_track_quality inside each complete source flight."),
    }
    _atomic_json(staging / "contract.json", contract)
    staging.replace(output_dir)
    return contract


def source_track_quality(points: pd.DataFrame, source_row: dict,
                         design: dict) -> tuple[pd.DataFrame, dict]:
    """Normalise one independent track and evaluate only frozen source rules."""
    required = set(STATE_VECTOR_COLUMNS)
    missing = sorted(required - set(points.columns))
    if missing:
        raise UncertaintyError(f"state vectors lack: {', '.join(missing)}")
    q = design["source_quality"]
    frame = points.copy()
    for name in (
        "time", "lastPosUpdate", "lat", "lon", "velocity", "baroAltitude",
        "geoAltitude", "vertRate",
    ):
        frame[name] = pd.to_numeric(frame[name], errors="coerce")
    frame = frame[
        np.isfinite(frame.lastPosUpdate) & np.isfinite(frame.lat) & np.isfinite(frame.lon)
        & frame.lat.between(-90, 90) & frame.lon.between(-180, 180)
    ].copy()
    dep, arr = float(source_row["source_dep_ts"]), float(source_row["source_arr_ts"])
    frame = frame[(frame.lastPosUpdate >= dep) & (frame.lastPosUpdate <= arr)]
    frame = frame.sort_values(["lastPosUpdate", "time"]).drop_duplicates(
        ["icao24", "lastPosUpdate"], keep="last")
    aggregation = design["state_vector_extraction"].get("temporal_aggregation")
    if aggregation:
        seconds = int(aggregation["seconds"])
        if seconds <= 0:
            raise UncertaintyError("source temporal aggregation must be positive")
        frame["_time_bin"] = np.floor(frame.lastPosUpdate / seconds).astype("int64")
        numeric = [
            "time", "lastPosUpdate", "lat", "lon", "velocity",
            "baroAltitude", "geoAltitude", "vertRate",
        ]
        medians = frame.groupby("_time_bin", sort=True)[numeric].median()
        ground = frame.groupby("_time_bin", sort=True).onGround.apply(
            lambda values: bool(values.fillna(False).astype(bool).mean() >= 0.5))
        icao = str(frame.icao24.iloc[0]) if not frame.empty else ""
        frame = medians.reset_index(drop=True)
        frame["onGround"] = ground.to_numpy(bool)
        frame["icao24"] = icao
    frame = frame.sort_values("lastPosUpdate").reset_index(drop=True)
    reasons = []
    n = len(frame)
    minimum = int(q["minimum_unique_points"])
    if n < minimum:
        reasons.append("too_few_unique_points")
    if n >= 2:
        t = frame.lastPosUpdate.to_numpy(float)
        dt = np.diff(t)
        duration = float(t[-1] - t[0])
        gaps = dt[dt > float(q["gap_threshold_s"])]
        coverage = 1.0 - float(gaps.sum()) / duration if duration > 0 else float("nan")
        segment_km = _haversine_km(
            frame.lat.to_numpy(float)[:-1], frame.lon.to_numpy(float)[:-1],
            frame.lat.to_numpy(float)[1:], frame.lon.to_numpy(float)[1:])
        flown = float(segment_km.sum())
        speed = segment_km / np.where(dt > 0, dt, np.nan) * 3600.0 / 1.852
        max_speed = float(np.nanmax(speed)) if np.isfinite(speed).any() else float("nan")
        usable_steps = int(((dt > 0) & (dt <= 600)
                            & np.isfinite(frame.velocity.to_numpy(float)[:-1])
                            & (frame.velocity.to_numpy(float)[:-1] > 0)).sum())
    else:
        duration = coverage = flown = max_speed = float("nan")
        usable_steps = 0
    gc = float(_haversine_km(
        source_row["source_o_lat"], source_row["source_o_lon"],
        source_row["source_d_lat"], source_row["source_d_lon"]))
    ratio = flown / gc if gc > 0 and math.isfinite(flown) else float("nan")
    if not math.isfinite(coverage) or coverage < float(q["coverage_min_fraction"]):
        reasons.append("coverage_below_threshold")
    if not math.isfinite(ratio) or ratio < float(q["flown_min_fraction_of_source_gc"]):
        reasons.append("flown_distance_below_threshold")
    if gc < float(q["great_circle_min_km"]):
        reasons.append("sector_below_threshold")
    if not math.isfinite(max_speed) or max_speed > float(q["maximum_segment_speed_kt"]):
        reasons.append("implausible_segment_speed")
    if usable_steps < 10:
        reasons.append("too_few_model_steps")
    altitude = frame.baroAltitude.where(
        np.isfinite(frame.baroAltitude), frame.geoAltitude)
    altitude = altitude * 3.280839895013123
    altitude = altitude.where(~frame.onGround.fillna(False).astype(bool), 0.0)
    model = pd.DataFrame({
        "t": frame.lastPosUpdate.astype(float),
        "lat": frame.lat.astype(float), "lon": frame.lon.astype(float),
        "alt_ft": altitude.astype(float),
        "gs_kt": frame.velocity.astype(float) * 1.9438444924406048,
        "ias_kt": np.nan,
        "vs_fpm": frame.vertRate.astype(float) * 196.8503937007874,
    })
    metrics = {
        "quality_pass": not reasons,
        "failure_reasons": sorted(set(reasons)),
        "unique_points": int(n), "usable_model_steps": int(usable_steps),
        "duration_s": duration, "coverage_fraction": coverage,
        "great_circle_km": gc, "flown_km": flown,
        "flown_fraction_of_gc": ratio, "maximum_segment_speed_kt": max_speed,
    }
    return model, metrics


def _ratio_metrics(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    real = float(sum(row["real_co2_kg"] for row in rows))
    ideal = float(sum(row["ideal_co2_kg"] for row in rows))
    hybrid = float(sum(row["hybrid_co2_kg"] for row in rows))
    if ideal <= 0:
        return None
    return {
        "flights": len(rows),
        "real_co2_tonnes": real / 1000.0,
        "ideal_co2_tonnes": ideal / 1000.0,
        "total_gap_pct": (real - ideal) / ideal * 100.0,
        "lateral_gap_pct": (hybrid - ideal) / ideal * 100.0,
        "vertical_gap_pct": (real - hybrid) / ideal * 100.0,
    }


def run_audit(*, design_path: Path, manifest_path: Path, private_path: Path,
              matches_path: Path, state_vectors_path: Path,
              era5_dir: Path) -> dict:
    """Compute aggregate conditional diagnostics and discard flight outcomes."""
    from decompose import decompose_flight
    from emissions import estimate_fuel
    from trajectories import Flight, Point
    from era5 import WindField, required_wind_days

    design = _load_json(design_path)
    validate_design(design, manifest_path)
    private_value = _load_json(private_path)
    matches_value = _load_json(matches_path)
    design_hash = sha256_file(design_path)
    if private_value.get("design_sha256") != design_hash:
        raise UncertaintyError("primary key does not belong to OpenSky design")
    if matches_value.get("design_sha256") != design_hash:
        raise UncertaintyError("matches do not belong to OpenSky design")
    private = pd.DataFrame(private_value.get("rows", []))
    matches = pd.DataFrame(matches_value.get("rows", []))
    if len(private) != int(design["population_rows"]) or len(matches) != len(private):
        raise UncertaintyError("OpenSky audit inputs do not close on registered population")
    joined = private.merge(matches, on="opaque_primary_id", validate="one_to_one")
    state_vectors_path = Path(state_vectors_path)
    if not state_vectors_path.is_dir():
        raise UncertaintyError(
            "audit requires the hash-partition directory produced by partition")
    partition_contract_path = state_vectors_path / "contract.json"
    partition_contract = _load_json(partition_contract_path)
    if partition_contract.get("design_sha256") != design_hash:
        raise UncertaintyError("state-vector partitions do not belong to OpenSky design")
    if partition_contract.get("matches_sha256") != sha256_file(matches_path):
        raise UncertaintyError("state-vector partitions do not belong to match result")
    registered_files = partition_contract.get("files", [])
    actual_names = sorted(path.name for path in state_vectors_path.glob("bucket-*.parquet"))
    expected_names = sorted(str(row.get("name")) for row in registered_files)
    if actual_names != expected_names:
        raise UncertaintyError("OpenSky state-vector bucket set differs from contract")
    for item in registered_files:
        path = state_vectors_path / str(item["name"])
        if path.stat().st_size != int(item["bytes"]) or sha256_file(path) != item["sha256"]:
            raise UncertaintyError(f"OpenSky state-vector bucket differs: {path.name}")
    manifest = ReleaseManifest.load(manifest_path)
    wind_days = required_wind_days([str(design["day"])])
    era5_config = manifest.data["configuration"]["era5"]
    wind_validation = {
        "levels": list(era5_config["pressure_levels_hpa"]),
        "area": list(era5_config["area_nwse"]),
        "grid": tuple(era5_config["grid_degrees"]),
        "variables": tuple(era5_config["variables"]),
    }
    wind = WindField(
        [Path(era5_dir) / f"{day}.nc" for day in wind_days],
        validation=wind_validation,
    )
    status_by_mask = defaultdict(Counter)
    quality_by_mask = defaultdict(Counter)
    quality_reasons = defaultdict(Counter)
    outcomes = []
    model_failures = Counter()
    for row in joined.itertuples(index=False):
        mask = str(row.failure_mask)
        status_by_mask[mask][str(row.match_status)] += 1
    matched_rows = joined[joined.match_status == "matched"].copy()
    if matched_rows.source_id.duplicated().any():
        raise UncertaintyError("matched source ids are not unique before audit")
    matched_by_source = {
        str(row.source_id): row for row in matched_rows.itertuples(index=False)
    }
    processed = set()
    for bucket_index, bucket_path in enumerate(
            (state_vectors_path / name for name in actual_names), start=1):
        bucket = pq.read_table(bucket_path).to_pandas()
        if not set(STATE_VECTOR_COLUMNS + ("source_id",)).issubset(bucket.columns):
            raise UncertaintyError(
                f"state-vector bucket has unexpected schema: {bucket_path.name}")
        for source_id, selected in bucket.groupby("source_id", sort=False):
            source_id = str(source_id)
            if source_id in processed:
                raise UncertaintyError(
                    f"source flight appears in multiple hash buckets: {source_id}")
            processed.add(source_id)
            row = matched_by_source.get(source_id)
            if row is None:
                raise UncertaintyError(
                    f"state-vector bucket contains unmatched source flight: {source_id}")
            mask = str(row.failure_mask)
            model_points, quality = source_track_quality(
                selected, row._asdict(), design)
            if not quality["quality_pass"]:
                quality_by_mask[mask]["source_quality_fail"] += 1
                for reason in quality["failure_reasons"]:
                    quality_reasons[mask][reason] += 1
                continue
            built = [
                Point(
                    t=float(p.t), lat=float(p.lat), lon=float(p.lon),
                    alt=_finite(p.alt_ft), gs=_finite(p.gs_kt), ias=None,
                    vs_rep=_finite(p.vs_fpm),
                )
                for p in model_points.itertuples(index=False)
            ]
            flight = Flight(
                icao="OPENSKY", typecode=str(row.typecode), reg=None, points=built)
            fuel = estimate_fuel(
                flight,
                load_factor=float(design["proxy_model"]["load_factor"]),
                reserve_kg=float(design["proxy_model"]["reserve_kg"]),
                tas_mode="gs",
            )
            if not fuel.ok or fuel.co2_kg <= 0:
                model_failures[f"fuel:{fuel.reason}"] += 1
                quality_by_mask[mask]["model_fail_after_source_quality_pass"] += 1
                continue
            result = decompose_flight(
                str(row.typecode), float(fuel.co2_kg),
                float(quality["great_circle_km"]), float(quality["flown_km"]),
                model_points.lat.to_numpy(float), model_points.lon.to_numpy(float),
                int(row.source_dep_ts), wind,
                load_factor=float(design["proxy_model"]["load_factor"]),
                reserve_kg=float(design["proxy_model"]["reserve_kg"]),
                alt_ft=model_points.alt_ft.to_numpy(float),
                ias_kt=None, vs_fpm=model_points.vs_fpm.to_numpy(float),
            )
            if result is None:
                model_failures["decomposition"] += 1
                quality_by_mask[mask]["model_fail_after_source_quality_pass"] += 1
                continue
            quality_by_mask[mask]["model_success_after_source_quality_pass"] += 1
            outcomes.append({
                "failure_mask": mask, "gate_pass": bool(row.gate_pass),
                "real_co2_kg": float(fuel.co2_kg),
                "ideal_co2_kg": float(result["ideal_gc_co2_kg"]),
                "hybrid_co2_kg": float(result["hybrid_co2_kg"]),
                "primary_real_co2_kg": _finite(row.primary_first_pass_co2_kg),
                "primary_airborne_co2_kg": _finite(row.primary_airborne_co2_kg),
                "primary_ideal_co2_kg": _finite(row.primary_ideal_co2_kg),
                "primary_hybrid_co2_kg": _finite(row.primary_hybrid_co2_kg),
            })
        print(
            f"audit: bucket {bucket_index}/{len(actual_names)}; "
            f"{len(processed):,} source flights checked; "
            f"{len(outcomes):,} model outcomes", flush=True)
    for source_id, row in matched_by_source.items():
        if source_id not in processed:
            quality_by_mask[str(row.failure_mask)]["missing_state_vectors"] += 1
    by_mask = []
    for registered in design["by_failure_mask"]:
        mask = str(registered["failure_mask"])
        row_outcomes = [row for row in outcomes if row["failure_mask"] == mask]
        by_mask.append({
            "failure_mask": mask,
            "gate_pass": bool(registered["gate_pass"]),
            "population_rows": int(registered["population_rows"]),
            "matching": dict(sorted(status_by_mask[mask].items())),
            "source_quality_and_model": dict(sorted(quality_by_mask[mask].items())),
            "source_quality_failure_reasons": dict(sorted(quality_reasons[mask].items())),
            "proxy": _ratio_metrics(row_outcomes),
        })
    passing = [row for row in outcomes if row["gate_pass"]]
    rejected = [row for row in outcomes if not row["gate_pass"]]
    pass_metric, reject_metric = _ratio_metrics(passing), _ratio_metrics(rejected)
    contrast = None
    if pass_metric is not None and reject_metric is not None:
        contrast = {
            name: float(reject_metric[name] - pass_metric[name])
            for name in ("total_gap_pct", "lateral_gap_pct", "vertical_gap_pct")
        }
    controls = [
        row for row in passing
        if all(row[name] is not None for name in (
            "primary_airborne_co2_kg", "primary_ideal_co2_kg",
            "primary_hybrid_co2_kg"))
    ]
    primary_control = _ratio_metrics([
        {
            "real_co2_kg": row["primary_airborne_co2_kg"],
            "ideal_co2_kg": row["primary_ideal_co2_kg"],
            "hybrid_co2_kg": row["primary_hybrid_co2_kg"],
        }
        for row in controls
    ])
    proxy_control = _ratio_metrics(controls)
    control_comparison = None
    if primary_control and proxy_control:
        control_comparison = {
            "flights": len(controls),
            "primary_frozen_airborne_a3000t70": primary_control,
            "opensky_takeoff_to_landing_ground_speed_proxy": proxy_control,
            "opensky_minus_primary_percentage_points": {
                name: float(proxy_control[name] - primary_control[name])
                for name in ("total_gap_pct", "lateral_gap_pct", "vertical_gap_pct")
            },
        }
    return {
        "schema_version": 1,
        "kind": "co2gap-opensky-day-selection-audit-result",
        "publication_status": "aggregate_diagnostic",
        "design_sha256": design_hash,
        "release_id": str(design["release_id"]),
        "day": str(design["day"]),
        "source_relation": design["external_source"]["independence_status"],
        "primary_headline_bias_bounded": False,
        "matching_status_counts": dict(sorted(Counter(matches.match_status).items())),
        "by_failure_mask": by_mask,
        "conditional_proxy": {
            "primary_gate_pass": pass_metric,
            "primary_gate_rejected": reject_metric,
            "rejected_minus_passed_percentage_points": contrast,
        },
        "primary_control_comparison": control_comparison,
        "model_failures": dict(sorted(model_failures.items())),
        "input_hashes": {
            "private_primary": sha256_file(private_path),
            "matches": sha256_file(matches_path),
            "state_vector_partition_contract": sha256_file(partition_contract_path),
        },
        "interpretation": (
            "One-day conditional diagnostic. Missing source matches and failed source "
            "quality are not imputed; receiver independence is not established; no "
            "full-release selection-bias bound follows from this result."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--design", type=Path, default=ROOT / "opensky-day-audit-design.json")
    parser.add_argument(
        "--release-manifest", type=Path, default=ROOT / "release-manifest.json")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate", help="validate the frozen one-day design")
    prepare = sub.add_parser("prepare", help="write blinded and private primary frames")
    prepare.add_argument("--flights-dir", type=Path, required=True)
    prepare.add_argument("--decomposition-dir", type=Path, required=True)
    prepare.add_argument("--ground-dir", type=Path, required=True)
    prepare.add_argument("--match-out", type=Path, required=True)
    prepare.add_argument("--private-out", type=Path, required=True)

    match = sub.add_parser("match", help="match a blinded frame to local OpenSky flights")
    match.add_argument("--primary", type=Path, required=True)
    match.add_argument("--source-flight", type=Path, action="append", required=True)
    match.add_argument("--out", type=Path, required=True)

    extract = sub.add_parser("extract", help="extract matched state vectors")
    extract.add_argument("--matches", type=Path, required=True)
    extract.add_argument("--source-state-vectors", action="append")
    extract.add_argument(
        "--s3-anonymous-prefix",
        help="anonymous OpenSky S3 prefix; all parquet objects below it are read")
    extract.add_argument("--out", type=Path, required=True)

    partition = sub.add_parser(
        "partition", help="assign dense points to source flights and hash buckets")
    partition.add_argument("--matches", type=Path, required=True)
    partition.add_argument("--state-vectors", type=Path, required=True)
    partition.add_argument("--buckets", type=int, default=64)
    partition.add_argument("--out-dir", type=Path, required=True)

    audit = sub.add_parser("audit", help="calculate aggregate one-day diagnostics")
    audit.add_argument("--private", type=Path, required=True)
    audit.add_argument("--matches", type=Path, required=True)
    audit.add_argument("--state-vectors", type=Path, required=True)
    audit.add_argument("--era5-dir", type=Path, required=True)
    audit.add_argument("--out", type=Path, required=True)

    args = parser.parse_args(argv)
    if args.command == "validate":
        result = validate_design(_load_json(args.design), args.release_manifest)
        print(
            f"OpenSky audit: {result['day']}, {result['population_rows']:,} flights, "
            f"design {result['design_sha256']}")
        return 0
    if args.command == "prepare":
        match_value, private_value = prepare_primary_artifacts(
            design_path=args.design, manifest_path=args.release_manifest,
            flights_dir=args.flights_dir, decomposition_dir=args.decomposition_dir,
            ground_dir=args.ground_dir,
            match_output=args.match_out, private_output=args.private_out)
        print(
            f"primary day: {len(match_value['rows']):,} blinded rows; "
            f"{len(private_value['rows']):,} private keys")
        return 0
    if args.command == "match":
        value = write_matches(
            design_path=args.design, primary_path=args.primary,
            source_paths=args.source_flight, output=args.out)
        print(
            f"OpenSky source flights: {value['source_valid_flights']:,}; "
            f"match statuses: {value['match_status_counts']}")
        return 0
    if args.command == "extract":
        source_paths = list(args.source_state_vectors or [])
        filesystem = None
        if args.s3_anonymous_prefix:
            from pyarrow import fs
            filesystem = fs.S3FileSystem(
                anonymous=True, endpoint_override="s3.opensky-network.org",
                scheme="https")
            infos = filesystem.get_file_info(
                fs.FileSelector(args.s3_anonymous_prefix, recursive=True))
            source_paths.extend(sorted(
                info.path for info in infos
                if info.type == fs.FileType.File and info.path.endswith(".parquet")))
        if not source_paths:
            raise UncertaintyError(
                "extract needs --source-state-vectors or --s3-anonymous-prefix")
        value = extract_state_vectors(
            design_path=args.design, matches_path=args.matches,
            source_paths=source_paths, output=args.out, filesystem=filesystem)
        print(f"extracted state vectors: {value['rows']:,}")
        return 0
    if args.command == "partition":
        value = partition_state_vectors(
            design_path=args.design, matches_path=args.matches,
            state_vectors_path=args.state_vectors, output_dir=args.out_dir,
            buckets=args.buckets)
        print(
            f"partitioned state vectors: {value['rows_after_batch_deduplication']:,} "
            f"rows in {value['bucket_files']} buckets")
        return 0
    if args.command == "audit":
        value = run_audit(
            design_path=args.design, manifest_path=args.release_manifest,
            private_path=args.private, matches_path=args.matches,
            state_vectors_path=args.state_vectors, era5_dir=args.era5_dir)
        _atomic_json(args.out, value)
        print(json.dumps(value["matching_status_counts"], sort_keys=True))
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except UncertaintyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
