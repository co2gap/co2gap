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
from sampling_precision import (FROZEN, PairedSamplingPrecision,
                                SamplingPrecisionError)  # noqa: E402
from combined_mass import (CombinedMassError, DESIGN_KIND,
                           load_combined_mass_design,
                           requested_mass_fraction,
                           validate_combined_mass_design)  # noqa: E402


REGISTRY_STATUSES = {
    "quantified", "coverage_measured", "scenario_only",
    "needs_evidence", "out_of_scope",
}
REGISTRY_CATEGORIES = {"observation", "parameter", "model", "selection", "variability"}
GROUND_DEFINITIONS = {"suolo", "a1000t40", "a1000t70", "a1000t100", "a3000t70"}
SENSITIVITY_REFERENCE_PROFILES = {"frozen-release", "corrected-wind"}
# This experimental profile changes wind handling only, not the nominal mass,
# airspeed, altitude or ground convention. It is not a generic model override.
CORRECTED_WIND_NOMINAL = {
    "load_factor": 0.82, "reserve_kg": 2000.0, "real_tas_mode": "ias",
    "cruise_alt_offset_ft": 0.0, "ground_definition": "a3000t70",
}
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
SELECTION_SENSITIVITY_COMPONENTS = (
    "gap_total_pct", "gap_lateral_pct", "gap_vertical_pct",
)
SELECTION_SENSITIVITY_STRESSES = {"prudente", "centrale", "severo"}
SELECTION_SENSITIVITY_PROFILES = {
    "signed_transfer", "adverse_lower", "adverse_upper",
}
TARGETED_VALIDATION_MASKS = ("0000", "0100", "1000", "1100")
TARGETED_SOURCE_RELATIONS = {
    "receiver_independent",
    "separate_source_receiver_overlap_unverified",
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


def _valid_sha256(value) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and not (set(value) - set("0123456789abcdef")))


def validate_selection_sensitivity_design(data: dict, design_path: Path) -> dict:
    """Validate the frozen aggregate stress design and all tracked inputs."""
    if (data.get("schema_version"), data.get("kind")) != (
            1, "co2gap-selection-sensitivity-design"):
        raise UncertaintyError("unknown selection-sensitivity design contract")
    if data.get("publication_status") != "diagnostic_sensitivity_design":
        raise UncertaintyError(
            "selection-sensitivity design must remain diagnostic")
    if data.get("analysis_status") != "design_frozen_before_implementation":
        raise UncertaintyError(
            "selection-sensitivity design was not frozen before implementation")

    claims = data.get("claims")
    if not isinstance(claims, dict):
        raise UncertaintyError("selection-sensitivity design lacks claims")
    for field in (
            "corrects_release_headline", "bounds_release_headline",
            "is_confidence_interval", "is_probability_model"):
        if claims.get(field) is not False:
            raise UncertaintyError(
                f"selection-sensitivity design cannot claim {field}")
    if claims.get("may_rank_external_validation_priorities") is not True:
        raise UncertaintyError(
            "selection-sensitivity design must declare its permitted use")

    contracts = data.get("input_contracts")
    if not isinstance(contracts, dict):
        raise UncertaintyError("selection-sensitivity design lacks input contracts")
    base = Path(design_path).resolve().parent
    tracked_names = (
        "release_manifest", "release_headlines",
        "selection_validation_design", "opensky_day_audit_result",
    )
    tracked_paths = {}
    for name in tracked_names:
        contract = contracts.get(name)
        if not isinstance(contract, dict):
            raise UncertaintyError(
                f"selection-sensitivity design lacks {name} contract")
        relative = contract.get("path")
        expected = contract.get("sha256")
        if not isinstance(relative, str) or not relative.strip():
            raise UncertaintyError(
                f"selection-sensitivity design has invalid {name} path")
        if not _valid_sha256(expected):
            raise UncertaintyError(
                f"selection-sensitivity design has invalid {name} hash")
        path = base / relative
        if not path.is_file() or sha256_file(path) != expected:
            raise UncertaintyError(
                f"selection-sensitivity tracked input differs: {name}")
        tracked_paths[name] = path
    release_id = data.get("release_id")
    manifest = ReleaseManifest.load(tracked_paths["release_manifest"])
    if release_id != manifest.release_id:
        raise UncertaintyError(
            "selection-sensitivity design names another release manifest")
    for name in tracked_names[1:]:
        if _load_json(tracked_paths[name]).get("release_id") != release_id:
            raise UncertaintyError(
                f"selection-sensitivity design mixes releases at {name}")
    audit_contract = contracts.get("selection_audit")
    if (not isinstance(audit_contract, dict)
            or not _valid_sha256(audit_contract.get("expected_sha256"))):
        raise UncertaintyError(
            "selection-sensitivity design has invalid selection-audit hash")
    if audit_contract.get("required_kind") != "co2gap-selection-audit":
        raise UncertaintyError(
            "selection-sensitivity design names another audit kind")
    if audit_contract.get(
            "required_exact_keyset_match_to_decomposition") is not True:
        raise UncertaintyError(
            "selection-sensitivity design must require an exact gate keyset")
    if audit_contract.get("weight_field") != (
            "activity.first_pass_gate_to_gate_co2_tonnes"):
        raise UncertaintyError(
            "selection-sensitivity design has an unknown exposure weight")

    reference = data.get("reference")
    if (not isinstance(reference, dict)
            or reference.get("failure_mask") != "0000"
            or reference.get("headline_components")
            != list(SELECTION_SENSITIVITY_COMPONENTS)):
        raise UncertaintyError(
            "selection-sensitivity design has an invalid reference")

    mappings = data.get("failure_mask_proxy_mapping")
    if not isinstance(mappings, list) or not mappings:
        raise UncertaintyError(
            "selection-sensitivity design lacks failure-mask mappings")
    masks = [row.get("failure_mask") for row in mappings
             if isinstance(row, dict)]
    if (len(masks) != len(mappings) or len(masks) != len(set(masks))
            or "0000" in masks
            or any(len(str(mask)) != 4 or set(str(mask)) - {"0", "1"}
                   for mask in masks)):
        raise UncertaintyError(
            "selection-sensitivity failure-mask mappings are invalid")
    for row in mappings:
        proxy = row.get("proxy_mask")
        if (proxy is not None
                and (not isinstance(proxy, str) or len(proxy) != 4
                     or set(proxy) - {"0", "1"})):
            raise UncertaintyError(
                "selection-sensitivity design has invalid proxy masks")
        if not isinstance(row.get("reason"), str) or not row["reason"].strip():
            raise UncertaintyError(
                "selection-sensitivity mask mapping lacks a reason")

    stresses = data.get("stress_ladder")
    stress_ids = _unique_ids(stresses, "selection-sensitivity stress ladder")
    if stress_ids != SELECTION_SENSITIVITY_STRESSES:
        raise UncertaintyError(
            "selection-sensitivity stress ladder must contain the frozen ids")
    scales = []
    for row in stresses:
        scale = _finite_number(
            row.get("contrast_scale"), f"{row['id']}.contrast_scale")
        if scale <= 0:
            raise UncertaintyError(
                "selection-sensitivity contrast scales must be positive")
        scales.append(scale)
    if scales != sorted(scales) or len(scales) != len(set(scales)):
        raise UncertaintyError(
            "selection-sensitivity contrast scales must increase uniquely")
    profile_ids = _unique_ids(data.get("profiles"),
                              "selection-sensitivity profiles")
    if profile_ids != SELECTION_SENSITIVITY_PROFILES:
        raise UncertaintyError(
            "selection-sensitivity design lacks the frozen profiles")
    return {"stress_levels": len(stresses), "mapped_masks": len(mappings)}


def validate_targeted_validation_design(data: dict, design_path: Path) -> dict:
    """Validate the frozen provider-neutral targeted held-out protocol."""
    if (data.get("schema_version"), data.get("kind")) != (
            1, "co2gap-targeted-heldout-validation-design"):
        raise UncertaintyError("unknown targeted-validation design contract")
    if data.get("publication_status") != "aggregate_preregistration":
        raise UncertaintyError(
            "targeted-validation design must remain an aggregate preregistration")
    if data.get("analysis_status") != (
            "protocol_frozen_before_external_source_access"):
        raise UncertaintyError(
            "targeted-validation protocol was not frozen before source access")
    parent = data.get("parent_contract")
    if not isinstance(parent, dict):
        raise UncertaintyError("targeted-validation design lacks parent contract")
    base = Path(design_path).resolve().parent
    parent_path_value = parent.get("design_path")
    if not isinstance(parent_path_value, str) or not parent_path_value.strip():
        raise UncertaintyError("targeted-validation parent design path is invalid")
    parent_path = base / parent_path_value
    if (not parent_path.is_file()
            or not _valid_sha256(parent.get("design_sha256"))
            or sha256_file(parent_path) != parent["design_sha256"]):
        raise UncertaintyError(
            "targeted-validation parent design differs from registration")
    parent_design = _load_json(parent_path)
    validate_selection_design(parent_design, base / "release-manifest.json")
    if (data.get("release_id") != parent_design.get("release_id")
            or parent.get("canonical_sample_rows")
            != parent_design.get("sample_rows")):
        raise UncertaintyError(
            "targeted-validation design differs from canonical population")
    parent_hashes = parent_design.get("private_artifact_sha256", {})
    if (parent.get("private_sample_sha256") != parent_hashes.get("sample")
            or parent.get("blinded_match_list_sha256")
            != parent_hashes.get("match_list")):
        raise UncertaintyError(
            "targeted-validation parent artifact hashes differ")
    if parent.get("canonical_design_unchanged") is not True:
        raise UncertaintyError(
            "targeted-validation design cannot replace the canonical design")

    allocation = data.get("target_allocation")
    if not isinstance(allocation, list):
        raise UncertaintyError("targeted-validation design lacks allocation")
    allocation_masks = [row.get("failure_mask") for row in allocation
                        if isinstance(row, dict)]
    if (len(allocation_masks) != len(allocation)
            or tuple(allocation_masks) != TARGETED_VALIDATION_MASKS):
        raise UncertaintyError(
            "targeted-validation allocation must use the frozen mask order")
    parent_by_mask = {
        str(row["failure_mask"]): int(row["sample_rows"])
        for row in parent_design["by_failure_mask"]
    }
    target_sum = 0
    for row in allocation:
        mask = row["failure_mask"]
        target = row.get("target_rows")
        if (not isinstance(target, int) or isinstance(target, bool)
                or not 0 < target <= parent_by_mask[mask]):
            raise UncertaintyError(
                f"targeted-validation allocation is invalid for mask {mask}")
        if mask != "0000" and target != parent_by_mask[mask]:
            raise UncertaintyError(
                f"targeted-validation must retain every canonical {mask} row")
        if not isinstance(row.get("selection"), str) or not row["selection"].strip():
            raise UncertaintyError(
                f"targeted-validation mask {mask} lacks a selection rule")
        target_sum += target
    if target_sum != data.get("target_rows"):
        raise UncertaintyError(
            "targeted-validation allocation does not close")

    controls = data.get("control_selection")
    if (not isinstance(controls, dict)
            or not isinstance(controls.get("namespace"), str)
            or not controls["namespace"].strip()
            or controls.get("randomness_claimed") is not False):
        raise UncertaintyError(
            "targeted-validation control selection is invalid")
    blinding = data.get("blinding")
    if not isinstance(blinding, dict):
        raise UncertaintyError("targeted-validation design lacks blinding")
    allowed = blinding.get("external_matching_allowed_fields")
    prohibited = blinding.get("external_matching_prohibited_fields")
    if (not isinstance(allowed, list) or not isinstance(prohibited, list)
            or len(allowed) != len(set(allowed))
            or len(prohibited) != len(set(prohibited))
            or set(allowed).intersection(prohibited)
            or "sample_id" not in allowed
            or "failure_mask" not in prohibited
            or blinding.get("failure_mask_joined_after_matching_and_source_quality")
            is not True
            or blinding.get("matching_received_gate_status") is not False
            or blinding.get("matching_received_primary_track_quality") is not False):
        raise UncertaintyError(
            "targeted-validation blinding contract is invalid")

    matching = data.get("matching")
    if (not isinstance(matching, dict)
            or matching.get("required_profile") != "targeted_mutual_nearest_v1"
            or matching.get("score") != (
                "abs(dep_delta)/900 + abs(arr_delta)/900 + origin_km/25 + "
                "destination_km/25")
            or matching.get("assignment") != "one-to-one mutual nearest neighbour"
            or matching.get("manual_overrides") is not False):
        raise UncertaintyError(
            "targeted-validation matching protocol is invalid")
    for field in (
            "candidate_max_departure_delta_s",
            "candidate_max_arrival_delta_s",
            "candidate_max_origin_distance_km",
            "candidate_max_destination_distance_km",
            "maximum_score", "tie_epsilon"):
        if _finite_number(matching.get(field), f"matching.{field}") <= 0:
            raise UncertaintyError(
                f"targeted-validation matching.{field} must be positive")

    quality = data.get("source_quality")
    if (not isinstance(quality, dict)
            or quality.get("required_profile") != "targeted_source_quality_v1"
            or quality.get("quality_evaluated_before_failure_mask_join") is not True
            or quality.get("post_source_amendment_allowed") is not False):
        raise UncertaintyError(
            "targeted-validation source-quality protocol is not frozen")
    for field in (
            "temporal_aggregation_seconds", "minimum_unique_points",
            "gap_threshold_s", "coverage_min_fraction",
            "flown_min_fraction_of_source_gc", "great_circle_min_km",
            "maximum_segment_speed_kt"):
        if _finite_number(quality.get(field), f"source_quality.{field}") <= 0:
            raise UncertaintyError(
                f"targeted-validation source_quality.{field} must be positive")

    minimum = data.get("minimum_measured_rows")
    targets = {row["failure_mask"]: row["target_rows"] for row in allocation}
    if not isinstance(minimum, dict) or set(minimum) != set(targets):
        raise UncertaintyError(
            "targeted-validation minimum support masks differ from allocation")
    for mask, value in minimum.items():
        if (not isinstance(value, int) or isinstance(value, bool)
                or not 0 < value <= targets[mask]):
            raise UncertaintyError(
                f"targeted-validation minimum support is invalid for {mask}")

    estimands = data.get("estimands")
    if (not isinstance(estimands, dict)
            or estimands.get("pool_rejected_masks") is not False
            or "weighted" not in str(estimands.get("primary", "")).lower()):
        raise UncertaintyError(
            "targeted-validation rejected masks must remain separate")
    clarification = data.get("pre_source_clarification")
    if (not isinstance(clarification, dict)
            or clarification.get("original_design_commit") != "6c8809e"
            or clarification.get("external_source_accessed") is not False
            or clarification.get("outcome_schema_changed") is not True):
        raise UncertaintyError(
            "targeted-validation pre-source clarification is not preserved")
    outcome = data.get("outcome_contract")
    if not isinstance(outcome, dict) or not isinstance(outcome.get("path"), str):
        raise UncertaintyError(
            "targeted-validation design lacks outcome contract")
    outcome_path = base / outcome["path"]
    if (not outcome_path.is_file()
            or not _valid_sha256(outcome.get("sha256"))
            or sha256_file(outcome_path) != outcome["sha256"]):
        raise UncertaintyError(
            "targeted-validation outcome contract differs")
    claims = data.get("claims")
    if not isinstance(claims, dict):
        raise UncertaintyError("targeted-validation design lacks claims")
    for field in (
            "replaces_canonical_validation", "corrects_release_headline",
            "bounds_release_headline", "is_confidence_interval",
            "is_probability_model"):
        if claims.get(field) is not False:
            raise UncertaintyError(
                f"targeted-validation design cannot claim {field}")
    return {"target_rows": target_sum, "masks": len(allocation)}


