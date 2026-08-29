#!/usr/bin/env python3
"""Reproducible uncertainty scaffolding for the CO2 release.

This module deliberately keeps three different objects separate:

* exact metrics for the frozen population;
* temporal-composition diagnostics obtained by resampling whole days;
* paired finite-difference scenarios for model sensitivity.

None of those is called a confidence interval.  A probabilistic interval is
blocked until the uncertainty register contains evidence-backed distributions.
The sensitivity runner recomputes the observed stored track and every baseline
with the same scenario parameters, preserving the covariance that makes the gap
more robust than either absolute fuel estimate on its own.

Per-flight sample manifests and intermediate results contain release-local
flight ids.  They must be written outside the repository (the documented
commands use /tmp); only aggregate JSON leaves the runner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import warnings
from collections import defaultdict
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
for _part in ("pipeline", "ingest", "lab"):
    sys.path.insert(0, str(ROOT / _part))
sys.path.insert(0, str(ROOT))

from release_manifest import ReleaseManifest, sha256_file  # noqa: E402


REGISTRY_STATUSES = {
    "quantified", "coverage_measured", "scenario_only",
    "needs_evidence", "out_of_scope",
}
REGISTRY_CATEGORIES = {"observation", "parameter", "model", "selection", "variability"}
GROUND_DEFINITIONS = {"suolo", "a1000t40", "a1000t70", "a1000t100", "a3000t70"}
DISTANCE_EDGES = [-math.inf, 300, 500, 800, 1200, 2000, math.inf]
DISTANCE_LABELS = ["lt300", "300_500", "500_800", "800_1200", "1200_2000", "ge2000"]
COVERAGE_EDGES = [-math.inf, 0.90, 0.99, math.inf]
COVERAGE_LABELS = ["lt090", "090_099", "ge099"]
SELECTION_COLUMNS = (
    "day", "flight_id", "typecode", "origin_icao", "dest_icao",
    "gc_km", "flown_km", "coverage_frac", "max_gap_s",
    "flown_ge_09gc", "co2_kg_v0",
)
SELECTION_GATE_ORDER = (
    "endpoints_resolved", "coverage_sufficient",
    "flown_distance_sufficient", "sector_length_sufficient",
)
METRIC_COLUMNS = (
    "co2_real_tonnes", "co2_ideal_tonnes", "co2_gap_tonnes",
    "gap_total_pct", "gap_lateral_pct", "gap_vertical_pct",
    "gap_total_pct_calibrated",
)
RATIO_METRICS = (
    "gap_total_pct", "gap_lateral_pct", "gap_vertical_pct",
    "gap_total_pct_calibrated",
)
VALIDATION_MATCH_COLUMNS = (
    "dep_ts", "arr_ts", "o_lat", "o_lon", "d_lat", "d_lon",
)
VALIDATION_OUTCOME_STATUSES = {
    "measured", "not_found", "unusable", "source_error",
}
NORMAL_95 = 1.959963984540054


class UncertaintyError(RuntimeError):
    """An uncertainty input or result violates its declared contract."""


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(Path(path).read_text())
    except Exception as exc:
        raise UncertaintyError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise UncertaintyError(f"{path} must contain a JSON object")
    return value


def _atomic_json(path: Path, value: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def _require_outside_repository(path: Path, label: str) -> None:
    resolved = Path(path).resolve()
    try:
        resolved.relative_to(ROOT.resolve())
    except ValueError:
        return
    raise UncertaintyError(
        f"{label} contains per-flight keys and must be outside the repository: {path}")


def _finite_number(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UncertaintyError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise UncertaintyError(f"{label} must be finite")
    return result


def _unique_ids(items: list, label: str) -> set[str]:
    if not isinstance(items, list) or not items:
        raise UncertaintyError(f"{label} must be a non-empty list")
    ids = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise UncertaintyError(f"{label}[{index}] has no string id")
        ids.append(item["id"])
    if len(ids) != len(set(ids)):
        raise UncertaintyError(f"{label} contains duplicate ids")
    return set(ids)


def validate_registry(data: dict) -> dict:
    """Validate the machine-readable uncertainty register."""
    if data.get("schema_version") != 1:
        raise UncertaintyError("uncertainty register schema_version must be 1")
    estimand_ids = _unique_ids(data.get("estimands"), "estimands")
    source_ids = _unique_ids(data.get("sources"), "sources")
    for item in data["estimands"]:
        if not isinstance(item.get("description"), str) or not item["description"].strip():
            raise UncertaintyError(f"estimand {item['id']} lacks a description")
    for source in data["sources"]:
        sid = source["id"]
        if source.get("category") not in REGISTRY_CATEGORIES:
            raise UncertaintyError(f"source {sid} has invalid category")
        if source.get("status") not in REGISTRY_STATUSES:
            raise UncertaintyError(f"source {sid} has invalid status")
        affects = source.get("affects")
        if not isinstance(affects, list) or not affects:
            raise UncertaintyError(f"source {sid} has no affected estimands")
        unknown = sorted(set(affects) - estimand_ids)
        if unknown:
            raise UncertaintyError(f"source {sid} names unknown estimands: {unknown}")
        for field in ("correlation_scope", "evidence", "propagation", "notes"):
            if not isinstance(source.get(field), str) or not source[field].strip():
                raise UncertaintyError(f"source {sid} lacks {field}")
        if (source["status"] in {"quantified", "coverage_measured"}
                and source.get("range") is None):
            raise UncertaintyError(f"measured source {sid} has no range")
    return {"estimands": len(estimand_ids), "sources": len(source_ids)}


def validate_scenarios(data: dict) -> dict:
    """Validate finite-difference scenarios without blessing them as bounds."""
    if data.get("schema_version") != 1:
        raise UncertaintyError("scenario schema_version must be 1")
    if data.get("publication_status") != "diagnostic_only":
        raise UncertaintyError("scenarios must remain diagnostic_only")
    scenario_ids = _unique_ids(data.get("scenarios"), "scenarios")
    nominal = data.get("nominal")
    if nominal not in scenario_ids:
        raise UncertaintyError("nominal scenario id does not exist")
    for scenario in data["scenarios"]:
        sid = scenario["id"]
        load = _finite_number(scenario.get("load_factor"), f"{sid}.load_factor")
        reserve = _finite_number(scenario.get("reserve_kg"), f"{sid}.reserve_kg")
        offset = _finite_number(
            scenario.get("cruise_alt_offset_ft"), f"{sid}.cruise_alt_offset_ft")
        if not 0.0 <= load <= 1.0:
            raise UncertaintyError(f"{sid}.load_factor must be between 0 and 1")
        if reserve < 0:
            raise UncertaintyError(f"{sid}.reserve_kg must be non-negative")
        if abs(offset) > 10000:
            raise UncertaintyError(f"{sid}.cruise_alt_offset_ft is not a sensitivity step")
        if scenario.get("real_tas_mode") not in {"ias", "gs"}:
            raise UncertaintyError(f"{sid}.real_tas_mode must be ias or gs")
        if scenario.get("ground_definition") not in GROUND_DEFINITIONS:
            raise UncertaintyError(f"{sid}.ground_definition is unknown")
        if not isinstance(scenario.get("purpose"), str) or not scenario["purpose"].strip():
            raise UncertaintyError(f"{sid} lacks a purpose")
    return {"scenarios": len(scenario_ids), "nominal": nominal}


def validate_selection_design(data: dict, release_manifest: Path) -> dict:
    """Validate the aggregate pre-registration without reading private rows."""
    if (data.get("schema_version"), data.get("kind")) != (
            1, "co2gap-selection-validation-design"):
        raise UncertaintyError("unknown selection-validation design contract")
    if data.get("publication_status") != "aggregate_preregistration":
        raise UncertaintyError(
            "selection-validation design must remain aggregate_preregistration")
    if data.get("analysis_status") != "awaiting_independent_outcomes":
        raise UncertaintyError(
            "selection-validation design cannot claim outcomes before validation")
    if data.get("primary_headline_bias_bounded") is not False:
        raise UncertaintyError(
            "selection-validation design cannot claim the headline bias is bounded")
    if data.get("release_manifest_sha256") != sha256_file(release_manifest):
        raise UncertaintyError(
            "selection-validation design names another release manifest")
    integer_fields = ("population_rows", "sample_rows", "strata")
    for field in integer_fields:
        if (not isinstance(data.get(field), int) or isinstance(data.get(field), bool)
                or data[field] <= 0):
            raise UncertaintyError(
                f"selection-validation design has invalid {field}")
    if data["sample_rows"] > data["population_rows"]:
        raise UncertaintyError(
            "selection-validation sample exceeds its population")
    maximum_weight = _finite_number(
        data.get("maximum_weight"), "selection design maximum_weight")
    effective = _finite_number(
        data.get("kish_effective_sample_size"),
        "selection design kish_effective_sample_size")
    if maximum_weight < 1 or not 0 < effective <= data["sample_rows"]:
        raise UncertaintyError(
            "selection-validation design has impossible weight diagnostics")
    parameters = data.get("parameters")
    if not isinstance(parameters, dict):
        raise UncertaintyError("selection-validation design lacks parameters")
    if parameters.get("target_sample_rows") != data["sample_rows"]:
        raise UncertaintyError(
            "selection-validation target differs from sample rows")
    common_types = parameters.get("common_types")
    if (not isinstance(common_types, list)
            or len(common_types) != parameters.get("top_types")
            or len(common_types) != len(set(common_types))):
        raise UncertaintyError(
            "selection-validation common aircraft types do not close")
    masks = data.get("by_failure_mask")
    if not isinstance(masks, list) or not masks:
        raise UncertaintyError("selection-validation design lacks failure masks")
    names = [row.get("failure_mask") for row in masks if isinstance(row, dict)]
    if (len(names) != len(masks) or len(names) != len(set(names))
            or any(len(str(name)) != 4 or set(str(name)) - {"0", "1"}
                   for name in names)):
        raise UncertaintyError("selection-validation failure masks are invalid")
    for row in masks:
        if (not isinstance(row.get("population_rows"), int)
                or not isinstance(row.get("sample_rows"), int)
                or not 0 < row["sample_rows"] <= row["population_rows"]):
            raise UncertaintyError(
                "selection-validation failure-mask counts are invalid")
        if row.get("gate_pass") is not (row["failure_mask"] == "0000"):
            raise UncertaintyError(
                "selection-validation failure mask contradicts gate status")
    if sum(row["population_rows"] for row in masks) != data["population_rows"]:
        raise UncertaintyError(
            "selection-validation failure masks do not close on population")
    if sum(row["sample_rows"] for row in masks) != data["sample_rows"]:
        raise UncertaintyError(
            "selection-validation failure masks do not close on sample")
    hashes = data.get("private_artifact_sha256")
    if not isinstance(hashes, dict):
        raise UncertaintyError(
            "selection-validation design lacks private artifact hashes")
    for name in ("sample", "match_list"):
        value = hashes.get(name)
        if (not isinstance(value, str) or len(value) != 64
                or set(value) - set("0123456789abcdef")):
            raise UncertaintyError(
                f"selection-validation design has invalid {name} hash")
    outcome_contract = data.get("outcome_contract")
    if not isinstance(outcome_contract, str) or not outcome_contract.strip():
        raise UncertaintyError(
            "selection-validation design lacks an outcome contract")
    outcome_path = ROOT / outcome_contract
    if (not outcome_path.is_file()
            or data.get("outcome_contract_sha256") != sha256_file(outcome_path)):
        raise UncertaintyError(
            "selection-validation outcome contract differs from pre-registration")
    return {
        "population_rows": data["population_rows"],
        "sample_rows": data["sample_rows"],
        "strata": data["strata"],
    }


def verify_registered_selection_artifacts(
        design: dict, *, sample_path: Path, match_path: Path) -> None:
    """Require regenerated private files to match the tracked registration."""
    hashes = design["private_artifact_sha256"]
    if sha256_file(sample_path) != hashes["sample"]:
        raise UncertaintyError(
            "private selection-validation sample differs from pre-registration")
    if sha256_file(match_path) != hashes["match_list"]:
        raise UncertaintyError(
            "private selection-validation match list differs from pre-registration")


def _require_columns(frame: pd.DataFrame, columns: Iterable[str], label: str) -> None:
    missing = sorted(set(columns) - set(frame.columns))
    if missing:
        raise UncertaintyError(f"{label} lacks required columns: {missing}")


def stratified_sample(population: pd.DataFrame, per_stratum: int, seed: int) -> pd.DataFrame:
    """Return a deterministic equal-allocation sample with expansion weights."""
    if per_stratum <= 0:
        raise UncertaintyError("per_stratum must be positive")
    _require_columns(
        population, ("day", "flight_id", "typecode", "gc_km", "coverage_frac"),
        "sample population")
    pop = population.copy()
    keys = list(zip(pop.day.astype(str), pop.flight_id.astype(int)))
    if len(keys) != len(set(keys)):
        raise UncertaintyError("sample population has duplicate (day, flight_id) keys")
    pop["day"] = pop.day.astype(str)
    pop["flight_id"] = pop.flight_id.astype(int)
    pop["distance_band"] = pd.cut(
        pd.to_numeric(pop.gc_km, errors="coerce"), DISTANCE_EDGES,
        labels=DISTANCE_LABELS).astype(str)
    pop["coverage_band"] = pd.cut(
        pd.to_numeric(pop.coverage_frac, errors="coerce"), COVERAGE_EDGES,
        labels=COVERAGE_LABELS).astype(str)
    if pop[["gc_km", "coverage_frac"]].isna().any().any():
        raise UncertaintyError("sample population contains non-numeric distance or coverage")
    pop["stratum"] = (
        pop.typecode.fillna("UNKNOWN").astype(str) + "|" + pop.distance_band
        + "|" + pop.coverage_band)
    rng = np.random.default_rng(seed)
    selected = []
    for stratum, group in pop.groupby("stratum", sort=True):
        ordered = group.sort_values(["day", "flight_id"])
        n_population = len(ordered)
        n_sample = min(per_stratum, n_population)
        positions = np.sort(rng.choice(n_population, size=n_sample, replace=False))
        chosen = ordered.iloc[positions].copy()
        chosen["population_n"] = n_population
        chosen["sample_n"] = n_sample
        chosen["weight"] = n_population / n_sample
        selected.append(chosen)
    if not selected:
        raise UncertaintyError("sample population is empty")
    result = pd.concat(selected, ignore_index=True)
    result = result.sort_values(["day", "flight_id"]).reset_index(drop=True)
    expanded = float(result.weight.sum())
    if not math.isclose(expanded, float(len(pop)), rel_tol=0, abs_tol=1e-8):
        raise UncertaintyError(
            f"sample weights expand to {expanded}, expected {len(pop)}")
    return result


def build_population(manifest: ReleaseManifest, flights_dir: Path,
                     decomposition_dir: Path) -> pd.DataFrame:
    """Join quality metadata onto the already quality-gated decomposition set."""
    frames = []
    for day in manifest.days:
        dec_path = decomposition_dir / f"{day}.parquet"
        flight_path = flights_dir / day / "flights.parquet"
        try:
            dec = pq.read_table(
                dec_path, columns=["day", "flight_id", "typecode", "gc_km"]).to_pandas()
            flights = pq.read_table(
                flight_path, columns=["flight_id", "coverage_frac", "max_gap_s"]
            ).to_pandas()
        except Exception as exc:
            raise UncertaintyError(f"cannot read sample input for {day}: {exc}") from exc
        if dec.flight_id.duplicated().any() or flights.flight_id.duplicated().any():
            raise UncertaintyError(f"duplicate flight_id in sample input for {day}")
        joined = dec.merge(flights, on="flight_id", how="left", validate="one_to_one")
        if joined[["coverage_frac", "max_gap_s"]].isna().any().any():
            raise UncertaintyError(f"decomposition keys missing from flights for {day}")
        frames.append(joined)
    population = pd.concat(frames, ignore_index=True)
    if sorted(population.day.unique()) != manifest.days:
        raise UncertaintyError("sample population differs from release day perimeter")
    return population


def selection_flags(frame: pd.DataFrame, *, coverage_min: float,
                    gc_min_km: float) -> pd.DataFrame:
    """Rebuild the four release-gate predicates without hiding overlap."""
    _require_columns(frame, SELECTION_COLUMNS, "pre-gate flight population")
    return pd.DataFrame({
        "endpoints_resolved": (
            frame.origin_icao.notna() & frame.dest_icao.notna()),
        "coverage_sufficient": (
            pd.to_numeric(frame.coverage_frac, errors="coerce") >= coverage_min),
        "flown_distance_sufficient": (
            frame.flown_ge_09gc.fillna(False).astype(bool)),
        "sector_length_sufficient": (
            pd.to_numeric(frame.gc_km, errors="coerce") >= gc_min_km),
    }, index=frame.index)


def _selection_activity(frame: pd.DataFrame) -> dict[str, float | int]:
    """Summarise observable exposure; CO2 remains the first-pass inventory."""
    values = {}
    for column in ("gc_km", "flown_km", "co2_kg_v0"):
        series = pd.to_numeric(frame[column], errors="coerce").to_numpy(float)
        if not np.isfinite(series).all() or (series < 0).any():
            raise UncertaintyError(
                f"selection population contains invalid {column} values")
        values[column] = float(series.sum())
    return {
        "flights": int(len(frame)),
        "great_circle_km": values["gc_km"],
        "flown_km": values["flown_km"],
        "first_pass_gate_to_gate_co2_tonnes": values["co2_kg_v0"] / 1000.0,
    }


def _activity_ratio(numerator: dict, denominator: dict) -> dict[str, float]:
    result = {}
    for key, value in numerator.items():
        base = denominator[key]
        if base <= 0:
            raise UncertaintyError(f"selection denominator {key} is not positive")
        result[key] = float(value / base)
    return result


def _selection_group_rows(frame: pd.DataFrame, flags: pd.DataFrame,
                          group: pd.Series, *, min_n: int) -> list[dict]:
    working = frame[["gc_km", "flown_km", "co2_kg_v0"]].copy()
    working["_group"] = group.astype("string").fillna("UNKNOWN")
    working["_pass"] = flags.all(axis=1).to_numpy()
    rows = []
    for name, subset in working.groupby("_group", sort=True, dropna=False):
        if len(subset) < min_n:
            continue
        source = _selection_activity(subset)
        retained = _selection_activity(subset.loc[subset._pass])
        rows.append({
            "group": str(name),
            "source": source,
            "retained": retained,
            "retained_share": _activity_ratio(retained, source),
        })
    return rows


def _selection_bands(frame: pd.DataFrame) -> dict[str, pd.Series]:
    return {
        "distance_band_km": pd.cut(
            pd.to_numeric(frame.gc_km, errors="coerce"),
            [-math.inf, 150, 300, 500, 800, 1200, 2000, math.inf],
            labels=["lt150", "150_300", "300_500", "500_800",
                    "800_1200", "1200_2000", "ge2000"], right=False),
        "coverage_band": pd.cut(
            pd.to_numeric(frame.coverage_frac, errors="coerce"),
            [-math.inf, 0.50, 0.85, 0.95, 0.99, math.inf],
            labels=["lt050", "050_085", "085_095", "095_099", "ge099"],
            right=False),
        "maximum_gap_band_s": pd.cut(
            pd.to_numeric(frame.max_gap_s, errors="coerce"),
            [-math.inf, 120, 300, 600, 900, math.inf],
            labels=["lt120", "120_300", "300_600", "600_900", "ge900"],
            right=False),
    }


def selection_audit(*, manifest_path: Path, flights_dir: Path,
                    decomposition_dir: Path, min_group_n: int,
                    verify: bool) -> dict:
    """Measure release-gate selection conditional on durable source flights."""
    if min_group_n < 10:
        raise UncertaintyError("selection groups must aggregate at least 10 flights")
    import track_quality

    manifest = ReleaseManifest.load(manifest_path)
    manifest.verify_track_quality(track_quality)
    if verify:
        manifest.verify_set("flights", flights_dir)
        manifest.verify_set("decomposition", decomposition_dir, artifact=True)

    frames = []
    decomposition_rows = 0
    for day in manifest.days:
        flight_path = flights_dir / day / "flights.parquet"
        dec_path = decomposition_dir / f"{day}.parquet"
        try:
            flights = pq.read_table(
                flight_path, columns=list(SELECTION_COLUMNS)).to_pandas()
            dec = pq.read_table(dec_path, columns=["flight_id"]).to_pandas()
        except Exception as exc:
            raise UncertaintyError(f"cannot read selection input for {day}: {exc}") from exc
        if flights.flight_id.duplicated().any() or dec.flight_id.duplicated().any():
            raise UncertaintyError(f"duplicate flight_id in selection input for {day}")
        if set(flights.day.astype(str)) != {day}:
            raise UncertaintyError(f"flight table identifies another day: {day}")
        flags = selection_flags(
            flights, coverage_min=track_quality.COV_MIN,
            gc_min_km=track_quality.GC_MIN_KM)
        expected = set(flights.loc[flags.all(axis=1), "flight_id"].astype(int))
        actual = set(dec.flight_id.astype(int))
        missing, extra = expected - actual, actual - expected
        if missing or extra:
            raise UncertaintyError(
                f"{day}: release decomposition differs from rebuilt quality gate: "
                f"{len(missing)} missing, {len(extra)} extra")
        frames.append(flights)
        decomposition_rows += len(dec)

    population = pd.concat(frames, ignore_index=True)
    flags = selection_flags(
        population, coverage_min=track_quality.COV_MIN,
        gc_min_km=track_quality.GC_MIN_KM)
    passed = flags.all(axis=1)
    source = _selection_activity(population)
    retained = _selection_activity(population.loc[passed])
    excluded = _selection_activity(population.loc[~passed])

    independent = []
    for criterion in SELECTION_GATE_ORDER:
        failed = ~flags[criterion]
        activity = _selection_activity(population.loc[failed])
        independent.append({
            "criterion": criterion,
            "failed": activity,
            "failed_share_of_source": _activity_ratio(activity, source),
            "overlaps_other_failures": True,
        })

    combinations = []
    masks = flags.apply(
        lambda row: "".join("0" if bool(value) else "1" for value in row), axis=1)
    for mask, indexes in masks.groupby(masks, sort=True).groups.items():
        subset = population.loc[indexes]
        combinations.append({
            "failure_mask": str(mask),
            "failed_criteria": [
                name for name, bit in zip(SELECTION_GATE_ORDER, mask) if bit == "1"],
            "activity": _selection_activity(subset),
        })

    cumulative = np.ones(len(population), dtype=bool)
    cascade = [{"stage": "stored_modelled_complete_flights", "activity": source}]
    for criterion in SELECTION_GATE_ORDER:
        cumulative &= flags[criterion].to_numpy(bool)
        cascade.append({
            "stage": f"after_{criterion}",
            "activity": _selection_activity(population.loc[cumulative]),
        })

    bands = _selection_bands(population)
    grouped = {
        "day": _selection_group_rows(
            population, flags, population.day, min_n=min_group_n),
        "aircraft_type": _selection_group_rows(
            population, flags, population.typecode, min_n=min_group_n),
    }
    for name, values in bands.items():
        grouped[name] = _selection_group_rows(
            population, flags, values, min_n=min_group_n)
    day_shares = np.asarray([
        row["retained_share"]["flights"] for row in grouped["day"]], dtype=float)
    day_quantiles = np.quantile(day_shares, [0.0, 0.05, 0.50, 0.95, 1.0])
    lowest_days = sorted(
        ({"day": row["group"], **row["retained_share"]}
         for row in grouped["day"]),
        key=lambda row: row["flights"],
    )[:10]

    return {
        "schema_version": 1,
        "kind": "co2gap-selection-audit",
        "release_id": manifest.release_id,
        "release_manifest_sha256": sha256_file(manifest.path),
        "source_manifest_verified": bool(verify),
        "population_boundary": {
            "denominator": (
                "Durable flights produced after regional trace filtering, complete-flight "
                "reconstruction, OpenAP type support and successful first-pass fuel modelling."
            ),
            "upstream_reconstructible_for_release": False,
            "unobserved_upstream_stages": [
                "global trace members outside the geographic box",
                "legs rejected as incomplete",
                "complete flights with unsupported aircraft types",
                "complete supported flights whose fuel model failed",
            ],
            "interpretation": (
                "Retention shares are exact conditional coverage of the durable pre-gate "
                "population, not coverage of all flights in the ECAC airspace."
            ),
        },
        "gate": {
            "criteria_order": list(SELECTION_GATE_ORDER),
            "coverage_min_fraction": float(track_quality.COV_MIN),
            "flown_min_fraction": float(track_quality.FLOWN_MIN_FRAC),
            "great_circle_min_km": float(track_quality.GC_MIN_KM),
            "exact_keyset_match_to_decomposition": True,
            "decomposition_rows": int(decomposition_rows),
        },
        "coverage_statement": {
            "source": source,
            "retained": retained,
            "excluded": excluded,
            "retained_share": _activity_ratio(retained, source),
            "estimand_bias_bounded": False,
        },
        "independent_failures": independent,
        "failure_combinations": combinations,
        "sequential_cascade": {
            "order_is_diagnostic_not_causal": True,
            "stages": cascade,
        },
        "group_diagnostics": {
            "minimum_group_size": int(min_group_n),
            "privacy": "Aggregate rows only; no flight identifiers are emitted.",
            "day_flight_retention": {
                "min": float(day_quantiles[0]),
                "q05": float(day_quantiles[1]),
                "median": float(day_quantiles[2]),
                "q95": float(day_quantiles[3]),
                "max": float(day_quantiles[4]),
                "lowest_ten_days": lowest_days,
            },
            **grouped,
        },
        "limitations": [
            "Independent failure counts overlap; use failure_combinations for an exact partition.",
            "First-pass gate-to-gate CO2 is an exposure proxy here, not the published airborne estimate.",
            "Retention coverage does not bound headline bias: rejected flights have no trustworthy decomposition.",
            "Airport comparisons cannot recover the airport of an unresolved endpoint.",
            "Historical upstream attrition cannot be reconstructed from the frozen source tables.",
        ],
    }


def write_sample_manifest(*, manifest: ReleaseManifest, population: pd.DataFrame,
                          sample: pd.DataFrame, per_stratum: int, seed: int,
                          output: Path, verified: bool) -> dict:
    _require_outside_repository(output, "uncertainty sample manifest")
    rows = []
    for row in sample.itertuples(index=False):
        rows.append({
            "day": str(row.day), "flight_id": int(row.flight_id),
            "stratum": str(row.stratum), "population_n": int(row.population_n),
            "sample_n": int(row.sample_n), "weight": float(row.weight),
        })
    value = {
        "schema_version": 1,
        "kind": "co2gap-uncertainty-sample",
        "release_id": manifest.release_id,
        "release_manifest_sha256": sha256_file(manifest.path),
        "source_manifest_verified": bool(verified),
        "seed": int(seed),
        "per_stratum": int(per_stratum),
        "population_rows": int(len(population)),
        "sample_rows": int(len(sample)),
        "strata": int(sample.stratum.nunique()),
        "weight_sum": float(sample.weight.sum()),
        "max_weight": float(sample.weight.max()),
        "kish_effective_sample_size": float(
            sample.weight.sum() ** 2 / np.square(sample.weight).sum()),
        "dimensions": ["typecode", "distance_band", "coverage_band"],
        "privacy": (
            "Contains release-local per-flight keys; keep outside git and "
            "publish aggregates only."
        ),
        "rows": rows,
    }
    _atomic_json(output, value)
    return value


def build_selection_validation_population(
        manifest: ReleaseManifest, flights_dir: Path,
        decomposition_dir: Path, *, verify: bool) -> pd.DataFrame:
    """Build the exact durable pre-gate frame for independent validation."""
    import track_quality

    manifest.verify_track_quality(track_quality)
    if verify:
        manifest.verify_set("flights", flights_dir)
        manifest.verify_set("decomposition", decomposition_dir, artifact=True)
    columns = list(dict.fromkeys(SELECTION_COLUMNS + VALIDATION_MATCH_COLUMNS))
    frames = []
    for day in manifest.days:
        flight_path = flights_dir / day / "flights.parquet"
        dec_path = decomposition_dir / f"{day}.parquet"
        try:
            flights = pq.read_table(flight_path, columns=columns).to_pandas()
            decomposition = pq.read_table(
                dec_path, columns=["flight_id"]).to_pandas()
        except Exception as exc:
            raise UncertaintyError(
                f"cannot read selection-validation input for {day}: {exc}") from exc
        if flights.flight_id.duplicated().any():
            raise UncertaintyError(
                f"duplicate source flight_id in selection validation for {day}")
        if decomposition.flight_id.duplicated().any():
            raise UncertaintyError(
                f"duplicate decomposition flight_id in selection validation for {day}")
        if set(flights.day.astype(str)) != {day}:
            raise UncertaintyError(
                f"selection-validation flight table identifies another day: {day}")
        flags = selection_flags(
            flights, coverage_min=track_quality.COV_MIN,
            gc_min_km=track_quality.GC_MIN_KM)
        expected = set(flights.loc[flags.all(axis=1), "flight_id"].astype(int))
        actual = set(decomposition.flight_id.astype(int))
        if expected != actual:
            raise UncertaintyError(
                f"{day}: selection-validation gate differs from release population: "
                f"{len(expected - actual)} missing, {len(actual - expected)} extra")
        frame = flights.copy()
        frame["failure_mask"] = flags.apply(
            lambda row: "".join("0" if bool(value) else "1" for value in row),
            axis=1)
        frame["gate_pass"] = flags.all(axis=1).to_numpy(bool)
        bands = _selection_bands(frame)
        frame["distance_band"] = bands["distance_band_km"].astype(
            "string").fillna("UNKNOWN")
        frame["coverage_band"] = bands["coverage_band"].astype(
            "string").fillna("UNKNOWN")
        frames.append(frame)
    population = pd.concat(frames, ignore_index=True)
    keys = list(zip(population.day.astype(str), population.flight_id.astype(int)))
    if len(keys) != len(set(keys)):
        raise UncertaintyError("selection-validation population has duplicate keys")
    numeric = population[list(VALIDATION_MATCH_COLUMNS)].apply(
        pd.to_numeric, errors="coerce").to_numpy(float)
    if not np.isfinite(numeric).all():
        raise UncertaintyError(
            "selection-validation population has invalid matching coordinates or times")
    if (pd.to_numeric(population.arr_ts) <=
            pd.to_numeric(population.dep_ts)).any():
        raise UncertaintyError(
            "selection-validation population has non-positive flight duration")
    return population


def stratified_selection_validation_sample(
        population: pd.DataFrame, *, per_stratum: int, top_types: int,
        target_sample: int, seed: int,
        namespace: str) -> tuple[pd.DataFrame, list[str]]:
    """Draw a minimum-plus-proportional SRS inside validation cells."""
    if per_stratum < 2:
        raise UncertaintyError(
            "selection-validation per-stratum must be at least 2 for variance estimation")
    if top_types < 1:
        raise UncertaintyError("selection-validation top-types must be positive")
    required = (
        "day", "flight_id", "typecode", "failure_mask", "gate_pass",
        "distance_band", "coverage_band", *VALIDATION_MATCH_COLUMNS,
    )
    _require_columns(population, required, "selection-validation population")
    pop = population.copy()
    pop["typecode"] = pop.typecode.astype("string").fillna("UNKNOWN")
    counts = pop.typecode.value_counts(dropna=False)
    common_types = [
        str(name) for name, _ in sorted(
            counts.items(), key=lambda item: (-int(item[1]), str(item[0])))[:top_types]
    ]
    pop["type_group"] = pop.typecode.where(
        pop.typecode.isin(common_types), "OTHER")
    pop["stratum"] = (
        pop.failure_mask.astype(str) + "|" + pop.distance_band.astype(str)
        + "|" + pop.coverage_band.astype(str) + "|" + pop.type_group.astype(str))
    stratum_sizes = pop.groupby("stratum", sort=True).size().astype(int)
    allocation = stratum_sizes.clip(upper=per_stratum).astype(int)
    minimum_total = int(allocation.sum())
    target_sample = min(int(target_sample), len(pop))
    if target_sample < minimum_total:
        raise UncertaintyError(
            f"selection-validation target-sample {target_sample} is below the "
            f"{minimum_total}-row stratum minimum")
    remaining = target_sample - minimum_total
    capacity = stratum_sizes - allocation
    while remaining:
        total_capacity = int(capacity.sum())
        if total_capacity <= 0:
            raise UncertaintyError(
                "selection-validation allocation cannot reach its target")
        quotas = capacity.astype(float) * remaining / total_capacity
        additions = np.floor(quotas).astype(int).clip(upper=capacity)
        used = int(additions.sum())
        if used == 0:
            order = sorted(
                capacity[capacity > 0].index,
                key=lambda name: (
                    -(float(quotas[name]) - math.floor(float(quotas[name]))),
                    -int(capacity[name]), str(name)),
            )
            for name in order[:remaining]:
                additions[name] = 1
            used = int(additions.sum())
        allocation += additions
        capacity = stratum_sizes - allocation
        remaining -= used
    if int(allocation.sum()) != target_sample:
        raise UncertaintyError("selection-validation allocation does not close")
    rng = np.random.default_rng(seed)
    selected = []
    for stratum, group in pop.groupby("stratum", sort=True):
        ordered = group.sort_values(["day", "flight_id"])
        n_population = len(ordered)
        n_sample = int(allocation[stratum])
        positions = np.sort(
            rng.choice(n_population, size=n_sample, replace=False))
        chosen = ordered.iloc[positions].copy()
        chosen["population_n"] = n_population
        chosen["sample_n"] = n_sample
        chosen["weight"] = n_population / n_sample
        selected.append(chosen)
    if not selected:
        raise UncertaintyError("selection-validation population is empty")
    sample = pd.concat(selected, ignore_index=True)
    sample["sample_id"] = [
        hashlib.sha256(
            f"{namespace}\0{seed}\0{day}\0{int(fid)}".encode()).hexdigest()[:24]
        for day, fid in zip(sample.day.astype(str), sample.flight_id)
    ]
    if sample.sample_id.duplicated().any():
        raise UncertaintyError("selection-validation sample id collision")
    sample = sample.sort_values(["day", "flight_id"]).reset_index(drop=True)
    if not math.isclose(
            float(sample.weight.sum()), float(len(pop)), rel_tol=0, abs_tol=1e-8):
        raise UncertaintyError(
            "selection-validation weights do not expand to the population")
    return sample, common_types


def write_selection_validation_artifacts(
        *, manifest: ReleaseManifest, population: pd.DataFrame,
        sample: pd.DataFrame, common_types: list[str], per_stratum: int,
        top_types: int, target_sample: int, seed: int, output: Path,
        match_output: Path,
        verified: bool) -> tuple[dict, dict]:
    """Write separate private design and blinded matching manifests."""
    _require_outside_repository(output, "selection-validation sample")
    _require_outside_repository(match_output, "selection-validation match list")
    if output.resolve() == match_output.resolve():
        raise UncertaintyError(
            "selection-validation sample and match list must be different files")
    design_rows = []
    match_rows = []
    for row in sample.itertuples(index=False):
        design_rows.append({
            "sample_id": str(row.sample_id),
            "day": str(row.day),
            "flight_id": int(row.flight_id),
            "failure_mask": str(row.failure_mask),
            "gate_pass": bool(row.gate_pass),
            "stratum": str(row.stratum),
            "distance_band": str(row.distance_band),
            "coverage_band": str(row.coverage_band),
            "type_group": str(row.type_group),
            "population_n": int(row.population_n),
            "sample_n": int(row.sample_n),
            "weight": float(row.weight),
        })
        match_rows.append({
            "sample_id": str(row.sample_id),
            "day": str(row.day),
            "typecode": str(row.typecode),
            "dep_ts": int(row.dep_ts),
            "arr_ts": int(row.arr_ts),
            "o_lat": float(row.o_lat), "o_lon": float(row.o_lon),
            "d_lat": float(row.d_lat), "d_lon": float(row.d_lon),
        })
    by_mask = []
    for mask, group in sample.groupby("failure_mask", sort=True):
        by_mask.append({
            "failure_mask": str(mask),
            "gate_pass": bool(group.gate_pass.iloc[0]),
            "sample_rows": int(len(group)),
            "expanded_population_rows": float(group.weight.sum()),
        })
    value = {
        "schema_version": 1,
        "kind": "co2gap-selection-validation-sample",
        "publication_status": "private_per_flight",
        "analysis_status": "awaiting_independent_outcomes",
        "release_id": manifest.release_id,
        "release_manifest_sha256": sha256_file(manifest.path),
        "source_manifest_verified": bool(verified),
        "seed": int(seed),
        "population_boundary": (
            "Durable complete, supported and successfully modelled pre-gate flights; "
            "upstream ingestion exclusions remain outside this sample."
        ),
        "population_rows": int(len(population)),
        "sample_rows": int(len(sample)),
        "strata": int(sample.stratum.nunique()),
        "weight_sum": float(sample.weight.sum()),
        "max_weight": float(sample.weight.max()),
        "kish_effective_sample_size": float(
            sample.weight.sum() ** 2 / np.square(sample.weight).sum()),
        "design": {
            "method": "simple random sample without replacement inside every stratum",
            "allocation": (
                "minimum per stratum, then proportional to remaining population"),
            "dimensions": [
                "quality-gate failure mask", "great-circle distance band",
                "coverage band", "aircraft type group",
            ],
            "failure_mask_order": list(SELECTION_GATE_ORDER),
            "minimum_per_stratum": int(per_stratum),
            "target_sample_rows": int(target_sample),
            "top_types_requested": int(top_types),
            "common_types": common_types,
            "other_type_group": "OTHER",
            "variance_estimable": True,
        },
        "by_failure_mask": by_mask,
        "outcome_contract": {
            "required_components_kg": [
                "real_co2_kg", "ideal_co2_kg", "hybrid_co2_kg"],
            "required_match_diagnostics": [
                "match_candidate_count=1", "departure_time_delta_s",
                "arrival_time_delta_s", "origin_distance_km",
                "destination_distance_km", "proxy_coverage_fraction",
                "proxy_quality_pass=true",
            ],
            "required_source_declarations": [
                "independent_of_primary_adsb_lol=true",
                "primary_trajectory_used=false",
                "matching_received_gate_status=false",
                "quality_rule",
            ],
            "complete_sample_required_for_estimation": True,
        },
        "privacy": (
            "Contains release-local flight keys and sampling weights. Keep outside git; "
            "publish only aggregate validation results."
        ),
        "rows": design_rows,
    }
    _atomic_json(output, value)
    sample_hash = sha256_file(output)
    match_value = {
        "schema_version": 1,
        "kind": "co2gap-selection-validation-match-list",
        "publication_status": "private_per_flight",
        "release_id": manifest.release_id,
        "sample_sha256": sample_hash,
        "gate_status_disclosed": False,
        "primary_track_quality_disclosed": False,
        "matching_fields": [
            "day", "typecode", "dep_ts", "arr_ts",
            "o_lat", "o_lon", "d_lat", "d_lon",
        ],
        "privacy": (
            "Times and endpoints can identify a flight. Share only with the independent "
            "matcher under the validation protocol and do not publish."
        ),
        "rows": match_rows,
    }
    _atomic_json(match_output, match_value)
    return value, match_value


def _proxy_metrics(totals: np.ndarray) -> dict[str, float]:
    real, ideal, hybrid = (float(value) for value in totals)
    if not all(math.isfinite(value) and value >= 0 for value in totals):
        raise UncertaintyError("independent proxy totals must be finite and non-negative")
    if ideal <= 0:
        raise UncertaintyError("independent proxy ideal CO2 must be positive")
    return {
        "co2_real_tonnes": real / 1000.0,
        "co2_ideal_tonnes": ideal / 1000.0,
        "co2_gap_tonnes": (real - ideal) / 1000.0,
        "gap_total_pct": (real - ideal) / ideal * 100.0,
        "gap_lateral_pct": (hybrid - ideal) / ideal * 100.0,
        "gap_vertical_pct": (real - hybrid) / ideal * 100.0,
    }


def _proxy_ratio_gradient(totals: np.ndarray, metric: str) -> np.ndarray:
    real, ideal, hybrid = (float(value) for value in totals)
    if ideal <= 0:
        raise UncertaintyError("cannot differentiate a proxy ratio with zero ideal CO2")
    if metric == "gap_total_pct":
        return np.asarray([100.0 / ideal, -100.0 * real / ideal ** 2, 0.0])
    if metric == "gap_lateral_pct":
        return np.asarray([0.0, -100.0 * hybrid / ideal ** 2, 100.0 / ideal])
    if metric == "gap_vertical_pct":
        return np.asarray([
            100.0 / ideal, -100.0 * (real - hybrid) / ideal ** 2,
            -100.0 / ideal,
        ])
    raise UncertaintyError(f"unknown independent proxy ratio: {metric}")


def _complete_proxy_estimate(joined: pd.DataFrame) -> dict:
    """Estimate full-minus-passing ratios and design-only sampling variance."""
    totals = np.zeros(6, dtype=float)
    covariance = np.zeros((6, 6), dtype=float)
    for stratum, group in joined.groupby("stratum", sort=True):
        population_values = group.population_n.unique()
        sample_values = group.sample_n.unique()
        if len(population_values) != 1 or len(sample_values) != 1:
            raise UncertaintyError(
                f"inconsistent selection-validation design metadata in {stratum}")
        population_n, sample_n = int(population_values[0]), int(sample_values[0])
        if len(group) != sample_n or not 0 < sample_n <= population_n:
            raise UncertaintyError(
                f"selection-validation sample count does not close in {stratum}")
        outcome = group[[
            "real_co2_kg", "ideal_co2_kg", "hybrid_co2_kg"]].to_numpy(float)
        passed = group.gate_pass.to_numpy(bool)[:, None]
        vector = np.concatenate([outcome, outcome * passed], axis=1)
        totals += population_n * vector.mean(axis=0)
        if sample_n < population_n:
            if sample_n < 2:
                raise UncertaintyError(
                    f"selection-validation variance is not estimable in {stratum}")
            covariance += (
                population_n ** 2 * (1.0 - sample_n / population_n)
                * np.cov(vector, rowvar=False, ddof=1) / sample_n)
    full_totals, accepted_totals = totals[:3], totals[3:]
    full = _proxy_metrics(full_totals)
    accepted = _proxy_metrics(accepted_totals)
    effects = {}
    for metric in ("gap_total_pct", "gap_lateral_pct", "gap_vertical_pct"):
        point = float(full[metric] - accepted[metric])
        gradient = np.concatenate([
            _proxy_ratio_gradient(full_totals, metric),
            -_proxy_ratio_gradient(accepted_totals, metric),
        ])
        variance = float(gradient @ covariance @ gradient)
        if variance < -1e-12:
            raise UncertaintyError(
                f"selection-validation variance is negative for {metric}")
        standard_error = math.sqrt(max(0.0, variance))
        effects[metric] = {
            "point_percentage_points": point,
            "design_standard_error_percentage_points": standard_error,
            "normal_95_interval_percentage_points": [
                point - NORMAL_95 * standard_error,
                point + NORMAL_95 * standard_error,
            ],
        }
    return {
        "full_pre_gate_proxy": full,
        "gate_pass_proxy": accepted,
        "selection_effect_full_minus_gate_pass": effects,
        "interval_scope": (
            "Sampling-design uncertainty only, conditional on complete outcomes and "
            "the validity of the declared independent proxy."
        ),
    }


def selection_validation_result(*, sample_path: Path, match_path: Path,
                                outcomes_path: Path) -> dict:
    """Validate independent outcomes and estimate only a complete design."""
    sample = _load_json(sample_path)
    match = _load_json(match_path)
    outcomes = _load_json(outcomes_path)
    if (sample.get("schema_version"), sample.get("kind")) != (
            1, "co2gap-selection-validation-sample"):
        raise UncertaintyError("unknown selection-validation sample contract")
    if (match.get("schema_version"), match.get("kind")) != (
            1, "co2gap-selection-validation-match-list"):
        raise UncertaintyError("unknown selection-validation match-list contract")
    if (outcomes.get("schema_version"), outcomes.get("kind")) != (
            1, "co2gap-independent-selection-outcomes"):
        raise UncertaintyError("unknown independent-outcome contract")
    if outcomes.get("publication_status") != "private_per_flight":
        raise UncertaintyError(
            "independent outcomes must be marked private_per_flight")
    if match.get("sample_sha256") != sha256_file(sample_path):
        raise UncertaintyError("match list names another validation sample")
    if outcomes.get("sample_sha256") != sha256_file(sample_path):
        raise UncertaintyError("independent outcomes name another validation sample")
    if outcomes.get("match_list_sha256") != sha256_file(match_path):
        raise UncertaintyError("independent outcomes name another match list")
    if match.get("gate_status_disclosed") is not False:
        raise UncertaintyError("match list discloses gate status")
    if match.get("primary_track_quality_disclosed") is not False:
        raise UncertaintyError("match list discloses primary track quality")
    source = outcomes.get("source")
    if not isinstance(source, dict):
        raise UncertaintyError("independent outcomes lack source declarations")
    for field in ("name", "version", "method_reference", "quality_rule"):
        if not isinstance(source.get(field), str) or not source[field].strip():
            raise UncertaintyError(f"independent outcome source lacks {field}")
    required_declarations = {
        "independent_of_primary_adsb_lol": True,
        "primary_trajectory_used": False,
        "matching_received_gate_status": False,
    }
    for field, expected in required_declarations.items():
        if source.get(field) is not expected:
            raise UncertaintyError(
                f"independent outcome source must declare {field}={str(expected).lower()}")

    sample_rows = sample.get("rows")
    match_rows = match.get("rows")
    outcome_rows = outcomes.get("rows")
    for rows, label in ((sample_rows, "sample"), (match_rows, "match list"),
                        (outcome_rows, "independent outcomes")):
        if not isinstance(rows, list) or not rows:
            raise UncertaintyError(f"selection-validation {label} has no rows")
    sample_frame = pd.DataFrame(sample_rows)
    match_frame = pd.DataFrame(match_rows)
    outcome_frame = pd.DataFrame(outcome_rows)
    for frame, label in ((sample_frame, "sample"), (match_frame, "match list"),
                         (outcome_frame, "independent outcomes")):
        _require_columns(frame, ("sample_id",), label)
        if frame.sample_id.duplicated().any():
            raise UncertaintyError(f"selection-validation {label} has duplicate ids")
    expected_ids = set(sample_frame.sample_id.astype(str))
    if set(match_frame.sample_id.astype(str)) != expected_ids:
        raise UncertaintyError("match-list ids differ from the validation sample")
    forbidden_match_fields = {
        "flight_id", "failure_mask", "gate_pass", "coverage_frac",
        "coverage_band", "max_gap_s", "flown_ge_09gc", "origin_icao",
        "dest_icao", "weight", "population_n", "sample_n", "stratum",
    }
    disclosed = sorted(forbidden_match_fields.intersection(match_frame.columns))
    if disclosed:
        raise UncertaintyError(
            f"match list discloses gate or design fields: {disclosed}")
    if set(outcome_frame.sample_id.astype(str)) != expected_ids:
        raise UncertaintyError("independent-outcome ids differ from the validation sample")
    _require_columns(
        sample_frame,
        ("stratum", "failure_mask", "gate_pass", "population_n", "sample_n", "weight"),
        "selection-validation sample")
    _require_columns(outcome_frame, ("status",), "independent outcomes")
    unknown_statuses = sorted(
        set(outcome_frame.status.astype(str)) - VALIDATION_OUTCOME_STATUSES)
    if unknown_statuses:
        raise UncertaintyError(
            f"independent outcomes contain unknown statuses: {unknown_statuses}")
    joined = sample_frame.merge(
        outcome_frame, on="sample_id", how="left", validate="one_to_one")
    measured = joined.status == "measured"
    for row in joined.loc[~measured].itertuples(index=False):
        if not isinstance(getattr(row, "reason", None), str) or not row.reason.strip():
            raise UncertaintyError(
                f"non-measured independent outcome lacks a reason: {row.sample_id}")
        for field in ("real_co2_kg", "ideal_co2_kg", "hybrid_co2_kg"):
            value = getattr(row, field, None)
            if value is not None and not pd.isna(value):
                raise UncertaintyError(
                    f"non-measured independent outcome contains {field}: {row.sample_id}")
    if measured.any():
        _require_columns(
            joined, (
                "real_co2_kg", "ideal_co2_kg", "hybrid_co2_kg",
                "match_candidate_count", "departure_time_delta_s",
                "arrival_time_delta_s", "origin_distance_km",
                "destination_distance_km", "proxy_coverage_fraction",
                "proxy_quality_pass",
            ),
            "independent outcomes")
        values = joined.loc[measured, [
            "real_co2_kg", "ideal_co2_kg", "hybrid_co2_kg"]].apply(
                pd.to_numeric, errors="coerce").to_numpy(float)
        if (not np.isfinite(values).all() or (values < 0).any()
                or (values[:, 1] <= 0).any()):
            raise UncertaintyError(
                "measured independent outcomes contain invalid CO2 components")
        candidates = pd.to_numeric(
            joined.loc[measured, "match_candidate_count"], errors="coerce")
        if (candidates.isna().any() or (candidates != 1).any()):
            raise UncertaintyError(
                "measured independent outcomes require exactly one match candidate")
        diagnostics = joined.loc[measured, [
            "departure_time_delta_s", "arrival_time_delta_s",
            "origin_distance_km", "destination_distance_km",
            "proxy_coverage_fraction",
        ]].apply(pd.to_numeric, errors="coerce")
        if (not np.isfinite(diagnostics.to_numpy(float)).all()
                or (diagnostics < 0).any().any()
                or (diagnostics.proxy_coverage_fraction > 1).any()):
            raise UncertaintyError(
                "measured independent outcomes contain invalid match or quality diagnostics")
        quality_values = joined.loc[measured, "proxy_quality_pass"].tolist()
        if not all(
                isinstance(value, (bool, np.bool_)) and bool(value)
                for value in quality_values):
            raise UncertaintyError(
                "measured independent outcomes must pass the declared proxy quality rule")
        joined.loc[measured, [
            "real_co2_kg", "ideal_co2_kg", "hybrid_co2_kg"]] = values

    response = {
        "sample_rows": int(len(joined)),
        "measured_rows": int(measured.sum()),
        "measured_row_share": float(measured.mean()),
        "measured_weight_share": float(
            joined.loc[measured, "weight"].sum() / joined.weight.sum()),
        "status_counts": {
            str(status): int(count)
            for status, count in joined.status.value_counts().sort_index().items()
        },
        "by_failure_mask": [],
    }
    if measured.any():
        response["measured_match_diagnostics"] = {
            column: {
                "median": float(joined.loc[measured, column].median()),
                "q95": float(joined.loc[measured, column].quantile(0.95)),
                "max": float(joined.loc[measured, column].max()),
            }
            for column in (
                "departure_time_delta_s", "arrival_time_delta_s",
                "origin_distance_km", "destination_distance_km",
                "proxy_coverage_fraction",
            )
        }
    for mask, group in joined.groupby("failure_mask", sort=True):
        group_measured = group.status == "measured"
        response["by_failure_mask"].append({
            "failure_mask": str(mask),
            "sample_rows": int(len(group)),
            "measured_rows": int(group_measured.sum()),
            "measured_weight_share": float(
                group.loc[group_measured, "weight"].sum() / group.weight.sum()),
        })
    complete = bool(measured.all())
    result = {
        "schema_version": 1,
        "kind": "co2gap-selection-validation-result",
        "publication_status": "diagnostic_only",
        "release_id": sample.get("release_id"),
        "sample_sha256": sha256_file(sample_path),
        "match_list_sha256": sha256_file(match_path),
        "outcomes_sha256": sha256_file(outcomes_path),
        "source": source,
        "source_independence_declared_not_verified_by_code": True,
        "response": response,
        "estimation_status": (
            "complete_proxy_estimate" if complete
            else "blocked_incomplete_independent_outcomes"),
        "primary_headline_bias_bounded": False,
        "limitations": [
            "The source-independence declarations are contract fields, not proof.",
            "Match and proxy-quality diagnostics are reported, but their validity "
            "still depends on the external method reference.",
            "The design covers only the durable pre-gate population, not upstream exclusions.",
            "A proxy selection effect is not automatically the bias of the published model.",
            "The interval, when available, contains sampling-design uncertainty only.",
        ],
    }
    if complete:
        result["proxy_estimate"] = _complete_proxy_estimate(joined)
    else:
        result["blocked_reason"] = (
            "Every sampled flight must have an independent measured outcome; response "
            "weighting would add a second unvalidated selection model."
        )
    return result


def _metrics(frame: pd.DataFrame) -> dict[str, float]:
    """Calculate the site headline bases from an authoritative release frame."""
    required = (
        "co2_kg_v0", "ideal_gc_co2_kg", "hybrid_co2_kg",
        "co2_real_kg", "co2_ideal_kg", "co2_hybrid_kg",
    )
    _require_columns(frame, required, "headline frame")
    real_u = float(frame.co2_kg_v0.sum())
    ideal_u = float(frame.ideal_gc_co2_kg.sum())
    hybrid_u = float(frame.hybrid_co2_kg.sum())
    real_c = float(frame.co2_real_kg.sum())
    ideal_c = float(frame.co2_ideal_kg.sum())
    hybrid_c = float(frame.co2_hybrid_kg.sum())
    if min(ideal_u, ideal_c) <= 0:
        raise UncertaintyError("headline denominator is not positive")
    total = (real_u - ideal_u) / ideal_u * 100.0
    lateral = (hybrid_u - ideal_u) / ideal_u * 100.0
    vertical = (real_u - hybrid_u) / ideal_u * 100.0
    if not math.isclose(total, lateral + vertical, rel_tol=0, abs_tol=1e-10):
        raise UncertaintyError("headline decomposition is not additive")
    return {
        "co2_real_tonnes": real_c / 1000.0,
        "co2_ideal_tonnes": ideal_c / 1000.0,
        "co2_gap_tonnes": (real_c - ideal_c) / 1000.0,
        "gap_total_pct": total,
        "gap_lateral_pct": lateral,
        "gap_vertical_pct": vertical,
        "gap_total_pct_calibrated": (real_c - ideal_c) / ideal_c * 100.0,
    }


def _ratio_delta(point: dict, nominal: dict) -> dict[str, float]:
    return {name: float(point[name] - nominal[name]) for name in RATIO_METRICS}


def _selection_strata(frame: pd.DataFrame) -> pd.Series:
    _require_columns(frame, ("typecode", "gc_km"), "selection-stress frame")
    distance = pd.cut(
        pd.to_numeric(frame.gc_km, errors="coerce"), DISTANCE_EDGES,
        labels=DISTANCE_LABELS).astype("string").fillna("UNKNOWN")
    return frame.typecode.astype("string").fillna("UNKNOWN") + "|" + distance


def poststratified_selection(frame: pd.DataFrame,
                             selected: pd.Series) -> dict:
    """Restore the nominal type/distance ideal-CO2 mix inside a strict subset."""
    selected = pd.Series(selected, index=frame.index).fillna(False).astype(bool)
    if not selected.any():
        raise UncertaintyError("selection-stress scenario retains no flights")
    input_columns = [
        "co2_kg_v0", "ideal_gc_co2_kg", "hybrid_co2_kg",
        "co2_real_kg", "co2_ideal_kg", "co2_hybrid_kg",
    ]
    _require_columns(frame, input_columns, "selection-stress frame")
    ideal = pd.to_numeric(frame.ideal_gc_co2_kg, errors="coerce").to_numpy(float)
    if not np.isfinite(ideal).all() or (ideal <= 0).any():
        raise UncertaintyError(
            "selection-stress frame contains non-positive or invalid ideal CO2")
    strata = _selection_strata(frame)
    target = frame.assign(_stratum=strata).groupby(
        "_stratum", sort=True)["ideal_gc_co2_kg"].sum()
    subset = frame.loc[selected, input_columns].copy()
    subset["_stratum"] = strata.loc[selected]
    observed = subset.groupby("_stratum", sort=True)["ideal_gc_co2_kg"].sum()
    supported = target.index.intersection(observed[observed > 0].index)
    if supported.empty:
        raise UncertaintyError("selection-stress scenario has no supported strata")
    factors = target.loc[supported] / observed.loc[supported]
    subset = subset.loc[subset._stratum.isin(supported)].copy()
    weights = subset._stratum.map(factors).astype(float)
    weighted = subset[input_columns].mul(weights.to_numpy(), axis=0)
    expected_ideal = float(target.loc[supported].sum())
    actual_ideal = float(weighted.ideal_gc_co2_kg.sum())
    if not math.isclose(actual_ideal, expected_ideal, rel_tol=1e-12, abs_tol=1e-3):
        raise UncertaintyError("poststratification does not close on target ideal CO2")
    support = expected_ideal / float(target.sum())
    return {
        "point_estimates": _metrics(weighted),
        "diagnostics": {
            "strata": int(len(target)),
            "supported_strata": int(len(supported)),
            "support_share_of_nominal_ideal_co2": float(support),
            "minimum_weight": float(weights.min()),
            "maximum_weight": float(weights.max()),
            "kish_effective_sample_size": float(
                weights.sum() ** 2 / np.square(weights).sum()),
            "target_ideal_co2_closure_relative": float(
                (actual_ideal - expected_ideal) / expected_ideal),
        },
    }


def _stress_scenario(frame: pd.DataFrame, selected: pd.Series, *,
                     scenario_id: str, family: str, description: str,
                     nominal: dict) -> dict:
    selected = pd.Series(selected, index=frame.index).fillna(False).astype(bool)
    subset = frame.loc[selected]
    if subset.empty:
        raise UncertaintyError(f"selection-stress scenario {scenario_id} is empty")
    raw = _metrics(subset)
    standardised = poststratified_selection(frame, selected)
    standardised_point = standardised["point_estimates"]
    return {
        "id": scenario_id,
        "family": family,
        "description": description,
        "retained": {
            "flights": int(len(subset)),
            "flight_share": float(len(subset) / len(frame)),
            "nominal_ideal_co2_share": float(
                subset.ideal_gc_co2_kg.sum() / frame.ideal_gc_co2_kg.sum()),
        },
        "raw_subset": {
            "point_estimates": raw,
            "ratio_delta_percentage_points": _ratio_delta(raw, nominal),
        },
        "poststratified": {
            **standardised,
            "ratio_delta_percentage_points": _ratio_delta(
                standardised_point, nominal),
        },
    }


def _require_nested_counts(counts: dict[str, int],
                           families: Iterable[tuple[str, ...]]) -> None:
    for family in families:
        values = [counts[name] for name in family]
        if values != sorted(values, reverse=True):
            raise UncertaintyError(f"selection-stress family is not nested: {family}")


def selection_stress(*, manifest_path: Path, flights_dir: Path,
                     decomposition_dir: Path, ground_dir: Path,
                     calibration: Path, ground_definition: str,
                     verify: bool) -> dict:
    """Stress the headline with stricter observed-quality subsets only."""
    from release_data import load_release_data
    import track_quality

    manifest = ReleaseManifest.load(manifest_path)
    manifest.verify_track_quality(track_quality)
    if verify:
        manifest.verify_set("flights", flights_dir)
    dataset = load_release_data(
        decomposition_dir, ground_dir, calibration,
        ground_def=ground_definition, manifest=manifest,
        verify_manifest=verify,
    )
    accepted = dataset.frame.copy()
    accepted_keys = {
        day: set(group.flight_id.astype(int))
        for day, group in accepted.groupby("day", sort=False)
    }
    quality_frames = []
    day_retention = []
    for day in manifest.days:
        path = flights_dir / day / "flights.parquet"
        try:
            source = pq.read_table(path, columns=list(SELECTION_COLUMNS)).to_pandas()
        except Exception as exc:
            raise UncertaintyError(f"cannot read selection-stress input for {day}: {exc}") from exc
        if source.flight_id.duplicated().any():
            raise UncertaintyError(f"duplicate source flight_id in selection stress for {day}")
        flags = selection_flags(
            source, coverage_min=track_quality.COV_MIN,
            gc_min_km=track_quality.GC_MIN_KM)
        passed = flags.all(axis=1)
        expected = set(source.loc[passed, "flight_id"].astype(int))
        actual = accepted_keys.get(day, set())
        if expected != actual:
            raise UncertaintyError(
                f"{day}: selection-stress keyset differs from release population: "
                f"{len(expected - actual)} missing, {len(actual - expected)} extra")
        day_retention.append({
            "day": day, "source_flights": int(len(source)),
            "retained_flights": int(passed.sum()),
            "retained_share": float(passed.mean()),
        })
        quality_frames.append(
            source.loc[passed, ["flight_id", "coverage_frac", "max_gap_s"]]
            .assign(day=day))

    quality = pd.concat(quality_frames, ignore_index=True)
    if quality.duplicated(["day", "flight_id"]).any():
        raise UncertaintyError("duplicate quality key in selection stress")
    frame = accepted.merge(
        quality, on=["day", "flight_id"], how="left", validate="one_to_one")
    if frame[["coverage_frac", "max_gap_s"]].isna().any().any():
        raise UncertaintyError("release flight lacks quality metadata in selection stress")
    nominal = _metrics(frame)

    specs = [
        ("coverage_ge_090", "coverage_floor", frame.coverage_frac >= 0.90,
         "Require at least 90% temporal coverage."),
        ("coverage_ge_095", "coverage_floor", frame.coverage_frac >= 0.95,
         "Require at least 95% temporal coverage."),
        ("coverage_ge_099", "coverage_floor", frame.coverage_frac >= 0.99,
         "Require at least 99% temporal coverage."),
        ("max_gap_le_900", "maximum_gap", frame.max_gap_s <= 900,
         "Reject every accepted track with a gap longer than 900 seconds."),
        ("max_gap_le_600", "maximum_gap", frame.max_gap_s <= 600,
         "Reject every accepted track with a gap longer than 600 seconds."),
        ("max_gap_le_300", "maximum_gap", frame.max_gap_s <= 300,
         "Reject every accepted track with a gap longer than 300 seconds."),
        ("max_gap_le_120", "maximum_gap", frame.max_gap_s <= 120,
         "Reject every accepted track with a gap longer than 120 seconds."),
        ("coverage_ge_095_and_max_gap_le_300", "combined_quality",
         (frame.coverage_frac >= 0.95) & (frame.max_gap_s <= 300),
         "Require 95% coverage and no gap longer than 300 seconds."),
    ]
    worst_days = sorted(day_retention, key=lambda row: (row["retained_share"], row["day"]))
    for count in (1, 2, 4, 10):
        omitted = [row["day"] for row in worst_days[:count]]
        specs.append((
            f"drop_worst_{count}_retention_days", "day_omission",
            ~frame.day.isin(omitted),
            f"Remove the {count} day(s) with the lowest pre-gate retention: "
            + ", ".join(omitted) + "."))

    scenarios = [
        _stress_scenario(
            frame, mask, scenario_id=scenario_id, family=family,
            description=description, nominal=nominal)
        for scenario_id, family, mask, description in specs
    ]
    counts = {row["id"]: row["retained"]["flights"] for row in scenarios}
    _require_nested_counts(counts, (
        ("coverage_ge_090", "coverage_ge_095", "coverage_ge_099"),
        ("max_gap_le_900", "max_gap_le_600", "max_gap_le_300", "max_gap_le_120"),
    ))

    return {
        "schema_version": 1,
        "kind": "co2gap-selection-stress",
        "publication_status": "diagnostic_only",
        "release_id": manifest.release_id,
        "release_manifest_sha256": sha256_file(manifest.path),
        "source_manifest_verified": bool(verify),
        "population": {
            "flights": int(len(frame)), "days": int(frame.day.nunique()),
            "ground_join_coverage": float(dataset.ground_coverage),
        },
        "nominal_point_estimates": nominal,
        "poststratification": {
            "target": "nominal ideal-CO2 distribution",
            "strata": "aircraft typecode x great-circle distance band",
            "purpose": (
                "Separate part of the quality gradient from changes in aircraft and "
                "distance composition; it does not make selection random within strata."
            ),
        },
        "lowest_retention_days": worst_days[:10],
        "scenarios": scenarios,
        "interpretation": (
            "Nested stricter-quality and day-omission diagnostics on flights already "
            "inside the release. Percentage-point changes are neither a correction nor "
            "a bound for excluded flights."
        ),
        "limitations": [
            "No outcome is imputed for a rejected flight.",
            "Raw-subset tonnes shrink with the perimeter and are not missing or avoided emissions.",
            "Poststratified tonnes are synthetic weighted totals used only to compare ratios.",
            "Poststratification controls only aircraft type and distance band.",
            "Quality may remain associated with route, phase, weather or reception within a stratum.",
            "Very strict subsets can be dominated by large poststratification weights; inspect diagnostics.",
            "An external or independently validated missing-outcome proxy is still required to bound bias.",
        ],
    }


def _daily_metrics_input(frame: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "co2_kg_v0", "ideal_gc_co2_kg", "hybrid_co2_kg",
        "co2_real_kg", "co2_ideal_kg", "co2_hybrid_kg",
    ]
    return frame.groupby("day", sort=True)[columns].sum().reset_index()


def block_resample_days(daily: pd.DataFrame, *, iterations: int,
                        seed: int) -> dict[str, dict[str, float]]:
    """Resample complete days; describe composition, not model uncertainty."""
    if iterations <= 0:
        raise UncertaintyError("bootstrap iterations must be positive")
    if len(daily) < 2:
        raise UncertaintyError("day-block resampling needs at least two days")
    cols = [
        "co2_kg_v0", "ideal_gc_co2_kg", "hybrid_co2_kg",
        "co2_real_kg", "co2_ideal_kg", "co2_hybrid_kg",
    ]
    _require_columns(daily, cols, "daily aggregates")
    values = daily[cols].to_numpy(float)
    rng = np.random.default_rng(seed)
    draws = defaultdict(list)
    for _ in range(iterations):
        idx = rng.integers(0, len(values), size=len(values))
        sums = values[idx].sum(axis=0)
        row = pd.DataFrame([{name: value for name, value in zip(cols, sums)}])
        metric = _metrics(row)
        for name, value in metric.items():
            draws[name].append(value)
    result = {}
    for name, series in draws.items():
        q05, q50, q95 = np.quantile(np.asarray(series), [0.05, 0.50, 0.95])
        result[name] = {"q05": float(q05), "q50": float(q50), "q95": float(q95)}
    return result


def leave_one_month_out(frame: pd.DataFrame) -> dict[str, dict[str, float]]:
    months = frame.day.astype(str).str.slice(0, 7)
    values = defaultdict(list)
    for month in sorted(months.unique()):
        subset = frame.loc[months != month]
        metric = _metrics(subset)
        for name, value in metric.items():
            # Removing a month necessarily removes its tonnes.  That is a
            # perimeter change, not a useful composition diagnostic.  Ratios
            # remain comparable because both numerator and denominator follow
            # the same reduced population.
            if name.startswith("gap_"):
                values[name].append(value)
    return {
        name: {"min": float(min(series)), "max": float(max(series))}
        for name, series in values.items()
    }


def release_summary(*, manifest_path: Path, decomposition_dir: Path,
                    ground_dir: Path, calibration: Path, ground_definition: str,
                    iterations: int, seed: int, verify: bool) -> dict:
    from release_data import load_release_data

    manifest = ReleaseManifest.load(manifest_path)
    dataset = load_release_data(
        decomposition_dir, ground_dir, calibration,
        ground_def=ground_definition, manifest=manifest,
        verify_manifest=verify,
    )
    point = _metrics(dataset.frame)
    daily = _daily_metrics_input(dataset.frame)
    ground = {
        name: float(value) for name, value in sorted(dataset.ground_band.items())
    }
    return {
        "schema_version": 1,
        "kind": "co2gap-uncertainty-release-summary",
        "release_id": manifest.release_id,
        "release_manifest_sha256": sha256_file(manifest.path),
        "source_manifest_verified": bool(verify),
        "population": {
            "flights": int(len(dataset.frame)),
            "days": int(dataset.frame.day.nunique()),
            "ground_join_coverage": float(dataset.ground_coverage),
        },
        "point_estimates": point,
        "diagnostics": {
            "day_block_resampling": {
                "iterations": int(iterations), "seed": int(seed),
                "central_90_percent": block_resample_days(
                    daily, iterations=iterations, seed=seed),
                "interpretation": (
                    "Variability from replacing the observed period with an "
                    "equal-size sample of whole observed days; not model uncertainty "
                    "and not a confidence interval for the frozen population."
                ),
            },
            "leave_one_month_out": {
                "range": leave_one_month_out(dataset.frame),
                "interpretation": (
                    "Range after removing each covered month in turn; a composition "
                    "diagnostic, not a probability interval."
                ),
            },
            "ground_definition": {
                "gap_total_pct_by_definition": ground,
                "interpretation": (
                    "Exact full-release structural alternatives already present in "
                    "the ground artefacts; definitions are not random draws."
                ),
            },
        },
        "limitations": [
            "No probability distribution is assigned to mass, engine, wind or model error.",
            "The frozen population is described exactly; resampling only probes "
            "temporal composition.",
            "Quality-gate retention is audited separately, but its effect on the "
            "headline is not bounded; selection before the durable pre-gate "
            "population is not reconstructible for this release.",
            "Structural cruise-baseline sensitivity is measured by the paired "
            "runner, not this summary.",
        ],
    }


def _safe_optional(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _flight_from_points(typecode: str, points: pd.DataFrame):
    from trajectories import Flight, Point

    built = []
    for row in points.sort_values("t").itertuples(index=False):
        built.append(Point(
            t=float(row.t), lat=float(row.lat), lon=float(row.lon),
            alt=_safe_optional(row.alt_ft), gs=_safe_optional(row.gs_kt),
            ias=_safe_optional(row.ias_kt), vs_rep=_safe_optional(row.vs_fpm),
        ))
    return Flight(icao="UNCERTAINTY", typecode=typecode, reg=None, points=built)


def _load_calibration(path: Path) -> dict[str, float]:
    content = _load_json(path)
    factors = content.get("factors", content)
    if not isinstance(factors, dict):
        raise UncertaintyError(f"invalid calibration map: {path}")
    result = {}
    for key, value in factors.items():
        factor = _finite_number(value, f"calibration.{key}")
        if factor <= 0:
            raise UncertaintyError(f"calibration.{key} must be positive")
        result[str(key)] = factor
    return result


def _new_accumulator() -> dict[str, float | int]:
    return {
        "sample_flights": 0, "failed_flights": 0, "weight": 0.0,
        "real_u": 0.0, "ideal_u": 0.0, "hybrid_u": 0.0,
        "real_c": 0.0, "ideal_c": 0.0, "hybrid_c": 0.0,
    }


def _finish_accumulator(acc: dict, scenario_id: str) -> dict:
    if acc["ideal_u"] <= 0 or acc["ideal_c"] <= 0:
        raise UncertaintyError(f"scenario {scenario_id} produced no valid denominator")
    total = (acc["real_u"] - acc["ideal_u"]) / acc["ideal_u"] * 100.0
    lateral = (acc["hybrid_u"] - acc["ideal_u"]) / acc["ideal_u"] * 100.0
    vertical = (acc["real_u"] - acc["hybrid_u"]) / acc["ideal_u"] * 100.0
    return {
        "sample_flights": int(acc["sample_flights"]),
        "failed_flights": int(acc["failed_flights"]),
        "expanded_population_weight": float(acc["weight"]),
        "co2_real_tonnes": float(acc["real_c"] / 1000.0),
        "co2_ideal_tonnes": float(acc["ideal_c"] / 1000.0),
        "co2_gap_tonnes": float((acc["real_c"] - acc["ideal_c"]) / 1000.0),
        "gap_total_pct": float(total),
        "gap_lateral_pct": float(lateral),
        "gap_vertical_pct": float(vertical),
        "gap_total_pct_calibrated": float(
            (acc["real_c"] - acc["ideal_c"]) / acc["ideal_c"] * 100.0),
        "additivity_residual_pct": float(total - lateral - vertical),
    }


def paired_sensitivity(*, sample_path: Path, scenarios_path: Path,
                       manifest_path: Path, flights_dir: Path,
                       decomposition_dir: Path, ground_dir: Path,
                       era5_dir: Path, calibration: Path,
                       verify: bool, limit: int | None = None) -> dict:
    """Run paired finite differences; return aggregates and no per-flight rows."""
    from decompose import decompose_flight
    from emissions import estimate_fuel
    from wind.era5 import WindField, required_wind_days

    # OpenAP's smooth limiter overflows for some extreme ground steps.  The
    # integrator converts those non-finite step flows to zero; this is the same
    # guarded condition suppressed by the production phase-0 runner.
    warnings.filterwarnings("ignore", category=RuntimeWarning,
                            module=r"openap(?:\..*)?$")

    sample = _load_json(sample_path)
    if sample.get("schema_version") != 1 or sample.get("kind") != "co2gap-uncertainty-sample":
        raise UncertaintyError("unsupported uncertainty sample manifest")
    manifest = ReleaseManifest.load(manifest_path)
    if sample.get("release_id") != manifest.release_id:
        raise UncertaintyError("sample and release ids differ")
    if sample.get("release_manifest_sha256") != sha256_file(manifest.path):
        raise UncertaintyError("sample was selected from a different release manifest")
    if verify:
        manifest.verify_set("flights", flights_dir)
        manifest.verify_set("era5", era5_dir)
        manifest.verify_set("decomposition", decomposition_dir, artifact=True)
        manifest.verify_set("ground", ground_dir, artifact=True)
        manifest.verify_file("calibration", calibration)

    config = _load_json(scenarios_path)
    validate_scenarios(config)
    scenarios = {entry["id"]: entry for entry in config["scenarios"]}
    nominal = scenarios[config["nominal"]]
    rows = list(sample.get("rows") or [])
    if not rows:
        raise UncertaintyError("sample manifest has no rows")
    truncated = limit is not None and limit < len(rows)
    if limit is not None:
        if limit <= 0:
            raise UncertaintyError("limit must be positive")
        rows = rows[:limit]
    requested_weights = np.asarray([float(row["weight"]) for row in rows])
    if not np.isfinite(requested_weights).all() or (requested_weights <= 0).any():
        raise UncertaintyError("sample contains non-positive or non-finite weights")
    effective_sample_size = float(
        requested_weights.sum() ** 2 / np.square(requested_weights).sum())
    wanted_by_day = defaultdict(dict)
    for row in rows:
        day, fid = str(row["day"]), int(row["flight_id"])
        if day not in set(manifest.days):
            raise UncertaintyError(f"sample day {day} is outside the release")
        key = (day, fid)
        if fid in wanted_by_day[day]:
            raise UncertaintyError(f"duplicate sample key {key}")
        wanted_by_day[day][fid] = float(row["weight"])

    factors = _load_calibration(calibration)
    era5_config = manifest.data.get("configuration", {}).get("era5", {})
    try:
        wind_validation = {
            "levels": list(era5_config["pressure_levels_hpa"]),
            "area": list(era5_config["area_nwse"]),
            "grid": list(era5_config["grid_degrees"]),
            "variables": tuple(era5_config["variables"]),
        }
    except (KeyError, TypeError) as exc:
        raise UncertaintyError(
            "release manifest lacks the ERA5 validation configuration") from exc
    acc = {sid: _new_accumulator() for sid in scenarios}
    failure_reasons: dict[str, int] = defaultdict(int)
    closure = {
        "stored_ideal_u": 0.0, "recomputed_ideal_u": 0.0,
        "stored_hybrid_u": 0.0, "recomputed_hybrid_u": 0.0,
        "max_abs_ideal_kg": 0.0, "max_abs_hybrid_kg": 0.0,
    }
    point_columns = ["flight_id", "t", "lat", "lon", "alt_ft", "gs_kt", "ias_kt", "vs_fpm"]
    decomp_columns = [
        "flight_id", "typecode", "gc_km", "flown_km", "dep_ts", "co2_kg_v0",
        "ideal_gc_co2_kg", "hybrid_co2_kg",
    ]
    ground_columns = ["flight_id", "fuel_recomputed_kg"] + [
        f"fuel_{name}_kg" for name in sorted(GROUND_DEFINITIONS)
    ]

    for day in sorted(wanted_by_day):
        ids = set(wanted_by_day[day])
        dec = pq.read_table(
            decomposition_dir / f"{day}.parquet", columns=decomp_columns).to_pandas()
        dec = dec[dec.flight_id.isin(ids)].set_index("flight_id")
        if not dec.index.is_unique:
            raise UncertaintyError(f"duplicate decomposition flight_id for {day}")
        ground = pq.read_table(
            ground_dir / f"{day}.parquet", columns=ground_columns).to_pandas()
        ground = ground[ground.flight_id.isin(ids)].set_index("flight_id")
        if not ground.index.is_unique:
            raise UncertaintyError(f"duplicate ground flight_id for {day}")
        points = pq.read_table(
            flights_dir / day / "points.parquet", columns=point_columns).to_pandas()
        points = points[points.flight_id.isin(ids)]
        groups = {int(fid): group for fid, group in points.groupby("flight_id", sort=False)}
        wind_paths = [era5_dir / f"{wind_day}.nc"
                      for wind_day in required_wind_days([day])]
        # The immutable manifest owns the grid.  Reading the process-wide
        # ERA5_AREA default here would reject a valid ECAC release whenever the
        # caller forgot an environment variable, despite its checksums matching.
        wind = WindField(wind_paths, validation=wind_validation)

        for fid, weight in wanted_by_day[day].items():
            if fid not in dec.index or fid not in ground.index or fid not in groups:
                for scenario_acc in acc.values():
                    scenario_acc["failed_flights"] += 1
                failure_reasons["missing_input_key"] += 1
                continue
            row = dec.loc[fid]
            grow = ground.loc[fid]
            flight = _flight_from_points(str(row.typecode), groups[fid])
            nominal_real = estimate_fuel(
                flight, load_factor=float(nominal["load_factor"]),
                reserve_kg=float(nominal["reserve_kg"]),
                tas_mode=str(nominal["real_tas_mode"]),
            )
            if not nominal_real.ok or nominal_real.co2_kg <= 0:
                for scenario_acc in acc.values():
                    scenario_acc["failed_flights"] += 1
                failure_reasons["nominal_real_fuel"] += 1
                continue
            anchor = float(row.co2_kg_v0) / nominal_real.co2_kg
            arrays = {
                name: groups[fid][name].to_numpy(np.float64)
                for name in ("lat", "lon", "alt_ft", "ias_kt", "vs_fpm")
            }
            flight_results = {}
            failure = None
            for sid, scenario in scenarios.items():
                real = estimate_fuel(
                    flight, load_factor=float(scenario["load_factor"]),
                    reserve_kg=float(scenario["reserve_kg"]),
                    tas_mode=str(scenario["real_tas_mode"]),
                )
                if not real.ok or real.co2_kg <= 0:
                    failure = f"{sid}:real_fuel"
                    break
                real_gate = real.co2_kg * anchor
                result = decompose_flight(
                    str(row.typecode), real_gate, float(row.gc_km),
                    float(row.flown_km), arrays["lat"], arrays["lon"],
                    int(row.dep_ts), wind,
                    load_factor=float(scenario["load_factor"]),
                    reserve_kg=float(scenario["reserve_kg"]),
                    alt_ft=arrays["alt_ft"], ias_kt=arrays["ias_kt"],
                    vs_fpm=arrays["vs_fpm"],
                    cruise_alt_offset_ft=float(scenario["cruise_alt_offset_ft"]),
                )
                if result is None:
                    failure = f"{sid}:decomposition"
                    break
                denominator = float(grow.fuel_recomputed_kg)
                ground_fuel = float(grow[f"fuel_{scenario['ground_definition']}_kg"])
                if denominator <= 0 or not 0 <= ground_fuel <= denominator:
                    failure = f"{sid}:ground_share"
                    break
                ground_share = ground_fuel / denominator
                real_airborne = real_gate * (1.0 - ground_share)
                ideal = float(result["ideal_gc_co2_kg"])
                hybrid = float(result["hybrid_co2_kg"])
                if min(real_airborne, ideal, hybrid) <= 0:
                    failure = f"{sid}:non_positive_result"
                    break
                factor = factors.get(str(row.typecode), 1.0)
                flight_results[sid] = (
                    real_airborne, ideal, hybrid, factor,
                )

            # Sensitivities are paired only if every scenario uses the same
            # flight population.  Letting one failed scenario silently drop a
            # row would mix a model effect with a changing sample.
            if failure is not None:
                for scenario_acc in acc.values():
                    scenario_acc["failed_flights"] += 1
                failure_reasons[failure] += 1
                continue

            nominal_values = flight_results[config["nominal"]]
            closure["stored_ideal_u"] += weight * float(row.ideal_gc_co2_kg)
            closure["recomputed_ideal_u"] += weight * nominal_values[1]
            closure["stored_hybrid_u"] += weight * float(row.hybrid_co2_kg)
            closure["recomputed_hybrid_u"] += weight * nominal_values[2]
            closure["max_abs_ideal_kg"] = max(
                closure["max_abs_ideal_kg"],
                abs(float(row.ideal_gc_co2_kg) - nominal_values[1]))
            closure["max_abs_hybrid_kg"] = max(
                closure["max_abs_hybrid_kg"],
                abs(float(row.hybrid_co2_kg) - nominal_values[2]))

            for sid, (real_airborne, ideal, hybrid, factor) in flight_results.items():
                scenario_acc = acc[sid]
                scenario_acc["sample_flights"] += 1
                scenario_acc["weight"] += weight
                scenario_acc["real_u"] += weight * real_airborne
                scenario_acc["ideal_u"] += weight * ideal
                scenario_acc["hybrid_u"] += weight * hybrid
                scenario_acc["real_c"] += weight * real_airborne * factor
                scenario_acc["ideal_c"] += weight * ideal * factor
                scenario_acc["hybrid_c"] += weight * hybrid * factor
        del wind

    results = {sid: _finish_accumulator(value, sid) for sid, value in acc.items()}
    nominal_result = results[config["nominal"]]
    for sid, result in results.items():
        result["delta_from_nominal"] = {
            name: float(result[name] - nominal_result[name]) for name in METRIC_COLUMNS
        }
    any_failures = any(value["failed_flights"] for value in results.values())
    closure_result = {
        "ideal_weighted_relative": (
            closure["recomputed_ideal_u"] / closure["stored_ideal_u"] - 1.0
            if closure["stored_ideal_u"] > 0 else None),
        "hybrid_weighted_relative": (
            closure["recomputed_hybrid_u"] / closure["stored_hybrid_u"] - 1.0
            if closure["stored_hybrid_u"] > 0 else None),
        "max_abs_ideal_kg": float(closure["max_abs_ideal_kg"]),
        "max_abs_hybrid_kg": float(closure["max_abs_hybrid_kg"]),
    }
    return {
        "schema_version": 1,
        "kind": "co2gap-paired-sensitivity",
        "release_id": manifest.release_id,
        "sample_sha256": sha256_file(sample_path),
        "scenario_sha256": sha256_file(scenarios_path),
        "sample_rows_requested": len(rows),
        "sample_truncated_for_smoke_test": truncated,
        "source_manifest_verified": bool(verify),
        "population_expansion_complete": not truncated and not any_failures,
        "population_estimate_is_exact": False,
        "sample_design": {
            "weight_sum": float(requested_weights.sum()),
            "max_weight": float(requested_weights.max()),
            "kish_effective_sample_size": effective_sample_size,
            "warning": (
                "Expansion weights recover the release population size, not its "
                "exact headline. Scenario deltas are the primary screening result; "
                "the nominal sample must be compared with release-summary."
            ),
        },
        "nominal": config["nominal"],
        "scenarios": results,
        "common_population_failures": dict(sorted(failure_reasons.items())),
        "nominal_baseline_reconstruction": closure_result,
        "method": {
            "paired_parameters": True,
            "stored_track_anchor": (
                "Each scenario's recomputed observed CO2 is multiplied by the "
                "per-flight frozen/recomputed-nominal ratio, so the nominal real "
                "side closes on co2_kg_v0 despite stored-track thinning."
            ),
            "ground_share": (
                "Uses the selected ground definition's nominal per-flight fuel "
                "share. Parameter dependence of that share is not yet recomputed."
            ),
            "privacy": "Only weighted aggregates are returned; per-flight results are discarded.",
        },
        "interpretation": (
            "Finite-difference sensitivity on a stratified sample. Scenario values "
            "are diagnostic steps, not probability bounds or confidence intervals."
        ),
    }


def _common_release_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--release-manifest", type=Path, required=True)
    parser.add_argument("--decomposition-dir", type=Path, required=True)
    parser.add_argument("--ground-dir", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument(
        "--skip-manifest-verification", action="store_true",
        help="exploratory only: skip checksums; release work must not use this",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("validate", help="validate register and diagnostic scenarios")
    check.add_argument("--registry", type=Path, default=ROOT / "uncertainty-register.json")
    check.add_argument("--scenarios", type=Path, default=ROOT / "uncertainty-scenarios.json")
    check.add_argument(
        "--selection-design", type=Path,
        default=ROOT / "selection-validation-design.json")

    sample_parser = sub.add_parser("sample", help="write a weighted stratified sample manifest")
    sample_parser.add_argument("--release-manifest", type=Path, required=True)
    sample_parser.add_argument("--flights-dir", type=Path, required=True)
    sample_parser.add_argument("--decomposition-dir", type=Path, required=True)
    sample_parser.add_argument("--per-stratum", type=int, default=5)
    sample_parser.add_argument("--seed", type=int, default=20260901)
    sample_parser.add_argument("--out", type=Path, required=True)
    sample_parser.add_argument("--skip-manifest-verification", action="store_true")

    summary_parser = sub.add_parser(
        "release-summary", help="summarise exact and temporal diagnostics")
    _common_release_arguments(summary_parser)
    summary_parser.add_argument("--ground-definition", choices=sorted(GROUND_DEFINITIONS),
                                default="a3000t70")
    summary_parser.add_argument("--iterations", type=int, default=2000)
    summary_parser.add_argument("--seed", type=int, default=20260901)
    summary_parser.add_argument("--out", type=Path, required=True)

    selection_parser = sub.add_parser(
        "selection", help="audit release-gate selection and its observable boundary")
    selection_parser.add_argument("--release-manifest", type=Path, required=True)
    selection_parser.add_argument("--flights-dir", type=Path, required=True)
    selection_parser.add_argument("--decomposition-dir", type=Path, required=True)
    selection_parser.add_argument("--min-group-n", type=int, default=10)
    selection_parser.add_argument("--out", type=Path, required=True)
    selection_parser.add_argument("--skip-manifest-verification", action="store_true")

    stress_parser = sub.add_parser(
        "selection-stress",
        help="measure stricter-quality and anomalous-day gradients inside the release")
    _common_release_arguments(stress_parser)
    stress_parser.add_argument("--flights-dir", type=Path, required=True)
    stress_parser.add_argument("--ground-definition", choices=sorted(GROUND_DEFINITIONS),
                               default="a3000t70")
    stress_parser.add_argument("--out", type=Path, required=True)

    validation_sample_parser = sub.add_parser(
        "selection-validation-sample",
        help="draw a private pre-gate sample and blinded external match list")
    validation_sample_parser.add_argument(
        "--release-manifest", type=Path, required=True)
    validation_sample_parser.add_argument("--flights-dir", type=Path, required=True)
    validation_sample_parser.add_argument(
        "--decomposition-dir", type=Path, required=True)
    validation_sample_parser.add_argument("--per-stratum", type=int, default=3)
    validation_sample_parser.add_argument("--target-sample", type=int, default=5000)
    validation_sample_parser.add_argument("--top-types", type=int, default=12)
    validation_sample_parser.add_argument("--seed", type=int, default=20260901)
    validation_sample_parser.add_argument("--out", type=Path, required=True)
    validation_sample_parser.add_argument("--match-out", type=Path, required=True)
    validation_sample_parser.add_argument(
        "--selection-design", type=Path,
        default=ROOT / "selection-validation-design.json")
    validation_sample_parser.add_argument(
        "--skip-manifest-verification", action="store_true")

    validation_parser = sub.add_parser(
        "selection-validation",
        help="validate complete independent outcomes and estimate proxy selection")
    validation_parser.add_argument("--sample", type=Path, required=True)
    validation_parser.add_argument("--match-list", type=Path, required=True)
    validation_parser.add_argument("--outcomes", type=Path, required=True)
    validation_parser.add_argument("--out", type=Path, required=True)
    validation_parser.add_argument(
        "--selection-design", type=Path,
        default=ROOT / "selection-validation-design.json")

    sensitivity_parser = sub.add_parser(
        "sensitivity", help="run paired finite-difference scenarios")
    _common_release_arguments(sensitivity_parser)
    sensitivity_parser.add_argument("--flights-dir", type=Path, required=True)
    sensitivity_parser.add_argument("--era5-dir", type=Path, required=True)
    sensitivity_parser.add_argument("--sample", type=Path, required=True)
    sensitivity_parser.add_argument("--scenarios", type=Path,
                                    default=ROOT / "uncertainty-scenarios.json")
    sensitivity_parser.add_argument("--limit", type=int, default=None,
                                    help="smoke test only; invalidates population estimate")
    sensitivity_parser.add_argument("--out", type=Path, required=True)

    args = parser.parse_args(argv)
    if args.command == "validate":
        registry = validate_registry(_load_json(args.registry))
        scenarios = validate_scenarios(_load_json(args.scenarios))
        design = validate_selection_design(
            _load_json(args.selection_design), ROOT / "release-manifest.json")
        print(f"uncertainty register: {registry['estimands']} estimands, "
              f"{registry['sources']} sources")
        print(f"diagnostic scenarios: {scenarios['scenarios']}, "
              f"nominal={scenarios['nominal']}")
        print(
            f"selection validation: {design['sample_rows']:,} rows in "
            f"{design['strata']:,} strata pre-registered")
        return 0

    if args.command == "sample":
        _require_outside_repository(args.out, "uncertainty sample manifest")
        manifest = ReleaseManifest.load(args.release_manifest)
        if not args.skip_manifest_verification:
            manifest.verify_set("flights", args.flights_dir)
            manifest.verify_set("decomposition", args.decomposition_dir, artifact=True)
        population = build_population(manifest, args.flights_dir, args.decomposition_dir)
        sample = stratified_sample(population, args.per_stratum, args.seed)
        value = write_sample_manifest(
            manifest=manifest, population=population, sample=sample,
            per_stratum=args.per_stratum, seed=args.seed, output=args.out,
            verified=not args.skip_manifest_verification)
        print(f"sample: {value['sample_rows']:,} rows in {value['strata']} strata "
              f"expand to {value['weight_sum']:,.0f} release flights -> {args.out}")
        return 0

    if args.command == "selection":
        result = selection_audit(
            manifest_path=args.release_manifest, flights_dir=args.flights_dir,
            decomposition_dir=args.decomposition_dir,
            min_group_n=args.min_group_n,
            verify=not args.skip_manifest_verification)
        _atomic_json(args.out, result)
        share = result["coverage_statement"]["retained_share"]
        print(
            f"selection: retained {share['flights'] * 100:.2f}% of flights, "
            f"{share['great_circle_km'] * 100:.2f}% of great-circle km and "
            f"{share['first_pass_gate_to_gate_co2_tonnes'] * 100:.2f}% of "
            f"first-pass CO2 -> {args.out}")
        return 0

    if args.command == "selection-stress":
        result = selection_stress(
            manifest_path=args.release_manifest, flights_dir=args.flights_dir,
            decomposition_dir=args.decomposition_dir, ground_dir=args.ground_dir,
            calibration=args.calibration,
            ground_definition=args.ground_definition,
            verify=not args.skip_manifest_verification)
        _atomic_json(args.out, result)
        deltas = {
            row["id"]: (
                row["poststratified"]["ratio_delta_percentage_points"]
                ["gap_total_pct"])
            for row in result["scenarios"]
        }
        print(
            "selection stress: poststratified total-gap deltas "
            f"coverage>=0.95 {deltas['coverage_ge_095']:+.3f} pp, "
            f"max-gap<=300s {deltas['max_gap_le_300']:+.3f} pp, "
            f"drop two worst days {deltas['drop_worst_2_retention_days']:+.3f} pp "
            f"-> {args.out}")
        return 0

    if args.command == "selection-validation-sample":
        _require_outside_repository(args.out, "selection-validation sample")
        _require_outside_repository(
            args.match_out, "selection-validation match list")
        manifest = ReleaseManifest.load(args.release_manifest)
        verify_sample = not args.skip_manifest_verification
        population = build_selection_validation_population(
            manifest, args.flights_dir, args.decomposition_dir,
            verify=verify_sample)
        sample, common_types = stratified_selection_validation_sample(
            population, per_stratum=args.per_stratum,
            top_types=args.top_types, target_sample=args.target_sample,
            seed=args.seed,
            namespace=manifest.release_id)
        value, _ = write_selection_validation_artifacts(
            manifest=manifest, population=population, sample=sample,
            common_types=common_types, per_stratum=args.per_stratum,
            top_types=args.top_types, target_sample=args.target_sample,
            seed=args.seed, output=args.out,
            match_output=args.match_out, verified=verify_sample)
        design = _load_json(args.selection_design)
        validate_selection_design(design, args.release_manifest)
        verify_registered_selection_artifacts(
            design, sample_path=args.out, match_path=args.match_out)
        print(
            f"selection validation sample: {value['sample_rows']:,} private rows "
            f"in {value['strata']:,} strata expand to "
            f"{value['weight_sum']:,.0f} pre-gate flights; independent outcomes "
            f"still required -> {args.out} · {args.match_out}")
        return 0

    if args.command == "selection-validation":
        design = _load_json(args.selection_design)
        validate_selection_design(design, ROOT / "release-manifest.json")
        verify_registered_selection_artifacts(
            design, sample_path=args.sample, match_path=args.match_list)
        result = selection_validation_result(
            sample_path=args.sample, match_path=args.match_list,
            outcomes_path=args.outcomes)
        result["aggregate_preregistration_verified"] = True
        _atomic_json(args.out, result)
        if result["estimation_status"] != "complete_proxy_estimate":
            print(
                "selection validation: estimation BLOCKED because independent "
                f"outcomes are incomplete -> {args.out}")
            return 2
        else:
            effect = result["proxy_estimate"][
                "selection_effect_full_minus_gate_pass"]["gap_total_pct"]
            interval = effect["normal_95_interval_percentage_points"]
            print(
                "selection validation: independent-proxy full-minus-gate effect "
                f"{effect['point_percentage_points']:+.3f} pp "
                f"(design-only 95% {interval[0]:+.3f}..{interval[1]:+.3f}) "
                f"-> {args.out}")
            return 0

    verify = not args.skip_manifest_verification
    if args.command == "release-summary":
        result = release_summary(
            manifest_path=args.release_manifest,
            decomposition_dir=args.decomposition_dir, ground_dir=args.ground_dir,
            calibration=args.calibration, ground_definition=args.ground_definition,
            iterations=args.iterations, seed=args.seed, verify=verify)
    else:
        result = paired_sensitivity(
            sample_path=args.sample, scenarios_path=args.scenarios,
            manifest_path=args.release_manifest, flights_dir=args.flights_dir,
            decomposition_dir=args.decomposition_dir, ground_dir=args.ground_dir,
            era5_dir=args.era5_dir, calibration=args.calibration,
            verify=verify, limit=args.limit)
    _atomic_json(args.out, result)
    print(f"{result['kind']}: wrote aggregate result to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
