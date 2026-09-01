"""Validate and load the registered combined-mass structural scenario.

The external TOW result constrains initial mass only as a combined quantity.
This module turns its retained aggregate type/distance cells into deterministic
mass perturbations.  It deliberately does not infer load factor, reserve fuel,
trip fuel or a probability distribution.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path


DESIGN_KIND = "co2gap-combined-mass-structural-sensitivity-design"
RESULT_KIND = "co2gap-takeoff-mass-external-validation-result"
EXPECTED_SCENARIOS = {
    "nominal": 0.0,
    "prc_cell_mean_alignment": 1.0,
}
EXPECTED_SHARED = {
    "load_factor": 0.82,
    "reserve_kg": 2000.0,
    "real_tas_mode": "ias",
    "cruise_alt_offset_ft": 0.0,
    "ground_definition": "a3000t70",
    "reference_profile": "corrected-wind",
}
EXPECTED_SAMPLE_SEEDS = (20260901, 20260902, 20260903)


class CombinedMassError(ValueError):
    """The structural mass scenario violates its frozen contract."""


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(Path(path).read_text())
    except Exception as exc:
        raise CombinedMassError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CombinedMassError(f"{path} must contain a JSON object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _finite(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CombinedMassError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise CombinedMassError(f"{label} must be finite")
    return number


def _source_path(design_path: Path, value, label: str) -> Path:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise CombinedMassError(f"{label} must be a relative file name")
    path = (design_path.parent / value).resolve()
    try:
        path.relative_to(design_path.parent.resolve())
    except ValueError as exc:
        raise CombinedMassError(f"{label} escapes the design directory") from exc
    if not path.is_file():
        raise CombinedMassError(f"missing {label}: {path}")
    return path


def _distance_edges(design: dict) -> list[float]:
    raw = design.get("cell_definition", {}).get("distance_edges_km")
    if not isinstance(raw, list) or len(raw) < 2:
        raise CombinedMassError("distance edges must be a non-empty partition")
    edges = []
    for index, value in enumerate(raw):
        if value == "infinity" and index == len(raw) - 1:
            edges.append(math.inf)
        else:
            edges.append(_finite(value, f"distance_edges_km[{index}]"))
    if edges[0] != 150.0 or any(a >= b for a, b in zip(edges, edges[1:])):
        raise CombinedMassError("distance edges differ from the frozen TOW partition")
    return edges


def _band_label(low: float, high: float) -> str:
    return f"{low:g}-{high:g}" if math.isfinite(high) else f"{low:g}+"


def validate_combined_mass_design(design: dict, design_path: Path) -> dict:
    """Validate design, hashes, source cells and non-claim guards."""
    design_path = Path(design_path).resolve()
    if (design.get("schema_version"), design.get("kind"), design.get("status")) != (
            1, DESIGN_KIND, "frozen_before_co2_scenario_outcomes"):
        raise CombinedMassError("unknown or unfrozen combined-mass design")
    if design.get("chronology", "").find("after the external TOW comparison") < 0:
        raise CombinedMassError("combined-mass chronology must disclose post-TOW design")

    source = design.get("source")
    if not isinstance(source, dict):
        raise CombinedMassError("combined-mass design has no source contract")
    result_path = _source_path(design_path, source.get("result"), "TOW result")
    source_design_path = _source_path(
        design_path, source.get("design"), "TOW validation design")
    if source.get("result_sha256") != _sha256(result_path):
        raise CombinedMassError("TOW result hash differs from frozen design")
    if source.get("design_sha256") != _sha256(source_design_path):
        raise CombinedMassError("TOW validation design hash differs from frozen design")

    shared = design.get("shared_parameters")
    if shared != EXPECTED_SHARED:
        raise CombinedMassError("combined-mass shared parameters changed")
    scenarios = design.get("scenarios")
    if not isinstance(scenarios, list) or len(scenarios) != len(EXPECTED_SCENARIOS):
        raise CombinedMassError("combined-mass design requires exactly two scenarios")
    runtime_scenarios = []
    seen = set()
    for entry in scenarios:
        if not isinstance(entry, dict) or not isinstance(entry.get("purpose"), str):
            raise CombinedMassError("invalid combined-mass scenario")
        sid = entry.get("id")
        scale = _finite(entry.get("combined_mass_pattern_scale"), f"{sid}.scale")
        if sid in seen or sid not in EXPECTED_SCENARIOS or scale != EXPECTED_SCENARIOS[sid]:
            raise CombinedMassError("combined-mass scenario ids or scales changed")
        seen.add(sid)
        runtime_scenarios.append({
            "id": sid,
            "combined_mass_pattern_scale": scale,
            "load_factor": shared["load_factor"],
            "reserve_kg": shared["reserve_kg"],
            "real_tas_mode": shared["real_tas_mode"],
            "cruise_alt_offset_ft": shared["cruise_alt_offset_ft"],
            "ground_definition": shared["ground_definition"],
            "purpose": entry["purpose"],
        })
    if seen != set(EXPECTED_SCENARIOS):
        raise CombinedMassError("combined-mass scenarios are incomplete")

    output = design.get("output_policy")
    if (not isinstance(output, dict)
            or output.get("aggregate_results_may_be_tracked") is not True
            or output.get("per_flight_results_may_be_tracked") is not False
            or output.get("external_rows_committed") is not False
            or output.get("site_or_release_change") is not False
            or output.get("push") is not False):
        raise CombinedMassError("combined-mass output policy was weakened")
    unsupported = design.get("cell_definition", {}).get("unsupported_cell_policy", "")
    if "zero perturbation" not in unsupported or "Do not extrapolate" not in unsupported:
        raise CombinedMassError("unsupported-cell policy must remain zero/no extrapolation")

    sampling = design.get("sampling")
    if (not isinstance(sampling, dict)
            or sampling.get("rows_per_run") != 2549
            or sampling.get("population_rows") != 1833127):
        raise CombinedMassError("combined-mass sampling population changed")
    sample_entries = [sampling.get("primary"), *(sampling.get("robustness") or [])]
    if len(sample_entries) != 3 or any(not isinstance(row, dict) for row in sample_entries):
        raise CombinedMassError("combined-mass design requires three registered samples")
    registered_samples = {}
    for expected_seed, row in zip(EXPECTED_SAMPLE_SEEDS, sample_entries):
        digest = row.get("sha256")
        if (row.get("seed") != expected_seed or not isinstance(digest, str)
                or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)):
            raise CombinedMassError("combined-mass sample registration changed")
        if digest in registered_samples:
            raise CombinedMassError("combined-mass sample hashes are not unique")
        registered_samples[digest] = expected_seed

    result = _load_json(result_path)
    if (result.get("schema_version"), result.get("kind"), result.get("status")) != (
            1, RESULT_KIND, "complete"):
        raise CombinedMassError("TOW result is not the completed registered result")
    if result.get("external_dataset_doi") != source.get("external_dataset_doi"):
        raise CombinedMassError("TOW dataset DOI differs from the frozen design")
    comparison = result.get("comparison")
    if not isinstance(comparison, dict) or comparison.get("status") != "complete":
        raise CombinedMassError("TOW comparison is incomplete")
    cells = comparison.get("cells")
    if (not isinstance(cells, list)
            or comparison.get("retained_cells") != source.get("retained_cells")
            or len(cells) != source.get("retained_cells")):
        raise CombinedMassError("TOW retained-cell count differs from the design")
    if comparison.get("minimum_rows_per_source_per_cell") != source.get(
            "cell_minimum_rows_per_source"):
        raise CombinedMassError("TOW cell minimum differs from the design")
    if not math.isclose(
            _finite(comparison.get("release_row_coverage"), "release coverage"),
            _finite(source.get("exact_release_coverage"), "design release coverage"),
            rel_tol=0.0, abs_tol=1e-12):
        raise CombinedMassError("TOW release coverage differs from the design")

    edges = _distance_edges(design)
    allowed_bands = {
        _band_label(low, high) for low, high in zip(edges, edges[1:])
    }
    minimum = source["cell_minimum_rows_per_source"]
    pattern = {}
    for index, row in enumerate(cells):
        if not isinstance(row, dict):
            raise CombinedMassError(f"TOW cell {index} is not an object")
        typecode, band = row.get("aircraft_type"), row.get("distance_band_km")
        if not isinstance(typecode, str) or not typecode or band not in allowed_bands:
            raise CombinedMassError(f"TOW cell {index} has invalid dimensions")
        key = (typecode, band)
        if key in pattern:
            raise CombinedMassError(f"duplicate TOW cell {typecode}|{band}")
        if row.get("prc_rows", 0) < minimum or row.get("co2gap_rows", 0) < minimum:
            raise CombinedMassError(f"TOW cell {typecode}|{band} is below its floor")
        co2gap = _finite(row.get("co2gap_mean_mass_fraction"), f"{key}.co2gap mean")
        prc = _finite(row.get("prc_mean_mass_fraction"), f"{key}.PRC mean")
        reported = _finite(row.get("co2gap_minus_prc_pp_mtow"), f"{key}.difference")
        if not math.isclose(100.0 * (co2gap - prc), reported,
                            rel_tol=0.0, abs_tol=2e-8):
            raise CombinedMassError(f"TOW cell {typecode}|{band} difference does not close")
        fraction = prc - co2gap
        if abs(fraction) > 0.15:
            raise CombinedMassError(f"TOW cell {typecode}|{band} exceeds frozen scale guard")
        pattern[key] = fraction

    return {
        "config": {
            "schema_version": 1,
            "publication_status": "diagnostic_only",
            "nominal": "nominal",
            "scenarios": runtime_scenarios,
        },
        "pattern": pattern,
        "edges": edges,
        "source_result_sha256": source["result_sha256"],
        "source_design_sha256": source["design_sha256"],
        "external_dataset_doi": source["external_dataset_doi"],
        "exact_release_coverage": source["exact_release_coverage"],
        "retained_cells": source["retained_cells"],
        "reference_profile": shared["reference_profile"],
        "registered_samples": registered_samples,
        "sample_rows": sampling["rows_per_run"],
        "population_rows": sampling["population_rows"],
    }


def load_combined_mass_design(path: Path) -> dict:
    path = Path(path)
    return validate_combined_mass_design(_load_json(path), path)


def distance_band(distance_km: float, edges: list[float]) -> str:
    distance = _finite(distance_km, "flown distance")
    for low, high in zip(edges, edges[1:]):
        if low <= distance < high:
            return _band_label(low, high)
    raise CombinedMassError(f"flown distance {distance:g} km is outside the design partition")


def requested_mass_fraction(typecode: str, distance_km: float,
                            loaded: dict) -> tuple[float, bool, str]:
    if not isinstance(typecode, str) or not typecode.strip():
        raise CombinedMassError("aircraft type is missing")
    band = distance_band(distance_km, loaded["edges"])
    key = (typecode.strip().upper(), band)
    if key not in loaded["pattern"]:
        return 0.0, False, f"{key[0]}|{band}"
    return float(loaded["pattern"][key]), True, f"{key[0]}|{band}"