def validate_targeted_validation_registration(
        registration: dict, design: dict, design_path: Path) -> dict:
    """Validate the public hashes and aggregate partition of a private tranche."""
    validate_targeted_validation_design(design, design_path)
    if (registration.get("schema_version"), registration.get("kind")) != (
            1, "co2gap-targeted-heldout-registration"):
        raise UncertaintyError("unknown targeted-validation registration contract")
    if registration.get("publication_status") != "aggregate_preregistration":
        raise UncertaintyError(
            "targeted-validation registration must remain aggregate")
    if registration.get("release_id") != design.get("release_id"):
        raise UncertaintyError(
            "targeted-validation registration names another release")
    if registration.get("targeted_design_sha256") != sha256_file(design_path):
        raise UncertaintyError(
            "targeted-validation registration names another design")
    hashes = registration.get("private_artifact_sha256")
    if (not isinstance(hashes, dict)
            or not all(_valid_sha256(hashes.get(name))
                       for name in ("sample", "match_list"))):
        raise UncertaintyError(
            "targeted-validation registration has invalid private hashes")
    if registration.get("sample_rows") != design.get("target_rows"):
        raise UncertaintyError(
            "targeted-validation registration row count differs")
    rows = registration.get("by_failure_mask")
    allocation = {
        row["failure_mask"]: row["target_rows"]
        for row in design["target_allocation"]
    }
    if (not isinstance(rows, list)
            or [row.get("failure_mask") for row in rows]
            != list(TARGETED_VALIDATION_MASKS)):
        raise UncertaintyError(
            "targeted-validation registration has invalid mask partition")
    for row in rows:
        if row.get("sample_rows") != allocation[row["failure_mask"]]:
            raise UncertaintyError(
                "targeted-validation registration mask counts do not close")
        for field in (
                "canonical_strata", "targeted_weight_sum",
                "maximum_targeted_weight", "kish_effective_sample_size"):
            if _finite_number(
                    row.get(field),
                    f"targeted registration {row['failure_mask']}.{field}") <= 0:
                raise UncertaintyError(
                    "targeted-validation registration has invalid diagnostics")
        if row["kish_effective_sample_size"] > row["sample_rows"]:
            raise UncertaintyError(
                "targeted-validation effective size exceeds sample rows")
        if row.get("minimum_measured_rows") != design[
                "minimum_measured_rows"][row["failure_mask"]]:
            raise UncertaintyError(
                "targeted-validation registration support threshold differs")
    parent_hashes = registration.get("parent_artifact_sha256")
    if parent_hashes != {
            "sample": design["parent_contract"]["private_sample_sha256"],
            "match_list": design["parent_contract"][
                "blinded_match_list_sha256"]}:
        raise UncertaintyError(
            "targeted-validation registration parent hashes differ")
    if registration.get("outcome_contract_sha256") != design[
            "outcome_contract"]["sha256"]:
        raise UncertaintyError(
            "targeted-validation registration outcome contract differs")
    if registration.get("canonical_design_unchanged") is not True:
        raise UncertaintyError(
            "targeted-validation registration changes the canonical design")
    if registration.get("primary_headline_bias_bounded") is not False:
        raise UncertaintyError(
            "targeted-validation registration claims a headline bound")
    if registration.get("gate_status_disclosed_to_matching") is not False:
        raise UncertaintyError(
            "targeted-validation registration discloses gate status")
    if registration.get("primary_track_quality_disclosed_to_matching") is not False:
        raise UncertaintyError(
            "targeted-validation registration discloses primary quality")
    return {"sample_rows": registration["sample_rows"], "masks": len(rows)}


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


def _sampling_integer(value: int, label: str, minimum: int = 1) -> int:
    if (isinstance(value, (bool, np.bool_))
            or not isinstance(value, (int, np.integer)) or value < minimum):
        raise UncertaintyError(f"{label} must be an integer at least {minimum}")
    return int(value)


def balanced_sensitivity_allocation(sizes: pd.Series, *, minimum: int,
                                    target: int) -> pd.Series:
    """Minimum plus proportional remaining capacity, integer largest remainders.

    Allocation uses cell counts only, never sensitivity outcomes. At least two
    observations are kept in each non-census cell. Singleton cells are censuses.
    This helper does not alter the separate held-out validation allocator.
    """
    minimum = _sampling_integer(minimum, "minimum per stratum", 2)
    target = _sampling_integer(target, "target sample")
    if sizes.empty or not sizes.index.is_unique:
        raise UncertaintyError("allocation requires nonempty, unique strata")
    sizes = sizes.sort_index()
    counts = {name: _sampling_integer(n, f"population of {name}")
              for name, n in sizes.items()}
    allocation = {name: min(minimum, n) for name, n in counts.items()}
    floor_total = sum(allocation.values())
    if not floor_total <= target <= sum(counts.values()):
        raise UncertaintyError(
            f"target sample {target} must be between stratum minimum {floor_total} "
            f"and population {sum(counts.values())}")
    remaining = target - floor_total
    if remaining:
        capacity = {name: n - allocation[name] for name, n in counts.items()}
        capacity_total = sum(capacity.values())
        # Python integer division avoids floating-point tie/remainder drift.
        quotients = {name: divmod(remaining * n, capacity_total)
                     for name, n in capacity.items()}
        for name, (whole, _) in quotients.items():
            allocation[name] += whole
        left = target - sum(allocation.values())
        order = sorted(
            (name for name in counts if allocation[name] < counts[name]),
            key=lambda name: (-quotients[name][1], str(name)))
        for name in order[:left]:
            allocation[name] += 1
    if (sum(allocation.values()) != target
            or any(not min(minimum, counts[name]) <= n <= counts[name]
                   for name, n in allocation.items())):
        raise UncertaintyError("balanced sensitivity allocation does not close")
    return pd.Series(allocation, dtype="int64")


def stratified_sample(population: pd.DataFrame, per_stratum: int, seed: int,
                      *, target_sample: int | None = None) -> pd.DataFrame:
    """Deterministic within-cell SRS; equal cap or minimum-plus-proportional.

    With target_sample, per_stratum denotes the minimum (at least two), not a
    cap. Without it, the historical equal-cap draw is preserved exactly.
    """
    per_stratum = _sampling_integer(per_stratum, "per_stratum")
    _require_columns(
        population, ("day", "flight_id", "typecode", "gc_km", "coverage_frac"),
        "sample population")
    pop = population.copy()
    keys = list(zip(pop.day.astype(str), pop.flight_id.astype(int)))
    if len(keys) != len(set(keys)):
        raise UncertaintyError("sample population has duplicate (day, flight_id) keys")
    pop["day"] = pop.day.astype(str)
    pop["flight_id"] = pop.flight_id.astype(int)
    distance = pd.to_numeric(pop.gc_km, errors="coerce")
    coverage = pd.to_numeric(pop.coverage_frac, errors="coerce")
    if (distance.isna().any() or coverage.isna().any()
            or not np.isfinite(distance).all() or not np.isfinite(coverage).all()):
        raise UncertaintyError("sample population contains non-numeric or non-finite distance or coverage")
    pop["distance_band"] = pd.cut(
        distance, DISTANCE_EDGES,
        labels=DISTANCE_LABELS).astype(str)
    pop["coverage_band"] = pd.cut(
        coverage, COVERAGE_EDGES,
        labels=COVERAGE_LABELS).astype(str)
    pop["stratum"] = (
        pop.typecode.fillna("UNKNOWN").astype(str) + "|" + pop.distance_band
        + "|" + pop.coverage_band)
    allocation = None
    if target_sample is not None:
        allocation = balanced_sensitivity_allocation(
            pop.groupby("stratum", sort=True).size(), minimum=per_stratum,
            target=target_sample)
    rng = np.random.default_rng(seed)
    selected = []
    for stratum, group in pop.groupby("stratum", sort=True):
        ordered = group.sort_values(["day", "flight_id"])
        n_population = len(ordered)
        n_sample = (min(per_stratum, n_population) if allocation is None
                    else int(allocation[stratum]))
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


def _selection_sensitivity_vector(row: dict, label: str) -> dict[str, float]:
    vector = {
        name: _finite_number(row.get(name), f"{label}.{name}")
        for name in SELECTION_SENSITIVITY_COMPONENTS
    }
    if not math.isclose(
            vector["gap_total_pct"],
            vector["gap_lateral_pct"] + vector["gap_vertical_pct"],
            rel_tol=0, abs_tol=1e-9):
        raise UncertaintyError(
            f"{label} breaks total = lateral + vertical")
    return vector


def _opensky_proxy_vector(row: dict, label: str) -> dict[str, float]:
    source_names = {
        "gap_total_pct": "total_gap_pct",
        "gap_lateral_pct": "lateral_gap_pct",
        "gap_vertical_pct": "vertical_gap_pct",
    }
    return _selection_sensitivity_vector(
        {target: row.get(source) for target, source in source_names.items()},
        label)


def _selection_sensitivity_input_path(
        design: dict, design_path: Path, name: str) -> Path:
    return Path(design_path).resolve().parent / design["input_contracts"][name]["path"]


def selection_sensitivity(*, design_path: Path,
                          selection_audit_path: Path) -> dict:
    """Propagate declared mask contrasts without estimating rejected outcomes."""
    design = _load_json(design_path)
    validate_selection_sensitivity_design(design, design_path)
    contracts = design["input_contracts"]
    if sha256_file(selection_audit_path) != contracts[
            "selection_audit"]["expected_sha256"]:
        raise UncertaintyError(
            "selection audit differs from the frozen sensitivity design")

    manifest_path = _selection_sensitivity_input_path(
        design, design_path, "release_manifest")
    headline_path = _selection_sensitivity_input_path(
        design, design_path, "release_headlines")
    validation_path = _selection_sensitivity_input_path(
        design, design_path, "selection_validation_design")
    opensky_path = _selection_sensitivity_input_path(
        design, design_path, "opensky_day_audit_result")
    manifest = ReleaseManifest.load(manifest_path)
    headlines = _load_json(headline_path)
    validation = _load_json(validation_path)
    opensky = _load_json(opensky_path)
    audit = _load_json(selection_audit_path)
    validate_selection_design(validation, manifest_path)

    release_id = design.get("release_id")
    for label, value in (
            ("manifest", manifest.release_id),
            ("release headlines", headlines.get("release_id")),
            ("selection validation", validation.get("release_id")),
            ("OpenSky result", opensky.get("release_id")),
            ("selection audit", audit.get("release_id"))):
        if value != release_id:
            raise UncertaintyError(
                f"selection-sensitivity {label} names another release")
    if (audit.get("kind") != contracts["selection_audit"]["required_kind"]
            or audit.get("source_manifest_verified") is not True
            or audit.get("release_manifest_sha256") != sha256_file(manifest_path)
            or audit.get("gate", {}).get(
                "exact_keyset_match_to_decomposition") is not True):
        raise UncertaintyError(
            "selection-sensitivity requires a verified exact selection audit")
    if (opensky.get("kind")
            != "co2gap-opensky-day-selection-audit-result"
            or opensky.get("publication_status") != "aggregate_diagnostic"
            or opensky.get("primary_headline_bias_bounded") is not False):
        raise UncertaintyError(
            "selection-sensitivity requires a non-bounding OpenSky diagnostic")

    headline_values = headlines.get("values")
    if not isinstance(headline_values, dict):
        raise UncertaintyError("release headlines lack values")
    headline = _selection_sensitivity_vector(
        headline_values, "release headlines")
    validation_masks = {
        str(row["failure_mask"]): row
        for row in validation["by_failure_mask"]
    }
    if headline_values.get("flights") != validation_masks["0000"][
            "population_rows"]:
        raise UncertaintyError(
            "release headline flights differ from gate-pass population")

    audit_rows_raw = audit.get("failure_combinations")
    if not isinstance(audit_rows_raw, list):
        raise UncertaintyError("selection audit lacks failure combinations")
    audit_rows = {
        str(row.get("failure_mask")): row
        for row in audit_rows_raw if isinstance(row, dict)
    }
    if (len(audit_rows) != len(audit_rows_raw)
            or set(audit_rows) != set(validation_masks)):
        raise UncertaintyError(
            "selection audit failure masks differ from frozen population")
    exposures = {}
    for mask, registered in validation_masks.items():
        activity = audit_rows[mask].get("activity")
        if not isinstance(activity, dict):
            raise UncertaintyError(
                f"selection audit mask {mask} lacks activity")
        if activity.get("flights") != registered["population_rows"]:
            raise UncertaintyError(
                f"selection audit mask {mask} population differs from design")
        exposure = _finite_number(
            activity.get("first_pass_gate_to_gate_co2_tonnes"),
            f"selection audit mask {mask} exposure")
        if exposure <= 0:
            raise UncertaintyError(
                f"selection audit mask {mask} exposure must be positive")
        exposures[mask] = exposure
    source = audit.get("coverage_statement", {}).get("source", {})
    if source.get("flights") != validation.get("population_rows"):
        raise UncertaintyError(
            "selection audit source population does not close")
    total_exposure = sum(exposures.values())
    source_exposure = _finite_number(
        source.get("first_pass_gate_to_gate_co2_tonnes"),
        "selection audit source exposure")
    if not math.isclose(
            total_exposure, source_exposure, rel_tol=0, abs_tol=1e-6):
        raise UncertaintyError(
            "selection audit failure-mask exposures do not close")

    opensky_rows_raw = opensky.get("by_failure_mask")
    if not isinstance(opensky_rows_raw, list):
        raise UncertaintyError("OpenSky result lacks failure masks")
    opensky_rows = {
        str(row.get("failure_mask")): row
        for row in opensky_rows_raw if isinstance(row, dict)
    }
    if len(opensky_rows) != len(opensky_rows_raw) or "0000" not in opensky_rows:
        raise UncertaintyError("OpenSky result has invalid failure masks")
    reference_proxy = opensky_rows["0000"].get("proxy")
    if not isinstance(reference_proxy, dict):
        raise UncertaintyError("OpenSky reference mask lacks a proxy")
    reference_vector = _opensky_proxy_vector(
        reference_proxy, "OpenSky reference proxy")
    proxy_vectors = {}
    for mask, row in opensky_rows.items():
        proxy = row.get("proxy")
        if proxy is not None:
            if not isinstance(proxy, dict):
                raise UncertaintyError(
                    f"OpenSky mask {mask} has invalid proxy")
            proxy_vectors[mask] = _opensky_proxy_vector(
                proxy, f"OpenSky mask {mask} proxy")

    mappings = {
        str(row["failure_mask"]): row
        for row in design["failure_mask_proxy_mapping"]
    }
    if set(mappings) != set(validation_masks) - {"0000"}:
        raise UncertaintyError(
            "selection-sensitivity mappings do not cover the release masks")
    contrasts = {}
    for mask, mapping in mappings.items():
        proxy_mask = mapping["proxy_mask"]
        if proxy_mask is None:
            contrasts[mask] = {
                name: 0.0 for name in SELECTION_SENSITIVITY_COMPONENTS}
            continue
        if proxy_mask not in proxy_vectors:
            raise UncertaintyError(
                f"selection-sensitivity proxy mask {proxy_mask} has no outcome")
        contrasts[mask] = {
            name: proxy_vectors[proxy_mask][name] - reference_vector[name]
            for name in SELECTION_SENSITIVITY_COMPONENTS
        }
        _selection_sensitivity_vector(
            contrasts[mask], f"OpenSky contrast for mask {mask}")

    def oriented(vector: dict[str, float], profile: str) -> dict[str, float]:
        if profile == "signed_transfer":
            factor = 1.0
        elif vector["gap_total_pct"] == 0:
            factor = 0.0
        elif profile == "adverse_upper":
            factor = 1.0 if vector["gap_total_pct"] > 0 else -1.0
        elif profile == "adverse_lower":
            factor = -1.0 if vector["gap_total_pct"] > 0 else 1.0
        else:  # protected by the design validator
            raise UncertaintyError(
                f"unknown selection-sensitivity profile {profile}")
        return {name: factor * vector[name]
                for name in SELECTION_SENSITIVITY_COMPONENTS}

    scenario_rows = []
    mask_contributions = defaultdict(dict)
    for stress in design["stress_ladder"]:
        scale = float(stress["contrast_scale"])
        profiles = {}
        for profile in design["profiles"]:
            profile_id = profile["id"]
            shift = {name: 0.0 for name in SELECTION_SENSITIVITY_COMPONENTS}
            contributions = []
            for mask in sorted(mappings):
                vector = oriented(contrasts[mask], profile_id)
                share = exposures[mask] / total_exposure
                contribution = {
                    name: share * scale * vector[name]
                    for name in SELECTION_SENSITIVITY_COMPONENTS
                }
                for name in SELECTION_SENSITIVITY_COMPONENTS:
                    shift[name] += contribution[name]
                contributions.append({
                    "failure_mask": mask,
                    "shift_percentage_points": contribution,
                })
                mask_contributions[(stress["id"], profile_id)][mask] = contribution
            full = {
                name: headline[name] + shift[name]
                for name in SELECTION_SENSITIVITY_COMPONENTS
            }
            _selection_sensitivity_vector(
                shift, f"{stress['id']} {profile_id} shift")
            _selection_sensitivity_vector(
                full, f"{stress['id']} {profile_id} full-population gap")
            profiles[profile_id] = {
                "full_population_sensitivity_gap_pct": full,
                "shift_from_frozen_headline_percentage_points": shift,
                "by_failure_mask": contributions,
            }
        scenario_rows.append({
            "id": stress["id"],
            "contrast_scale": scale,
            "profiles": profiles,
        })

    central_signed = mask_contributions[("centrale", "signed_transfer")]
    central_upper = mask_contributions[("centrale", "adverse_upper")]
    gross_signed = sum(abs(row["gap_total_pct"])
                       for row in central_signed.values())
    net_signed = sum(row["gap_total_pct"] for row in central_signed.values())
    envelope_total = sum(row["gap_total_pct"]
                         for row in central_upper.values())
    priorities = []
    for mask in sorted(mappings):
        magnitude = central_upper[mask]["gap_total_pct"]
        priorities.append({
            "failure_mask": mask,
            "proxy_mask": mappings[mask]["proxy_mask"],
            "population_rows": validation_masks[mask]["population_rows"],
            "first_pass_exposure_tonnes": exposures[mask],
            "share_of_full_first_pass_exposure": exposures[mask] / total_exposure,
            "centrale_adverse_total_contribution_percentage_points": magnitude,
            "share_of_centrale_adverse_total": (
                magnitude / envelope_total if envelope_total else 0.0),
        })
    priorities.sort(
        key=lambda row: (
            -abs(row["centrale_adverse_total_contribution_percentage_points"]),
            row["failure_mask"]),
    )

    return {
        "schema_version": 1,
        "kind": "co2gap-selection-sensitivity",
        "publication_status": "aggregate_diagnostic",
        "analysis_status": "complete_diagnostic",
        "release_id": release_id,
        "population_boundary": design["population_boundary"],
        "design_sha256": sha256_file(design_path),
        "input_hashes": {
            "release_manifest": sha256_file(manifest_path),
            "release_headlines": sha256_file(headline_path),
            "selection_validation_design": sha256_file(validation_path),
            "opensky_day_audit_result": sha256_file(opensky_path),
            "selection_audit": sha256_file(selection_audit_path),
        },
        "population": {
            "rows": validation["population_rows"],
            "gate_pass_rows": validation_masks["0000"]["population_rows"],
            "gate_rejected_rows": (
                validation["population_rows"]
                - validation_masks["0000"]["population_rows"]),
            "first_pass_exposure_tonnes": total_exposure,
            "gate_rejected_first_pass_exposure_share": (
                1.0 - exposures["0000"] / total_exposure),
        },
        "frozen_gate_pass_headline_pct": headline,
        "opensky_reference_proxy_pct": reference_vector,
        "opensky_level_offset_transferred": False,
        "mask_assumptions": [
            {
                "failure_mask": mask,
                "proxy_mask": mappings[mask]["proxy_mask"],
                "reason": mappings[mask]["reason"],
                "opensky_contrast_percentage_points": contrasts[mask],
            }
            for mask in sorted(mappings)
        ],
        "stress_ladder": scenario_rows,
        "cancellation_diagnostic": {
            "centrale_signed_total_shift_percentage_points": net_signed,
            "centrale_signed_gross_absolute_contributions_percentage_points": gross_signed,
            "centrale_signed_cancellation_fraction": (
                1.0 - abs(net_signed) / gross_signed if gross_signed else 0.0),
            "centrale_adverse_half_width_percentage_points": envelope_total,
        },
        "external_validation_priorities": priorities,
        "claims": design["claims"],
        "limitations": [
            "The OpenSky contrasts are one-day, post-pilot, conditional diagnostics with differential source response.",
            "First-pass gate-to-gate CO2 is only an exposure weight, not the missing airborne ideal denominator.",
            "Zero contrast for unsupported masks is an explicit scenario assumption, not evidence of no effect.",
            "The adverse profiles are deterministic stress directions, not probability or confidence limits.",
            "Historical selection before the durable pre-gate population remains outside the calculation.",
        ],
        "interpretation": (
            "Failure-mask sensitivity around the frozen passed-flight headline. "
            "It diagnoses scale, cancellation and validation priorities; it does "
            "not correct or bound the release headline."
        ),
        "privacy": design["privacy"],
    }


def write_sample_manifest(*, manifest: ReleaseManifest, population: pd.DataFrame,
                          sample: pd.DataFrame, per_stratum: int, seed: int,
                          output: Path, verified: bool,
                          target_sample: int | None = None) -> dict:
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
    if target_sample is not None:
        minimum = _sampling_integer(per_stratum, "minimum per stratum", 2)
        target = _sampling_integer(target_sample, "target sample")
        if len(sample) != target:
            raise UncertaintyError("balanced sample row count differs from declared target")
        value.pop("per_stratum")
        value["sampling_design"] = {
            "allocation": "minimum-plus-proportional-remaining-capacity",
            "minimum_per_stratum": minimum,
            "target_sample_rows": target,
            "rounding": "integer largest remainders, lexicographic stratum ties",
            "within_stratum": "simple random sampling without replacement",
            "uses_sensitivity_outcomes": False,
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


def _targeted_control_allocation(
        controls: pd.DataFrame, target_rows: int) -> dict[str, int]:
    """Allocate deterministic second-stage controls with every stratum covered."""
    metadata = []
    for stratum, group in controls.groupby("stratum", sort=True):
        population = pd.to_numeric(group.population_n, errors="coerce").unique()
        parent_sample = pd.to_numeric(group.sample_n, errors="coerce").unique()
        if (len(population) != 1 or len(parent_sample) != 1
                or int(parent_sample[0]) != len(group)
                or not 0 < int(parent_sample[0]) <= int(population[0])):
            raise UncertaintyError(
                f"targeted control stratum metadata is invalid: {stratum}")
        metadata.append({
            "stratum": str(stratum),
            "population_n": int(population[0]),
            "capacity": int(parent_sample[0]),
        })
    if not metadata:
        raise UncertaintyError("targeted validation has no passing control strata")
    if target_rows < len(metadata) or target_rows > len(controls):
        raise UncertaintyError(
            "targeted control count cannot cover every canonical stratum")
    allocation = {row["stratum"]: 1 for row in metadata}
    capacity = {row["stratum"]: row["capacity"] - 1 for row in metadata}
    population = {row["stratum"]: row["population_n"] for row in metadata}
    remaining = target_rows - len(metadata)
    while remaining:
        active = [name for name in sorted(allocation) if capacity[name] > 0]
        if not active:
            raise UncertaintyError(
                "targeted control allocation exhausted before reaching target")
        selected = max(
            active,
            key=lambda name: (
                population[name] ** 2
                / (allocation[name] * (allocation[name] + 1)),
                population[name], name),
        )
        allocation[selected] += 1
        capacity[selected] -= 1
        remaining -= 1
    if sum(allocation.values()) != target_rows:
        raise UncertaintyError("targeted control allocation does not close")
    return allocation


def write_targeted_validation_artifacts(
        *, design_path: Path, parent_sample_path: Path,
        parent_match_path: Path, output: Path, match_output: Path,
        registration_output: Path) -> tuple[dict, dict, dict]:
    """Build a blinded targeted tranche without altering the canonical sample."""
    _require_outside_repository(output, "targeted-validation sample")
    _require_outside_repository(match_output, "targeted-validation match list")
    output_paths = {
        output.resolve(), match_output.resolve(), registration_output.resolve()}
    if len(output_paths) != 3:
        raise UncertaintyError(
            "targeted-validation outputs must be three different files")
    parent_paths = {parent_sample_path.resolve(), parent_match_path.resolve()}
    if output_paths.intersection(parent_paths):
        raise UncertaintyError(
            "targeted-validation outputs cannot overwrite canonical artifacts")
    design = _load_json(design_path)
    validate_targeted_validation_design(design, design_path)
    parent = design["parent_contract"]
    if sha256_file(parent_sample_path) != parent["private_sample_sha256"]:
        raise UncertaintyError(
            "canonical private sample differs from targeted design")
    if sha256_file(parent_match_path) != parent["blinded_match_list_sha256"]:
        raise UncertaintyError(
            "canonical blinded match list differs from targeted design")
    sample = _load_json(parent_sample_path)
    match = _load_json(parent_match_path)
    if (sample.get("schema_version"), sample.get("kind")) != (
            1, "co2gap-selection-validation-sample"):
        raise UncertaintyError("unknown canonical sample contract")
    if (match.get("schema_version"), match.get("kind")) != (
            1, "co2gap-selection-validation-match-list"):
        raise UncertaintyError("unknown canonical match-list contract")
    if (sample.get("release_id") != design.get("release_id")
            or match.get("release_id") != design.get("release_id")
            or match.get("sample_sha256") != sha256_file(parent_sample_path)
            or sample.get("sample_rows") != parent["canonical_sample_rows"]):
        raise UncertaintyError(
            "canonical artifacts differ from targeted release contract")
    if (match.get("gate_status_disclosed") is not False
            or match.get("primary_track_quality_disclosed") is not False):
        raise UncertaintyError(
            "canonical match list discloses gate or primary quality")
    sample_rows = sample.get("rows")
    match_rows = match.get("rows")
    if not isinstance(sample_rows, list) or not isinstance(match_rows, list):
        raise UncertaintyError("canonical artifacts lack rows")
    sample_frame = pd.DataFrame(sample_rows)
    match_frame = pd.DataFrame(match_rows)
    for frame, label in ((sample_frame, "canonical sample"),
                         (match_frame, "canonical match list")):
        _require_columns(frame, ("sample_id",), label)
        if frame.sample_id.duplicated().any():
            raise UncertaintyError(f"{label} has duplicate sample ids")
    if (set(sample_frame.sample_id.astype(str))
            != set(match_frame.sample_id.astype(str))):
        raise UncertaintyError(
            "canonical sample and match-list ids differ")
    _require_columns(
        sample_frame,
        ("failure_mask", "gate_pass", "stratum", "population_n",
         "sample_n", "weight"),
        "canonical sample")
    allowed_match = design["blinding"]["external_matching_allowed_fields"]
    _require_columns(match_frame, allowed_match, "canonical match list")
    disclosed = sorted(
        set(design["blinding"]["external_matching_prohibited_fields"])
        .intersection(match_frame.columns))
    if disclosed:
        raise UncertaintyError(
            f"canonical match list discloses targeted design fields: {disclosed}")

    targets = {
        row["failure_mask"]: int(row["target_rows"])
        for row in design["target_allocation"]
    }
    parent_counts = sample_frame.failure_mask.astype(str).value_counts().to_dict()
    for mask in TARGETED_VALIDATION_MASKS:
        if int(parent_counts.get(mask, 0)) < targets[mask]:
            raise UncertaintyError(
                f"canonical sample lacks targeted rows for mask {mask}")
    selected_parts = []
    controls = sample_frame.loc[
        sample_frame.failure_mask.astype(str) == "0000"].copy()
    control_allocation = _targeted_control_allocation(
        controls, targets["0000"])
    namespace = design["control_selection"]["namespace"]
    for stratum, group in controls.groupby("stratum", sort=True):
        n_target = control_allocation[str(stratum)]
        ranked = group.copy()
        ranked["_target_rank"] = [
            hashlib.sha256(
                f"{namespace}\0{sample_id}".encode()).hexdigest()
            for sample_id in ranked.sample_id.astype(str)
        ]
        chosen = ranked.sort_values(
            ["_target_rank", "sample_id"]).head(n_target).drop(
                columns=["_target_rank"])
        chosen["targeted_sample_n"] = n_target
        chosen["targeted_weight"] = (
            pd.to_numeric(chosen.population_n, errors="raise") / n_target)
        selected_parts.append(chosen)
    for mask in TARGETED_VALIDATION_MASKS[1:]:
        chosen = sample_frame.loc[
            sample_frame.failure_mask.astype(str) == mask].copy()
        if len(chosen) != targets[mask]:
            raise UncertaintyError(
                f"targeted design requires every canonical row for mask {mask}")
        chosen["targeted_sample_n"] = pd.to_numeric(
            chosen.sample_n, errors="raise").astype(int)
        chosen["targeted_weight"] = pd.to_numeric(
            chosen.weight, errors="raise").astype(float)
        selected_parts.append(chosen)
    targeted = pd.concat(selected_parts, ignore_index=True)
    targeted["failure_mask"] = targeted.failure_mask.astype(str)
    targeted = targeted.sort_values("sample_id").reset_index(drop=True)
    observed_counts = targeted.failure_mask.value_counts().to_dict()
    if (len(targeted) != design["target_rows"]
            or any(int(observed_counts.get(mask, 0)) != targets[mask]
                   for mask in TARGETED_VALIDATION_MASKS)
            or targeted.sample_id.duplicated().any()):
        raise UncertaintyError("targeted private sample does not close")
    for stratum, group in targeted.groupby("stratum", sort=True):
        weights = pd.to_numeric(group.targeted_weight, errors="coerce")
        if not np.isfinite(weights.to_numpy(float)).all() or (weights <= 0).any():
            raise UncertaintyError(
                f"targeted sample has invalid weights in {stratum}")

    private_rows = []
    for row in targeted.itertuples(index=False):
        private_rows.append({
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
            "canonical_sample_n": int(row.sample_n),
            "canonical_weight": float(row.weight),
            "targeted_sample_n": int(row.targeted_sample_n),
            "targeted_weight": float(row.targeted_weight),
        })
    private_value = {
        "schema_version": 1,
        "kind": "co2gap-targeted-heldout-sample",
        "publication_status": "private_per_flight",
        "analysis_status": "awaiting_external_outcomes",
        "release_id": design["release_id"],
        "targeted_design_sha256": sha256_file(design_path),
        "parent_sample_sha256": sha256_file(parent_sample_path),
        "parent_match_list_sha256": sha256_file(parent_match_path),
        "sample_rows": int(len(targeted)),
        "privacy": (
            "Contains release-local keys, failure masks and weights. Keep outside "
            "git and never give this file to the external matcher."),
        "rows": private_rows,
    }
    _atomic_json(output, private_value)
    sample_hash = sha256_file(output)
    selected_ids = set(targeted.sample_id.astype(str))
    blinded = match_frame.loc[
        match_frame.sample_id.astype(str).isin(selected_ids), allowed_match].copy()
    blinded = blinded.sort_values("sample_id").reset_index(drop=True)
    if len(blinded) != len(targeted):
        raise UncertaintyError("targeted blinded match list does not close")
    blinded_rows = []
    for row in blinded.to_dict(orient="records"):
        blinded_rows.append({
            name: (int(row[name]) if name in {"dep_ts", "arr_ts"}
                   else float(row[name]) if name in {
                       "o_lat", "o_lon", "d_lat", "d_lon"}
                   else str(row[name]))
            for name in allowed_match
        })
    match_value = {
        "schema_version": 1,
        "kind": "co2gap-targeted-heldout-match-list",
        "publication_status": "private_per_flight",
        "release_id": design["release_id"],
        "targeted_design_sha256": sha256_file(design_path),
        "sample_sha256": sample_hash,
        "gate_status_disclosed": False,
        "primary_track_quality_disclosed": False,
        "matching_fields": allowed_match[1:],
        "privacy": (
            "Times and endpoints can identify a flight. Share only under the "
            "held-out validation protocol and do not publish."),
        "rows": blinded_rows,
    }
    _atomic_json(match_output, match_value)
    match_hash = sha256_file(match_output)

    by_mask = []
    for mask in TARGETED_VALIDATION_MASKS:
        group = targeted.loc[targeted.failure_mask == mask]
        group_weights = group.targeted_weight.to_numpy(float)
        by_mask.append({
            "failure_mask": mask,
            "sample_rows": int(len(group)),
            "canonical_strata": int(group.stratum.nunique()),
            "targeted_weight_sum": float(group_weights.sum()),
            "maximum_targeted_weight": float(group_weights.max()),
            "kish_effective_sample_size": float(
                group_weights.sum() ** 2 / np.square(group_weights).sum()),
            "minimum_measured_rows": int(
                design["minimum_measured_rows"][mask]),
        })
    registration = {
        "schema_version": 1,
        "kind": "co2gap-targeted-heldout-registration",
        "publication_status": "aggregate_preregistration",
        "analysis_status": "awaiting_external_outcomes",
        "release_id": design["release_id"],
        "targeted_design_sha256": sha256_file(design_path),
        "parent_artifact_sha256": {
            "sample": sha256_file(parent_sample_path),
            "match_list": sha256_file(parent_match_path),
        },
        "private_artifact_sha256": {
            "sample": sample_hash,
            "match_list": match_hash,
        },
        "sample_rows": int(len(targeted)),
        "by_failure_mask": by_mask,
        "gate_status_disclosed_to_matching": False,
        "primary_track_quality_disclosed_to_matching": False,
        "outcome_contract_sha256": design["outcome_contract"]["sha256"],
        "canonical_design_unchanged": True,
        "primary_headline_bias_bounded": False,
        "privacy": (
            "Aggregate registration only; contains no sample id, flight key, "
            "time, endpoint or aircraft type."),
    }
    validate_targeted_validation_registration(
        registration, design, design_path)
    _atomic_json(registration_output, registration)
    return private_value, match_value, registration


def verify_registered_targeted_artifacts(
        registration: dict, *, sample_path: Path, match_path: Path) -> None:
    hashes = registration["private_artifact_sha256"]
    if sha256_file(sample_path) != hashes["sample"]:
        raise UncertaintyError(
            "private targeted-validation sample differs from registration")
    if sha256_file(match_path) != hashes["match_list"]:
        raise UncertaintyError(
            "private targeted-validation match list differs from registration")


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


def _targeted_gap_metrics(values: np.ndarray, weights: np.ndarray | None) -> dict:
    if weights is None:
        totals = values.sum(axis=0)
    else:
        totals = (values * weights[:, None]).sum(axis=0)
    metrics = _proxy_metrics(totals)
    return {
        name: float(metrics[name])
        for name in ("gap_total_pct", "gap_lateral_pct", "gap_vertical_pct")
    }


def targeted_validation_result(
        *, design_path: Path, registration_path: Path, sample_path: Path,
        match_path: Path, outcomes_path: Path) -> dict:
    """Validate and aggregate the frozen targeted held-out diagnostic."""
    design = _load_json(design_path)
    registration = _load_json(registration_path)
    validate_targeted_validation_registration(
        registration, design, design_path)
    verify_registered_targeted_artifacts(
        registration, sample_path=sample_path, match_path=match_path)
    sample = _load_json(sample_path)
    match = _load_json(match_path)
    outcomes = _load_json(outcomes_path)
    if (sample.get("schema_version"), sample.get("kind")) != (
            1, "co2gap-targeted-heldout-sample"):
        raise UncertaintyError("unknown targeted-validation sample contract")
    if (match.get("schema_version"), match.get("kind")) != (
            1, "co2gap-targeted-heldout-match-list"):
        raise UncertaintyError("unknown targeted-validation match-list contract")
    if (outcomes.get("schema_version"), outcomes.get("kind")) != (
            1, "co2gap-targeted-heldout-outcomes"):
        raise UncertaintyError("unknown targeted-validation outcome contract")
    design_hash = sha256_file(design_path)
    if (sample.get("targeted_design_sha256") != design_hash
            or match.get("targeted_design_sha256") != design_hash
            or outcomes.get("targeted_design_sha256") != design_hash):
        raise UncertaintyError(
            "targeted-validation private artifact names another design")
    if (match.get("sample_sha256") != sha256_file(sample_path)
            or outcomes.get("sample_sha256") != sha256_file(sample_path)
            or outcomes.get("match_list_sha256") != sha256_file(match_path)):
        raise UncertaintyError(
            "targeted-validation private artifact hashes do not close")
    if (sample.get("publication_status") != "private_per_flight"
            or match.get("publication_status") != "private_per_flight"
            or outcomes.get("publication_status") != "private_per_flight"):
        raise UncertaintyError(
            "targeted-validation private artifacts must remain private_per_flight")
    if (sample.get("release_id") != design.get("release_id")
            or match.get("release_id") != design.get("release_id")):
        raise UncertaintyError(
            "targeted-validation private artifacts name another release")
    if (match.get("gate_status_disclosed") is not False
            or match.get("primary_track_quality_disclosed") is not False):
        raise UncertaintyError(
            "targeted-validation match list discloses gate or primary quality")

    top_allowed = {
        "schema_version", "kind", "publication_status",
        "targeted_design_sha256", "sample_sha256", "match_list_sha256",
        "source", "rows",
    }
    extra_top = sorted(set(outcomes) - top_allowed)
    if extra_top:
        raise UncertaintyError(
            f"targeted-validation outcomes have unknown fields: {extra_top}")
    source = outcomes.get("source")
    source_allowed = {
        "name", "version", "method_reference", "quality_rule",
        "licence_or_permission_reference", "receiver_provenance_statement",
        "matching_profile", "source_quality_profile", "processing_profile",
        "source_relation", "primary_trajectory_used",
        "matching_received_gate_status",
        "matching_received_primary_track_quality",
        "protocol_frozen_before_source_access",
    }
    if not isinstance(source, dict):
        raise UncertaintyError("targeted-validation outcomes lack source declarations")
    extra_source = sorted(set(source) - source_allowed)
    if extra_source:
        raise UncertaintyError(
            f"targeted-validation source has unknown fields: {extra_source}")
    for field in (
            "name", "version", "method_reference", "quality_rule",
            "licence_or_permission_reference", "receiver_provenance_statement"):
        if not isinstance(source.get(field), str) or not source[field].strip():
            raise UncertaintyError(
                f"targeted-validation outcome source lacks {field}")
    required_source = {
        "matching_profile": design["matching"]["required_profile"],
        "source_quality_profile": design["source_quality"]["required_profile"],
        "processing_profile": design["proxy_model"]["required_profile"],
        "primary_trajectory_used": False,
        "matching_received_gate_status": False,
        "matching_received_primary_track_quality": False,
        "protocol_frozen_before_source_access": True,
    }
    for field, expected in required_source.items():
        if source.get(field) != expected:
            raise UncertaintyError(
                f"targeted-validation source must declare {field}={expected!r}")
    if source.get("source_relation") not in TARGETED_SOURCE_RELATIONS:
        raise UncertaintyError(
            "targeted-validation source relation is unknown")

    sample_rows = sample.get("rows")
    match_rows = match.get("rows")
    outcome_rows = outcomes.get("rows")
    for rows, label in ((sample_rows, "sample"), (match_rows, "match list"),
                        (outcome_rows, "outcomes")):
        if not isinstance(rows, list) or not rows:
            raise UncertaintyError(f"targeted-validation {label} has no rows")
    sample_frame = pd.DataFrame(sample_rows)
    match_frame = pd.DataFrame(match_rows)
    outcome_frame = pd.DataFrame(outcome_rows)
    for frame, label in ((sample_frame, "sample"), (match_frame, "match list"),
                         (outcome_frame, "outcomes")):
        _require_columns(frame, ("sample_id",), f"targeted-validation {label}")
        if frame.sample_id.duplicated().any():
            raise UncertaintyError(
                f"targeted-validation {label} has duplicate ids")
        ids = frame.sample_id.astype(str)
        if any(len(value) != 24 or set(value) - set("0123456789abcdef")
               for value in ids):
            raise UncertaintyError(
                f"targeted-validation {label} has invalid ids")
    expected_ids = set(sample_frame.sample_id.astype(str))
    if (set(match_frame.sample_id.astype(str)) != expected_ids
            or set(outcome_frame.sample_id.astype(str)) != expected_ids):
        raise UncertaintyError(
            "targeted-validation sample, match and outcome ids differ")
    if len(sample_frame) != design["target_rows"]:
        raise UncertaintyError(
            "targeted-validation sample row count differs from design")
    _require_columns(
        sample_frame,
        ("failure_mask", "gate_pass", "stratum", "targeted_weight"),
        "targeted-validation sample")
    weights = pd.to_numeric(sample_frame.targeted_weight, errors="coerce")
    if (not np.isfinite(weights.to_numpy(float)).all() or (weights <= 0).any()):
        raise UncertaintyError("targeted-validation sample has invalid weights")
    sample_frame["targeted_weight"] = weights
    sample_masks = sample_frame.failure_mask.astype(str)
    if (set(sample_masks) != set(TARGETED_VALIDATION_MASKS)
            or any(bool(value) is not (mask == "0000")
                   for value, mask in zip(sample_frame.gate_pass, sample_masks))):
        raise UncertaintyError(
            "targeted-validation sample has invalid gate partition")
    disclosed = sorted(
        set(design["blinding"]["external_matching_prohibited_fields"])
        .intersection(match_frame.columns))
    if disclosed:
        raise UncertaintyError(
            f"targeted-validation match list discloses design fields: {disclosed}")
    if set(match_frame.columns) != set(
            design["blinding"]["external_matching_allowed_fields"]):
        raise UncertaintyError(
            "targeted-validation match list fields differ from frozen blinding")

    _require_columns(outcome_frame, ("status",), "targeted-validation outcomes")
    unknown_statuses = sorted(
        set(outcome_frame.status.astype(str)) - VALIDATION_OUTCOME_STATUSES)
    if unknown_statuses:
        raise UncertaintyError(
            f"targeted-validation outcomes contain unknown statuses: {unknown_statuses}")
    row_allowed = {
        "sample_id", "status", "real_co2_kg", "ideal_co2_kg",
        "hybrid_co2_kg", "match_candidate_count",
        "match_mutual_nearest", "primary_runner_up_score",
        "source_runner_up_score",
        "departure_time_delta_s", "arrival_time_delta_s",
        "origin_distance_km", "destination_distance_km",
        "proxy_coverage_fraction", "source_unique_points",
        "source_flown_distance_km", "source_gc_distance_km",
        "source_max_segment_speed_kt", "proxy_quality_pass", "reason",
    }
    extra_rows = sorted(set(outcome_frame.columns) - row_allowed)
    if extra_rows:
        raise UncertaintyError(
            f"targeted-validation outcome rows have unknown fields: {extra_rows}")
    joined = sample_frame.merge(
        outcome_frame, on="sample_id", how="left", validate="one_to_one")
    measured = joined.status == "measured"
    for row in joined.loc[~measured].itertuples(index=False):
        if not isinstance(getattr(row, "reason", None), str) or not row.reason.strip():
            raise UncertaintyError(
                f"targeted non-measured outcome lacks a reason: {row.sample_id}")
        for field in ("real_co2_kg", "ideal_co2_kg", "hybrid_co2_kg"):
            value = getattr(row, field, None)
            if value is not None and not pd.isna(value):
                raise UncertaintyError(
                    f"targeted non-measured outcome contains {field}: {row.sample_id}")
    if measured.any():
        measured_fields = (
            "real_co2_kg", "ideal_co2_kg", "hybrid_co2_kg",
            "match_candidate_count", "match_mutual_nearest",
            "primary_runner_up_score", "source_runner_up_score",
            "departure_time_delta_s",
            "arrival_time_delta_s", "origin_distance_km",
            "destination_distance_km", "proxy_coverage_fraction",
            "source_unique_points", "source_flown_distance_km",
            "source_gc_distance_km", "source_max_segment_speed_kt",
            "proxy_quality_pass",
        )
        _require_columns(joined, measured_fields, "targeted measured outcomes")
        values = joined.loc[measured, [
            "real_co2_kg", "ideal_co2_kg", "hybrid_co2_kg"]].apply(
                pd.to_numeric, errors="coerce").to_numpy(float)
        if (not np.isfinite(values).all() or (values < 0).any()
                or (values[:, 1] <= 0).any()):
            raise UncertaintyError(
                "targeted measured outcomes contain invalid CO2 components")
        candidates = pd.to_numeric(
            joined.loc[measured, "match_candidate_count"], errors="coerce")
        diagnostics = joined.loc[measured, [
            "departure_time_delta_s", "arrival_time_delta_s",
            "origin_distance_km", "destination_distance_km",
            "proxy_coverage_fraction", "source_unique_points",
            "source_flown_distance_km", "source_gc_distance_km",
            "source_max_segment_speed_kt"]].apply(
                pd.to_numeric, errors="coerce")
        if (candidates.isna().any() or (candidates < 1).any()
                or (candidates % 1 != 0).any()):
            raise UncertaintyError(
                "targeted measured outcomes have invalid match candidate counts")
        if (not np.isfinite(diagnostics.to_numpy(float)).all()
                or (diagnostics < 0).any().any()
                or (diagnostics.proxy_coverage_fraction > 1).any()):
            raise UncertaintyError(
                "targeted measured outcomes contain invalid diagnostics")
        matching = design["matching"]
        if ((diagnostics.departure_time_delta_s
             > matching["candidate_max_departure_delta_s"]).any()
                or (diagnostics.arrival_time_delta_s
                    > matching["candidate_max_arrival_delta_s"]).any()
                or (diagnostics.origin_distance_km
                    > matching["candidate_max_origin_distance_km"]).any()
                or (diagnostics.destination_distance_km
                    > matching["candidate_max_destination_distance_km"]).any()):
            raise UncertaintyError(
                "targeted measured outcome exceeds frozen matching limits")
        score = (
            diagnostics.departure_time_delta_s / 900.0
            + diagnostics.arrival_time_delta_s / 900.0
            + diagnostics.origin_distance_km / 25.0
            + diagnostics.destination_distance_km / 25.0)
        if (score > matching["maximum_score"]).any():
            raise UncertaintyError(
                "targeted measured outcome exceeds frozen matching score")
        mutual = joined.loc[measured, "match_mutual_nearest"].tolist()
        if not all(isinstance(value, (bool, np.bool_)) and bool(value)
                   for value in mutual):
            raise UncertaintyError(
                "targeted measured outcomes must be mutual nearest matches")
        primary_runner = pd.to_numeric(
            joined.loc[measured, "primary_runner_up_score"], errors="coerce")
        source_runner = pd.to_numeric(
            joined.loc[measured, "source_runner_up_score"], errors="coerce")
        singleton = candidates == 1
        if ((~primary_runner.loc[singleton].isna()).any()
                or (~source_runner.loc[singleton].isna()).any()):
            raise UncertaintyError(
                "targeted singleton matches cannot have runner-up scores")
        multiple = ~singleton
        if multiple.any():
            primary_multiple = primary_runner.loc[multiple]
            source_multiple = source_runner.loc[multiple]
            multiple_score = score.loc[multiple]
            if (primary_multiple.isna().any() or source_multiple.isna().any()
                    or (primary_multiple <= 0).any()
                    or (source_multiple <= 0).any()
                    or (multiple_score > 0.75 * primary_multiple).any()
                    or (multiple_score > 0.75 * source_multiple).any()):
                raise UncertaintyError(
                    "targeted non-singleton match fails frozen runner-up rule")
        source_quality = design["source_quality"]
        unique_points = diagnostics.source_unique_points
        if ((unique_points % 1 != 0).any()
                or (unique_points
                    < source_quality["minimum_unique_points"]).any()
                or (diagnostics.proxy_coverage_fraction
                    < source_quality["coverage_min_fraction"]).any()
                or (diagnostics.source_gc_distance_km
                    < source_quality["great_circle_min_km"]).any()
                or (diagnostics.source_flown_distance_km
                    < source_quality["flown_min_fraction_of_source_gc"]
                    * diagnostics.source_gc_distance_km).any()
                or (diagnostics.source_max_segment_speed_kt
                    > source_quality["maximum_segment_speed_kt"]).any()):
            raise UncertaintyError(
                "targeted measured outcome fails frozen source-quality thresholds")
        quality = joined.loc[measured, "proxy_quality_pass"].tolist()
        if not all(isinstance(value, (bool, np.bool_)) and bool(value)
                   for value in quality):
            raise UncertaintyError(
                "targeted measured outcomes must pass frozen source quality")
        joined.loc[measured, [
            "real_co2_kg", "ideal_co2_kg", "hybrid_co2_kg"]] = values

    response_by_mask = []
    proxy_by_mask = {}
    minimum = design["minimum_measured_rows"]
    all_support = True
    for mask in TARGETED_VALIDATION_MASKS:
        group = joined.loc[joined.failure_mask.astype(str) == mask]
        group_measured = group.status == "measured"
        n_measured = int(group_measured.sum())
        meets = n_measured >= int(minimum[mask])
        all_support = all_support and meets
        row = {
            "failure_mask": mask,
            "requested_rows": int(len(group)),
            "measured_rows": n_measured,
            "measured_row_share": float(group_measured.mean()),
            "measured_targeted_weight_share": float(
                group.loc[group_measured, "targeted_weight"].sum()
                / group.targeted_weight.sum()),
            "minimum_measured_rows": int(minimum[mask]),
            "minimum_support_met": bool(meets),
            "status_counts": {
                str(status): int(count)
                for status, count in group.status.value_counts().sort_index().items()
            },
        }
        if n_measured:
            measured_group = group.loc[group_measured]
            proxy_values = measured_group[[
                "real_co2_kg", "ideal_co2_kg",
                "hybrid_co2_kg"]].to_numpy(float)
            proxy_by_mask[mask] = {
                "weighted_conditional_gap_pct": _targeted_gap_metrics(
                    proxy_values,
                    measured_group.targeted_weight.to_numpy(float)),
                "unweighted_conditional_gap_pct": _targeted_gap_metrics(
                    proxy_values, None),
            }
            row.update(proxy_by_mask[mask])
        response_by_mask.append(row)

    status = (
        "complete_targeted_diagnostic" if all_support
        else "blocked_insufficient_mask_support")
    result = {
        "schema_version": 1,
        "kind": "co2gap-targeted-heldout-validation-result",
        "publication_status": "aggregate_diagnostic",
        "analysis_status": status,
        "release_id": design["release_id"],
        "targeted_design_sha256": design_hash,
        "registration_sha256": sha256_file(registration_path),
        "input_hashes": {
            "sample": sha256_file(sample_path),
            "match_list": sha256_file(match_path),
            "outcomes": sha256_file(outcomes_path),
        },
        "source": source,
        "source_relation_verified_by_code": False,
        "response_by_failure_mask": response_by_mask,
        "rejected_masks_pooled": False,
        "primary_headline_bias_bounded": False,
        "claims": design["claims"],
        "limitations": [
            "Every proxy is conditional on external matching and frozen source quality; targeted weights do not correct source nonresponse.",
            "Minimum measured-row thresholds provide descriptive support, not unbiasedness or statistical coverage.",
            "Receiver provenance is declared by the source and cannot be verified by this code.",
            "The tranche excludes rare failure masks and all historical upstream ingestion exclusions.",
            "No result from this targeted diagnostic corrects or bounds the frozen release headline."
        ],
        "privacy": (
            "Aggregate result only; no sample id, flight key, time, endpoint or "
            "provider track id is emitted."),
    }
    if all_support:
        control = proxy_by_mask["0000"]
        contrasts = []
        for mask in TARGETED_VALIDATION_MASKS[1:]:
            row = {"failure_mask": mask}
            for basis in (
                    "weighted_conditional_gap_pct",
                    "unweighted_conditional_gap_pct"):
                row[f"{basis}_minus_0000_percentage_points"] = {
                    metric: proxy_by_mask[mask][basis][metric] - control[basis][metric]
                    for metric in (
                        "gap_total_pct", "gap_lateral_pct", "gap_vertical_pct")
                }
            contrasts.append(row)
        result["conditional_contrasts"] = contrasts
        result["interpretation"] = (
            "Held-out mask-specific conditional diagnostic. Weighted contrasts are "
            "primary; unweighted contrasts diagnose composition. Neither is a "
            "release-wide correction or bound.")
    else:
        result["blocked_reason"] = (
            "At least one frozen failure mask has fewer measured outcomes than "
            "its pre-source minimum. No mask contrast is released.")
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


def _new_mass_accumulator() -> dict[str, float | int]:
    return {
        "sample_flights": 0, "supported_sample_flights": 0,
        "expanded_weight": 0.0, "supported_expanded_weight": 0.0,
        "requested_fraction_weighted": 0.0,
        "requested_abs_fraction_weighted": 0.0,
        "realised_real_fraction_weighted": 0.0,
        "realised_ideal_fraction_weighted": 0.0,
        "realised_hybrid_fraction_weighted": 0.0,
        "real_capped_weight": 0.0, "ideal_capped_weight": 0.0,
        "hybrid_capped_weight": 0.0,
        "real_capped_sample_flights": 0, "ideal_capped_sample_flights": 0,
        "hybrid_capped_sample_flights": 0,
    }


def _accumulate_mass(acc: dict, *, weight: float, supported: bool,
                     requested_fraction: float, realised: dict,
                     caps: dict) -> None:
    acc["sample_flights"] += 1
    acc["supported_sample_flights"] += int(supported)
    acc["expanded_weight"] += weight
    acc["supported_expanded_weight"] += weight * int(supported)
    acc["requested_fraction_weighted"] += weight * requested_fraction
    acc["requested_abs_fraction_weighted"] += weight * abs(requested_fraction)
    for name in ("real", "ideal", "hybrid"):
        acc[f"realised_{name}_fraction_weighted"] += weight * realised[name]
        acc[f"{name}_capped_weight"] += weight * int(caps[name])
        acc[f"{name}_capped_sample_flights"] += int(caps[name])


def _finish_mass_accumulator(acc: dict, scenario_id: str) -> dict:
    weight = float(acc["expanded_weight"])
    supported_weight = float(acc["supported_expanded_weight"])
    if weight <= 0:
        raise UncertaintyError(
            f"combined-mass scenario {scenario_id} has no expanded weight")
    return {
        "sample_flights": int(acc["sample_flights"]),
        "supported_sample_flights": int(acc["supported_sample_flights"]),
        "expanded_weight": weight,
        "supported_expanded_weight": supported_weight,
        "supported_expanded_weight_fraction": supported_weight / weight,
        "requested_mean_pp_mtow_all_flights": (
            100.0 * acc["requested_fraction_weighted"] / weight),
        "requested_mean_abs_pp_mtow_all_flights": (
            100.0 * acc["requested_abs_fraction_weighted"] / weight),
        "requested_mean_pp_mtow_supported_flights": (
            100.0 * acc["requested_fraction_weighted"] / supported_weight
            if supported_weight > 0 else None),
        "realised_mean_shift_pp_mtow": {
            name: 100.0 * acc[f"realised_{name}_fraction_weighted"] / weight
            for name in ("real", "ideal", "hybrid")
        },
        "mtow_cap": {
            name: {
                "sample_flights": int(acc[f"{name}_capped_sample_flights"]),
                "expanded_weight_fraction": acc[f"{name}_capped_weight"] / weight,
            } for name in ("real", "ideal", "hybrid")
        },
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


def _accumulate_flight(acc: dict, weight: float, real: float, ideal: float,
                       hybrid: float, factor: float) -> None:
    acc["sample_flights"] += 1
    acc["weight"] += weight
    for name, value in (("real", real), ("ideal", ideal), ("hybrid", hybrid)):
        acc[f"{name}_u"] += weight * value
        acc[f"{name}_c"] += weight * value * factor


def _require_nominal_baseline(stored: float, recomputed: float, label: str) -> None:
    """Refuse a sensitivity whose reference differs beyond numerical roundoff."""
    if (not math.isfinite(stored) or not math.isfinite(recomputed)
            or stored <= 0 or recomputed <= 0
            or not math.isclose(stored, recomputed, rel_tol=1e-10, abs_tol=1e-6)):
        raise UncertaintyError(
            f"nominal baseline mismatch for {label}: stored={stored}, "
            f"recomputed={recomputed}; sensitivity cannot use a changed reference")


def _check_corrected_wind_reference(row: pd.Series, nominal: dict,
                                    result: dict, label: str) -> dict:
    """Replay stored scalar winds; permit only wind-driven nominal fuel changes.

    This does not restore historical ERA5 extrapolation. The frozen mean winds
    are already in the immutable parquet. Replaying the fuel/profile calculation
    first checks provenance; replay at the newly sampled winds then verifies
    that the new nominal has not changed anything downstream of those winds.
    Neither replay provides independent validation of the physical model.
    """
    from emissions import estimate_fuel
    from excess_wind import _build_profile

    def fuel(distance: float, wind: float) -> float:
        profile = _build_profile(str(row.typecode), distance,
                                 float(row.cruise_alt_ft), wind)
        if profile is None:
            raise UncertaintyError(f"stored-wind replay cannot build profile: {label}")
        estimate = estimate_fuel(
            profile, load_factor=float(nominal["load_factor"]),
            reserve_kg=float(nominal["reserve_kg"]), tas_mode="gs")
        if not estimate.ok:
            raise UncertaintyError(f"stored-wind replay fuel failed: {label}")
        return float(estimate.co2_kg)

    checks = {}
    for name, distance_key, wind_key, co2_key in (
            ("ideal", "gc_km", "mean_wpar_gc_ms", "ideal_gc_co2_kg"),
            ("hybrid", "flown_km", "mean_wpar_track_ms", "hybrid_co2_kg")):
        old_wind = _finite_number(row.get(wind_key), f"{label}/stored/{wind_key}")
        new_wind = _finite_number(result.get(wind_key), f"{label}/corrected/{wind_key}")
        stored = _finite_number(row.get(co2_key), f"{label}/stored/{co2_key}")
        replay = fuel(float(row[distance_key]), old_wind)
        _require_nominal_baseline(stored, replay, f"{label}/stored-wind-replay/{name}")
        corrected_replay = (replay if new_wind == old_wind else
                            fuel(float(row[distance_key]), new_wind))
        _require_nominal_baseline(
            corrected_replay, float(result[co2_key]),
            f"{label}/corrected-wind-only/{name}")
        checks[name] = {
            "replay_abs_kg": abs(replay - stored),
            "wind_abs_delta_ms": abs(new_wind - old_wind),
        }
    return checks


def _verified_precision_sample(sample, manifest, flights_dir, decomposition_dir):
    """Reproduce the declared draw from the verified frame, not just its weights."""
    if sample.get("source_manifest_verified") is not True:
        raise UncertaintyError("sampling precision requires a verified sample manifest")
    seed = sample.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise UncertaintyError("sampling precision requires a nonnegative integer seed")
    design = sample.get("sampling_design")
    if design is None:
        per_stratum = _sampling_integer(sample.get("per_stratum"), "per_stratum")
        target = None
    else:
        if not isinstance(design, dict):
            raise UncertaintyError("invalid precision sampling design")
        per_stratum = _sampling_integer(design.get("minimum_per_stratum"), "minimum", 2)
        target = _sampling_integer(design.get("target_sample_rows"), "target sample")
        if design != {
            "allocation": "minimum-plus-proportional-remaining-capacity",
            "minimum_per_stratum": per_stratum, "target_sample_rows": target,
            "rounding": "integer largest remainders, lexicographic stratum ties",
            "within_stratum": "simple random sampling without replacement",
            "uses_sensitivity_outcomes": False,
        } or "per_stratum" in sample:
            raise UncertaintyError("sampling precision requires the declared SRS design")
    population = build_population(manifest, flights_dir, decomposition_dir)
    draw = stratified_sample(population, per_stratum, seed, target_sample=target)
    expected = [{
        "day": str(row.day), "flight_id": int(row.flight_id),
        "stratum": str(row.stratum), "population_n": int(row.population_n),
        "sample_n": int(row.sample_n), "weight": float(row.weight),
    } for row in draw.itertuples(index=False)]
    if sample.get("rows") != expected or sample.get("population_rows") != len(population):
        raise UncertaintyError("precision sample differs from regenerated frame/draw")


def paired_sensitivity(*, sample_path: Path, scenarios_path: Path,
                       manifest_path: Path, flights_dir: Path,
                       decomposition_dir: Path, ground_dir: Path,
                       era5_dir: Path, calibration: Path,
                       verify: bool, limit: int | None = None,
                       reference_profile: str = "frozen-release",
                       sampling_precision: bool = False,
                       precision_audit_out: Path | None = None) -> dict:
    """Run paired finite differences; return aggregates and no per-flight rows."""
    from decompose import decompose_flight
    from emissions import estimate_fuel
    from wind.era5 import WindField, required_wind_days

    if precision_audit_out is not None:
        if not sampling_precision:
            raise UncertaintyError("precision-audit-out requires sampling-precision")
        _require_outside_repository(precision_audit_out, "private sampling moments")
        if precision_audit_out.exists():
            raise UncertaintyError("private sampling moments output already exists")
    if sampling_precision and (not verify or limit is not None):
        raise UncertaintyError("sampling precision requires verified full inputs and no limit")
    if reference_profile not in SENSITIVITY_REFERENCE_PROFILES:
        raise UncertaintyError(f"unknown sensitivity reference profile: {reference_profile}")
    implementation = {
        name: sha256_file(ROOT / name) for name in (
            "lab/uncertainty.py", "pipeline/decompose.py", "pipeline/excess_wind.py",
            "pipeline/emissions.py", "wind/era5.py",
        )
    }
    if sampling_precision:
        implementation["lab/sampling_precision.py"] = sha256_file(ROOT / "lab/sampling_precision.py")
    # OpenAP's smooth limiter overflows for some extreme ground steps.  The
    # integrator converts those non-finite step flows to zero; this is the same
    # guarded condition suppressed by the production phase-0 runner.
    warnings.filterwarnings("ignore", category=RuntimeWarning,
                            module=r"openap(?:\..*)?$")

    raw_config = _load_json(scenarios_path)
    combined_mass = None
    if raw_config.get("kind") == DESIGN_KIND:
        try:
            combined_mass = validate_combined_mass_design(raw_config, scenarios_path)
        except CombinedMassError as exc:
            raise UncertaintyError(f"invalid combined-mass design: {exc}") from exc
        config = combined_mass["config"]
        implementation["lab/combined_mass.py"] = sha256_file(
            ROOT / "lab/combined_mass.py")
        if reference_profile != combined_mass["reference_profile"]:
            raise UncertaintyError(
                "combined-mass design requires corrected-wind reference profile")
    else:
        config = raw_config
        validate_scenarios(config)

    sample = _load_json(sample_path)
    if sample.get("schema_version") != 1 or sample.get("kind") != "co2gap-uncertainty-sample":
        raise UncertaintyError("unsupported uncertainty sample manifest")
    sample_digest = sha256_file(sample_path)
    registered_mass_seed = None
    if combined_mass is not None:
        registered_mass_seed = combined_mass["registered_samples"].get(sample_digest)
        if registered_mass_seed is None:
            raise UncertaintyError(
                "combined-mass run requires one of the three frozen balanced samples")
        if (sample.get("seed") != registered_mass_seed
                or sample.get("sample_rows") != combined_mass["sample_rows"]
                or sample.get("population_rows") != combined_mass["population_rows"]):
            raise UncertaintyError(
                "combined-mass sample metadata differs from its registration")
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

    scenarios = {entry["id"]: entry for entry in config["scenarios"]}
    nominal = scenarios[config["nominal"]]
    if reference_profile == "corrected-wind":
        for name, value in CORRECTED_WIND_NOMINAL.items():
            if nominal.get(name) != value:
                raise UncertaintyError(f"corrected-wind nominal requires {name}={value}")
        ground_definition = manifest.data.get("configuration", {}).get("ground", {}).get("definition")
        if ground_definition != nominal["ground_definition"]:
            raise UncertaintyError("corrected-wind nominal ground definition differs from release")
    rows = list(sample.get("rows") or [])
    if not rows:
        raise UncertaintyError("sample manifest has no rows")
    precision = None
    if sampling_precision:
        _verified_precision_sample(sample, manifest, flights_dir, decomposition_dir)
        precision = PairedSamplingPrecision(sample, scenarios, config["nominal"])
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
    mass_acc = ({sid: _new_mass_accumulator() for sid in scenarios}
                if combined_mass is not None else None)
    frozen_acc = _new_accumulator()
    replay_checks = {
        "checked_flights": 0,
        "max_abs_ideal_kg": 0.0, "max_abs_hybrid_kg": 0.0,
        "max_abs_ideal_wind_delta_ms": 0.0, "max_abs_hybrid_wind_delta_ms": 0.0,
        "flights_with_ideal_wind_change": 0, "flights_with_hybrid_wind_change": 0,
    }
    failure_reasons: dict[str, int] = defaultdict(int)
    closure = {
        "stored_ideal_u": 0.0, "recomputed_ideal_u": 0.0,
        "stored_hybrid_u": 0.0, "recomputed_hybrid_u": 0.0,
        "max_abs_ideal_kg": 0.0, "max_abs_hybrid_kg": 0.0,
    }
    point_columns = ["flight_id", "t", "lat", "lon", "alt_ft", "gs_kt", "ias_kt", "vs_fpm"]
    decomp_columns = [
        "flight_id", "typecode", "gc_km", "flown_km", "dep_ts", "co2_kg_v0",
        "ideal_gc_co2_kg", "hybrid_co2_kg", "cruise_alt_ft",
    ]
    if reference_profile == "corrected-wind":
        decomp_columns += ["mean_wpar_gc_ms", "mean_wpar_track_ms"]
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
            flights_dir / day / "points.parquet", columns=point_columns,
            filters=[("flight_id", "in", sorted(ids))]).to_pandas()
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
                for scenario_acc in [*acc.values(), frozen_acc]:
                    scenario_acc["failed_flights"] += 1
                failure_reasons["missing_input_key"] += 1
                continue
            row = dec.loc[fid]
            grow = ground.loc[fid]
            flight = _flight_from_points(str(row.typecode), groups[fid])
            base_mass_fraction = 0.0
            mass_supported = False
            mass_cell = None
            if combined_mass is not None:
                try:
                    base_mass_fraction, mass_supported, mass_cell = requested_mass_fraction(
                        str(row.typecode), float(row.flown_km), combined_mass)
                except CombinedMassError as exc:
                    raise UncertaintyError(
                        f"combined-mass lookup failed for {day}/{fid}: {exc}") from exc
            nominal_mass_kwargs = ({"mass_adjustment_frac_mtow": 0.0}
                                   if combined_mass is not None else {})
            nominal_real = estimate_fuel(
                flight, load_factor=float(nominal["load_factor"]),
                reserve_kg=float(nominal["reserve_kg"]),
                tas_mode=str(nominal["real_tas_mode"]),
                **nominal_mass_kwargs,
            )
            if not nominal_real.ok or nominal_real.co2_kg <= 0:
                for scenario_acc in [*acc.values(), frozen_acc]:
                    scenario_acc["failed_flights"] += 1
                failure_reasons["nominal_real_fuel"] += 1
                continue
            anchor = float(row.co2_kg_v0) / nominal_real.co2_kg
            arrays = {
                name: groups[fid][name].to_numpy(np.float64)
                for name in ("lat", "lon", "alt_ft", "ias_kt", "vs_fpm")
            }
            flight_results = {}
            flight_mass_results = {}
            nominal_decomposition = None
            failure = None
            for sid, scenario in scenarios.items():
                pattern_scale = float(scenario.get("combined_mass_pattern_scale", 0.0))
                applied_mass_fraction = base_mass_fraction * pattern_scale
                mass_kwargs = ({"mass_adjustment_frac_mtow": applied_mass_fraction}
                               if combined_mass is not None else {})
                real = estimate_fuel(
                    flight, load_factor=float(scenario["load_factor"]),
                    reserve_kg=float(scenario["reserve_kg"]),
                    tas_mode=str(scenario["real_tas_mode"]),
                    **mass_kwargs,
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
                    cruise_alt_override_ft=float(row.cruise_alt_ft),
                    **({
                        "mass_adjustment_frac_mtow": applied_mass_fraction,
                        "include_mass_diagnostics": True,
                    } if combined_mass is not None else {}),
                )
                if result is None:
                    failure = f"{sid}:decomposition"
                    break
                if sid == config["nominal"]:
                    nominal_decomposition = result
                denominator = float(grow.fuel_recomputed_kg)
                ground_fuel = float(grow[f"fuel_{scenario['ground_definition']}_kg"])
                if denominator <= 0 or not 0 <= ground_fuel <= denominator:
                    failure = f"{sid}:ground_share"
                    break
                ground_share = ground_fuel / denominator
                real_airborne = real_gate * (1.0 - ground_share)
                ideal = float(result["ideal_gc_co2_kg"])
                hybrid = float(result["hybrid_co2_kg"])
                if not all(math.isfinite(value) and value > 0
                           for value in (real_airborne, ideal, hybrid)):
                    failure = f"{sid}:non_positive_result"
                    break
                factor = factors.get(str(row.typecode), 1.0)
                flight_results[sid] = (
                    real_airborne, ideal, hybrid, factor,
                )
                if combined_mass is not None:
                    diagnostic = result.get("combined_mass_diagnostics")
                    if not isinstance(diagnostic, dict):
                        failure = f"{sid}:mass_diagnostics"
                        break
                    mtow = _finite_number(real.mtow_kg, f"{day}/{fid}/{sid}/MTOW")
                    if mtow <= 0 or not math.isclose(
                            _finite_number(diagnostic.get("mtow_kg"),
                                           f"{day}/{fid}/{sid}/baseline MTOW"),
                            mtow, rel_tol=0.0, abs_tol=1e-9):
                        failure = f"{sid}:mass_mtow"
                        break
                    if not math.isclose(
                            _finite_number(diagnostic.get("requested_fraction_mtow"),
                                           f"{day}/{fid}/{sid}/requested mass"),
                            applied_mass_fraction, rel_tol=0.0, abs_tol=1e-12):
                        failure = f"{sid}:mass_request"
                        break
                    flight_mass_results[sid] = {
                        "requested_fraction": applied_mass_fraction,
                        "mtow_kg": mtow,
                        "real_init_mass_kg": _finite_number(
                            real.init_mass_kg, f"{day}/{fid}/{sid}/real initial mass"),
                        "ideal_init_mass_kg": _finite_number(
                            diagnostic.get("ideal_init_mass_kg"),
                            f"{day}/{fid}/{sid}/ideal initial mass"),
                        "hybrid_init_mass_kg": _finite_number(
                            diagnostic.get("hybrid_init_mass_kg"),
                            f"{day}/{fid}/{sid}/hybrid initial mass"),
                        "real_capped": bool(real.mass_capped_at_mtow),
                        "ideal_capped": bool(diagnostic.get("ideal_capped_at_mtow")),
                        "hybrid_capped": bool(diagnostic.get("hybrid_capped_at_mtow")),
                        "cell": mass_cell,
                    }

            # Sensitivities are paired only if every scenario uses the same
            # flight population.  Letting one failed scenario silently drop a
            # row would mix a model effect with a changing sample.
            if failure is not None:
                for scenario_acc in [*acc.values(), frozen_acc]:
                    scenario_acc["failed_flights"] += 1
                failure_reasons[failure] += 1
                continue

            nominal_values = flight_results[config["nominal"]]
            if reference_profile == "frozen-release":
                _require_nominal_baseline(
                    float(row.ideal_gc_co2_kg), nominal_values[1], f"{day}/{fid}/ideal")
                _require_nominal_baseline(
                    float(row.hybrid_co2_kg), nominal_values[2], f"{day}/{fid}/hybrid")
            else:
                checks = _check_corrected_wind_reference(
                    row, nominal, nominal_decomposition, f"{day}/{fid}")
                replay_checks["checked_flights"] += 1
                for name, check in checks.items():
                    replay_checks[f"max_abs_{name}_kg"] = max(
                        replay_checks[f"max_abs_{name}_kg"], check["replay_abs_kg"])
                    replay_checks[f"max_abs_{name}_wind_delta_ms"] = max(
                        replay_checks[f"max_abs_{name}_wind_delta_ms"], check["wind_abs_delta_ms"])
                    replay_checks[f"flights_with_{name}_wind_change"] += int(check["wind_abs_delta_ms"] > 0)
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

            frozen_real = float(row.co2_kg_v0) * (
                1.0 - float(grow[f"fuel_{nominal['ground_definition']}_kg"])
                / float(grow.fuel_recomputed_kg))
            _accumulate_flight(frozen_acc, weight, frozen_real,
                              float(row.ideal_gc_co2_kg), float(row.hybrid_co2_kg),
                              nominal_values[3])
            if precision is not None:
                precision.add((day, fid),
                              {sid: values[:3] for sid, values in flight_results.items()},
                              (frozen_real, float(row.ideal_gc_co2_kg), float(row.hybrid_co2_kg)))
            for sid, values in flight_results.items():
                _accumulate_flight(acc[sid], weight, *values)
            if mass_acc is not None:
                nominal_mass = flight_mass_results[config["nominal"]]
                for sid, value in flight_mass_results.items():
                    mtow = value["mtow_kg"]
                    realised = {
                        name: (
                            value[f"{name}_init_mass_kg"]
                            - nominal_mass[f"{name}_init_mass_kg"]
                        ) / mtow
                        for name in ("real", "ideal", "hybrid")
                    }
                    caps = {
                        name: value[f"{name}_capped"]
                        for name in ("real", "ideal", "hybrid")
                    }
                    _accumulate_mass(
                        mass_acc[sid], weight=weight, supported=mass_supported,
                        requested_fraction=value["requested_fraction"],
                        realised=realised, caps=caps)
        del wind

    results = {sid: _finish_accumulator(value, sid) for sid, value in acc.items()}
    nominal_result = results[config["nominal"]]
    frozen_result = _finish_accumulator(frozen_acc, "frozen-same-sample")
    for sid, result in results.items():
        result["delta_from_nominal"] = {
            name: float(result[name] - nominal_result[name]) for name in METRIC_COLUMNS
        }
    any_failures = any(value["failed_flights"] for value in results.values())
    mass_results = None
    if mass_acc is not None:
        mass_results = {
            "status": "structural_scenario_not_probability_or_correction",
            "source_result_sha256": combined_mass["source_result_sha256"],
            "source_design_sha256": combined_mass["source_design_sha256"],
            "external_dataset_doi": combined_mass["external_dataset_doi"],
            "retained_source_cells": combined_mass["retained_cells"],
            "exact_release_support_fraction_from_tow_validation": (
                combined_mass["exact_release_coverage"]),
            "unsupported_cell_policy": "zero perturbation; no extrapolation",
            "scenarios": {
                sid: _finish_mass_accumulator(value, sid)
                for sid, value in mass_acc.items()
            },
            "limitations": [
                "PRC 2022 selected-airline cell means are transferred to the 2026 release as a structural scenario, not a representative correction.",
                "Payload, reserve and trip fuel are not identified separately.",
                "The nominal stored ground-fuel share is held fixed under the mass perturbation.",
                "Requested and realised shifts can differ because trip fuel is re-iterated and initial mass remains capped at MTOW.",
                "Sample expanded support is an estimate under the existing sensitivity draw; exact source support is reported separately.",
            ],
        }
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
    output = {
        "schema_version": 2,
        "kind": "co2gap-paired-sensitivity",
        "reference_profile": reference_profile,
        "implementation_sha256": implementation,
        "release_id": manifest.release_id,
        "release_manifest_sha256": sha256_file(manifest.path),
        "sample_sha256": sample_digest,
        "scenario_sha256": sha256_file(scenarios_path),
        "sample_rows_requested": len(rows),
        "sample_truncated_for_smoke_test": truncated,
        "source_manifest_verified": bool(verify),
        "population_expansion_complete": not truncated and not any_failures,
        "population_estimate_is_exact": False,
        "sample_design": {
            "allocation": sample.get("sampling_design", {
                "allocation": "historical-equal-cap",
                "per_stratum": sample.get("per_stratum"),
            }),
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
        "frozen_same_sample_reference": frozen_result,
        "nominal_minus_frozen_same_sample": {
            name: float(nominal_result[name] - frozen_result[name]) for name in METRIC_COLUMNS
        },
        "stored_wind_replay": replay_checks if reference_profile == "corrected-wind" else None,
        "common_population_failures": dict(sorted(failure_reasons.items())),
        "nominal_baseline_reconstruction": closure_result,
        "method": {
            "paired_parameters": True,
            "reference_policy": (
                "Strict per-flight closure on frozen ideal/hybrid CO2."
                if reference_profile == "frozen-release" else
                "Experimental corrected-wind nominal: day plus following-day ERA5, "
                "without time extrapolation. Stored-wind fuel replay must reproduce "
                "frozen baselines, and current nominal fuel must reproduce the same "
                "calculation with only the sampled mean winds changed. Altitude, "
                "real-track anchor, ground shares and calibration stay historical. "
                "Not a replacement release or independent model validation."
            ),
            "comparison_order": (
                "First compare nominal_minus_frozen_same_sample (reference change), "
                "then each scenario's delta_from_nominal (finite difference). "
                "Only a separate full-population summary measures sampling error."
            ),
            "baseline_altitude_anchor": (
                "Every scenario starts from the cruise_alt_ft stored for that "
                "release flight, then applies the declared offset. This avoids "
                "re-running the order-sensitive historical altitude cache on "
                "a differently ordered sample."
            ),
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
    if mass_results is not None:
        mass_results["registered_sample_seed"] = registered_mass_seed
        output["combined_mass_pattern"] = mass_results
    if precision is not None:
        totals = {
            sid: [value[f"{name}_u"] for name in ("real", "ideal", "hybrid")]
            for sid, value in {**acc, FROZEN: frozen_acc}.items()
        }
        precision_result, audit = precision.finish(totals)
        precision_result["draw_regenerated_from_verified_frame"] = True
        if precision_audit_out is not None:
            audit["provenance"] = {
                name: output[name] for name in (
                    "sample_sha256", "release_manifest_sha256", "scenario_sha256",
                    "reference_profile", "implementation_sha256", "nominal")
            }
            _atomic_json(precision_audit_out, audit)
            precision_result["private_moments_sha256"] = sha256_file(precision_audit_out)
        output["sampling_precision"] = precision_result
    return output


def _common_release_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--release-manifest", type=Path, required=True)
    parser.add_argument("--decomposition-dir", type=Path, required=True)
    parser.add_argument("--ground-dir", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument(
        "--skip-manifest-verification", action="store_true",
        help="exploratory only: skip checksums; release work must not use this",
    )


def _sample_allocation_options(args) -> tuple[int, int | None]:
    """Refuse contradictory options before reading any release inputs."""
    if args.allocation == "equal":
        if args.target_sample is not None or args.min_per_stratum is not None:
            raise UncertaintyError("equal allocation cannot use target-sample or min-per-stratum")
        cap = 5 if args.per_stratum is None else args.per_stratum
        return _sampling_integer(cap, "per_stratum"), None
    if args.allocation != "balanced":
        raise UncertaintyError(f"unknown sample allocation: {args.allocation}")
    if args.per_stratum is not None:
        raise UncertaintyError("balanced allocation uses min-per-stratum, not per-stratum")
    if args.target_sample is None:
        raise UncertaintyError("balanced allocation requires target-sample")
    minimum = 2 if args.min_per_stratum is None else args.min_per_stratum
    return (_sampling_integer(minimum, "minimum per stratum", 2),
            _sampling_integer(args.target_sample, "target sample"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("validate", help="validate register and diagnostic scenarios")
    check.add_argument("--registry", type=Path, default=ROOT / "uncertainty-register.json")
    check.add_argument("--scenarios", type=Path, default=ROOT / "uncertainty-scenarios.json")
    check.add_argument(
        "--combined-mass-design", type=Path,
        default=ROOT / "combined-mass-sensitivity-design.json")
    check.add_argument(
        "--selection-design", type=Path,
        default=ROOT / "selection-validation-design.json")
    check.add_argument(
        "--selection-sensitivity-design", type=Path,
        default=ROOT / "selection-sensitivity-design.json")
    check.add_argument(
        "--targeted-validation-design", type=Path,
        default=ROOT / "targeted-validation-design.json")
    check.add_argument(
        "--targeted-validation-registration", type=Path,
        default=ROOT / "targeted-validation-registration.json")

    sample_parser = sub.add_parser("sample", help="write a weighted stratified sample manifest")
    sample_parser.add_argument("--release-manifest", type=Path, required=True)
    sample_parser.add_argument("--flights-dir", type=Path, required=True)
    sample_parser.add_argument("--decomposition-dir", type=Path, required=True)
    sample_parser.add_argument("--allocation", choices=["equal", "balanced"], default="equal")
    sample_parser.add_argument("--per-stratum", type=int, default=None,
                               help="equal allocation only: cap per stratum (default 5)")
    sample_parser.add_argument("--min-per-stratum", type=int, default=None,
                               help="balanced only: minimum per stratum (default 2)")
    sample_parser.add_argument("--target-sample", type=int, default=None,
                               help="balanced only: exact total sample size, no silent clipping")
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

    selection_sensitivity_parser = sub.add_parser(
        "selection-sensitivity",
        help="propagate declared gate-failure contrasts as aggregate stresses")
    selection_sensitivity_parser.add_argument(
        "--design", type=Path,
        default=ROOT / "selection-sensitivity-design.json")
    selection_sensitivity_parser.add_argument(
        "--selection-audit", type=Path, required=True)
    selection_sensitivity_parser.add_argument("--out", type=Path, required=True)

    targeted_sample_parser = sub.add_parser(
        "targeted-validation-sample",
        help="derive the private targeted tranche and blinded match list")
    targeted_sample_parser.add_argument("--parent-sample", type=Path, required=True)
    targeted_sample_parser.add_argument(
        "--parent-match-list", type=Path, required=True)
    targeted_sample_parser.add_argument(
        "--design", type=Path,
        default=ROOT / "targeted-validation-design.json")
    targeted_sample_parser.add_argument("--out", type=Path, required=True)
    targeted_sample_parser.add_argument("--match-out", type=Path, required=True)
    targeted_sample_parser.add_argument(
        "--registration-out", type=Path, required=True)
    targeted_sample_parser.add_argument(
        "--registered", type=Path,
        default=ROOT / "targeted-validation-registration.json",
        help="tracked aggregate registration that regenerated outputs must match")

    targeted_parser = sub.add_parser(
        "targeted-validation",
        help="validate external targeted outcomes and emit mask diagnostics")
    targeted_parser.add_argument("--sample", type=Path, required=True)
    targeted_parser.add_argument("--match-list", type=Path, required=True)
    targeted_parser.add_argument("--outcomes", type=Path, required=True)
    targeted_parser.add_argument(
        "--design", type=Path,
        default=ROOT / "targeted-validation-design.json")
    targeted_parser.add_argument(
        "--registration", type=Path,
        default=ROOT / "targeted-validation-registration.json")
    targeted_parser.add_argument("--out", type=Path, required=True)

    sensitivity_parser = sub.add_parser(
        "sensitivity", help="run paired finite-difference scenarios")
    _common_release_arguments(sensitivity_parser)
    sensitivity_parser.add_argument("--flights-dir", type=Path, required=True)
    sensitivity_parser.add_argument("--era5-dir", type=Path, required=True)
    sensitivity_parser.add_argument("--sample", type=Path, required=True)
    sensitivity_parser.add_argument("--scenarios", type=Path,
                                    default=ROOT / "uncertainty-scenarios.json")
    sensitivity_parser.add_argument(
        "--reference-profile", choices=sorted(SENSITIVITY_REFERENCE_PROFILES),
        default="frozen-release",
        help="frozen-release requires closure; corrected-wind is an explicit experimental nominal")
    sensitivity_parser.add_argument("--limit", type=int, default=None,
                                    help="smoke test only; invalidates population estimate")
    sensitivity_parser.add_argument("--out", type=Path, required=True)
    sensitivity_parser.add_argument("--sampling-precision", action="store_true",
                                    help="conditional sampling SEs; verified full draw only, not model uncertainty")
    sensitivity_parser.add_argument("--precision-audit-out", type=Path,
                                    help="optional PRIVATE within-cell moments outside the repository; requires sampling-precision")

    args = parser.parse_args(argv)
    if args.command == "validate":
        registry = validate_registry(_load_json(args.registry))
        scenarios = validate_scenarios(_load_json(args.scenarios))
        try:
            combined_mass_design = load_combined_mass_design(
                args.combined_mass_design)
        except CombinedMassError as exc:
            raise UncertaintyError(f"invalid combined-mass design: {exc}") from exc
        design = validate_selection_design(
            _load_json(args.selection_design), ROOT / "release-manifest.json")
        sensitivity_design = validate_selection_sensitivity_design(
            _load_json(args.selection_sensitivity_design),
            args.selection_sensitivity_design)
        targeted_design_value = _load_json(args.targeted_validation_design)
        targeted_design = validate_targeted_validation_design(
            targeted_design_value, args.targeted_validation_design)
        targeted_registration = validate_targeted_validation_registration(
            _load_json(args.targeted_validation_registration),
            targeted_design_value, args.targeted_validation_design)
        print(f"uncertainty register: {registry['estimands']} estimands, "
              f"{registry['sources']} sources")
        print(f"diagnostic scenarios: {scenarios['scenarios']}, "
              f"nominal={scenarios['nominal']}")
        print(
            "combined-mass structural scenario: "
            f"{combined_mass_design['retained_cells']} TOW cells, "
            f"{combined_mass_design['exact_release_coverage'] * 100:.2f}% "
            "exact release support")
        print(
            f"selection validation: {design['sample_rows']:,} rows in "
            f"{design['strata']:,} strata pre-registered")
        print(
            "selection sensitivity: "
            f"{sensitivity_design['stress_levels']} stress levels, "
            f"{sensitivity_design['mapped_masks']} rejected masks")
        print(
            "targeted held-out validation: "
            f"{targeted_design['target_rows']:,} rows in "
            f"{targeted_registration['masks']} masks registered")
        return 0

    if args.command == "sample":
        per_stratum, target_sample = _sample_allocation_options(args)
        _require_outside_repository(args.out, "uncertainty sample manifest")
        manifest = ReleaseManifest.load(args.release_manifest)
        if not args.skip_manifest_verification:
            manifest.verify_set("flights", args.flights_dir)
            manifest.verify_set("decomposition", args.decomposition_dir, artifact=True)
        population = build_population(manifest, args.flights_dir, args.decomposition_dir)
        sample = stratified_sample(population, per_stratum, args.seed,
                                   target_sample=target_sample)
        value = write_sample_manifest(
            manifest=manifest, population=population, sample=sample,
            per_stratum=per_stratum, seed=args.seed, output=args.out,
            verified=not args.skip_manifest_verification, target_sample=target_sample)
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

    if args.command == "selection-sensitivity":
        result = selection_sensitivity(
            design_path=args.design,
            selection_audit_path=args.selection_audit)
        _atomic_json(args.out, result)
        central = next(
            row for row in result["stress_ladder"] if row["id"] == "centrale")
        signed = central["profiles"]["signed_transfer"][
            "shift_from_frozen_headline_percentage_points"]["gap_total_pct"]
        lower = central["profiles"]["adverse_lower"][
            "shift_from_frozen_headline_percentage_points"]["gap_total_pct"]
        upper = central["profiles"]["adverse_upper"][
            "shift_from_frozen_headline_percentage_points"]["gap_total_pct"]
        print(
            "selection sensitivity: centrale signed "
            f"{signed:+.3f} pp, adverse {lower:+.3f}..{upper:+.3f} pp; "
            f"not a bound -> {args.out}")
        return 0

    if args.command == "targeted-validation-sample":
        sample_value, _, registration = write_targeted_validation_artifacts(
            design_path=args.design,
            parent_sample_path=args.parent_sample,
            parent_match_path=args.parent_match_list,
            output=args.out, match_output=args.match_out,
            registration_output=args.registration_out)
        registered = _load_json(args.registered)
        validate_targeted_validation_registration(
            registered, _load_json(args.design), args.design)
        if registration != registered:
            raise UncertaintyError(
                "regenerated targeted-validation tranche differs from the "
                "tracked aggregate registration")
        counts = ", ".join(
            f"{row['failure_mask']}={row['sample_rows']}"
            for row in registration["by_failure_mask"])
        print(
            f"targeted validation: {sample_value['sample_rows']:,} private rows "
            f"({counts}); blinded match list and aggregate registration written")
        return 0

    if args.command == "targeted-validation":
        result = targeted_validation_result(
            design_path=args.design,
            registration_path=args.registration,
            sample_path=args.sample, match_path=args.match_list,
            outcomes_path=args.outcomes)
        _atomic_json(args.out, result)
        if result["analysis_status"] != "complete_targeted_diagnostic":
            print(
                "targeted validation: BLOCKED because at least one mask is "
                f"below frozen support -> {args.out}")
            return 2
        totals = ", ".join(
            f"{row['failure_mask']}="
            f"{row['weighted_conditional_gap_pct_minus_0000_percentage_points']['gap_total_pct']:+.3f} pp"
            for row in result["conditional_contrasts"])
        print(
            f"targeted validation: weighted conditional total contrasts "
            f"{totals}; not a bound -> {args.out}")
        return 0

    verify = not args.skip_manifest_verification
    if args.command == "release-summary":
        result = release_summary(
            manifest_path=args.release_manifest,
            decomposition_dir=args.decomposition_dir, ground_dir=args.ground_dir,
            calibration=args.calibration, ground_definition=args.ground_definition,
            iterations=args.iterations, seed=args.seed, verify=verify)
    else:
        if args.precision_audit_out is not None and args.precision_audit_out.resolve() == args.out.resolve():
            raise UncertaintyError("private precision audit and aggregate output must differ")
        result = paired_sensitivity(
            sample_path=args.sample, scenarios_path=args.scenarios,
            manifest_path=args.release_manifest, flights_dir=args.flights_dir,
            decomposition_dir=args.decomposition_dir, ground_dir=args.ground_dir,
            era5_dir=args.era5_dir, calibration=args.calibration,
            verify=verify, limit=args.limit, reference_profile=args.reference_profile,
            sampling_precision=args.sampling_precision,
            precision_audit_out=args.precision_audit_out)
    _atomic_json(args.out, result)
    if args.command == "sensitivity":
        print(f"reference profile: {result['reference_profile']}; diagnostic only")
    print(f"{result['kind']}: wrote aggregate result to {args.out}")
    return 0


def cli(argv: list[str] | None = None) -> int:
    """Render expected contract failures without hiding unexpected defects."""
    try:
        return main(argv)
    except (UncertaintyError, SamplingPrecisionError) as exc:
        print(f"uncertainty: ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())
