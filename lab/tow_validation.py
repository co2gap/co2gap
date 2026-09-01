#!/usr/bin/env python3
"""Externally check co2gap initial mass against the PRC 2024 TOW dataset.

The external rows and release flight rows are inputs only.  This program emits
aggregate diagnostics and refuses to write its result inside the repository.
It does not infer separate payload, reserve or trip-fuel distributions: the
reference TOW observes only their combined contribution to initial mass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from importlib.metadata import version as package_version
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from openap import prop

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "pipeline"), str(ROOT)]

import track_quality  # noqa: E402
from emissions import openap_model  # noqa: E402
from release_manifest import ReleaseManifest, sha256_file  # noqa: E402


class TowValidationError(RuntimeError):
    """The registered comparison cannot be executed as declared."""


NM_TO_KM = 1.852
MODEL_COLUMNS = [
    "flight_id", "day", "typecode", "origin_icao", "dest_icao", "gc_km",
    "flown_km", "coverage_frac", "flown_ge_09gc", "init_mass_kg",
    "load_factor", "reserve_kg",
]


def _json(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except Exception as exc:
        raise TowValidationError(f"cannot read JSON {path}: {exc}") from exc


def _finite_number(value, label: str) -> float:
    if isinstance(value, bool):
        raise TowValidationError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TowValidationError(f"{label} must be a finite number") from exc
    if not math.isfinite(number):
        raise TowValidationError(f"{label} must be a finite number")
    return number


def validate_design(design: dict, design_path: Path,
                    manifest_path: Path) -> dict:
    if design.get("schema_version") != 1:
        raise TowValidationError("unsupported TOW validation design schema")
    if design.get("kind") != "co2gap-takeoff-mass-external-validation-design":
        raise TowValidationError("wrong TOW validation design kind")
    if design.get("status") != "frozen_before_tow_values_were_accessed":
        raise TowValidationError("the TOW design is not frozen before value access")
    source = design.get("external_source", {})
    file_spec = source.get("file", {})
    if (source.get("doi") !=
            "10.4121/8cb8484b-dbe7-4750-8b87-a5b1dbc621b4.v2"):
        raise TowValidationError("unexpected external dataset DOI/version")
    if source.get("license") != "CC BY 4.0":
        raise TowValidationError("unexpected external dataset licence")
    if file_spec.get("name") != "flight_list.csv":
        raise TowValidationError("only the complete flight_list.csv is registered")
    if not isinstance(file_spec.get("bytes"), int) or file_spec["bytes"] <= 0:
        raise TowValidationError("invalid registered external file size")
    md5 = file_spec.get("md5")
    if not isinstance(md5, str) or len(md5) != 32:
        raise TowValidationError("invalid registered external MD5")

    target = design.get("co2gap_target", {})
    actual_manifest_sha = sha256_file(Path(manifest_path))
    if actual_manifest_sha != target.get("release_manifest_sha256"):
        raise TowValidationError(
            "release manifest differs from the one frozen in the TOW design")
    manifest = ReleaseManifest.load(manifest_path)
    if manifest.release_id != target.get("release_id"):
        raise TowValidationError("release id differs from the TOW design")
    if (manifest.data.get("inputs", {}).get("flights", {}).get("sha256") !=
            target.get("flight_input_sha256")):
        raise TowValidationError("flight input digest differs from the TOW design")
    manifest.verify_track_quality(track_quality)
    assumptions = target.get("nominal_assumptions", {})
    if _finite_number(assumptions.get("load_factor"), "load_factor") != 0.82:
        raise TowValidationError("unexpected registered load factor")
    if _finite_number(assumptions.get("reserve_kg"), "reserve_kg") != 2000.0:
        raise TowValidationError("unexpected registered reserve")

    standard = design.get("standardisation", {})
    edges = standard.get("distance_edges_km")
    if not isinstance(edges, list) or len(edges) < 3 or edges[-1] != "infinity":
        raise TowValidationError("distance bands must end at infinity")
    numeric_edges = [_finite_number(x, "distance edge") for x in edges[:-1]]
    if numeric_edges != sorted(set(numeric_edges)):
        raise TowValidationError("distance edges must be increasing and unique")
    minimum = standard.get("minimum_rows_per_source_per_cell")
    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 2:
        raise TowValidationError("invalid minimum rows per cell")
    coverage = _finite_number(
        standard.get("minimum_release_row_coverage"), "coverage guard")
    if not 0 < coverage <= 1:
        raise TowValidationError("coverage guard must lie in (0, 1]")
    precision = design.get("precision", {})
    if not isinstance(precision.get("replicates"), int) or precision["replicates"] < 100:
        raise TowValidationError("too few registered bootstrap replicates")
    if not isinstance(precision.get("seed"), int):
        raise TowValidationError("bootstrap seed must be an integer")
    output = design.get("output_policy", {})
    if (output.get("aggregate_only") is not True or
            output.get("external_rows_committed") is not False or
            output.get("co2gap_flight_rows_committed") is not False or
            output.get("site_or_release_change") is not False):
        raise TowValidationError("unsafe output policy in TOW design")
    if len(design.get("known_noncomparabilities", [])) < 5:
        raise TowValidationError("known non-comparabilities are incomplete")
    return {
        "design_sha256": sha256_file(Path(design_path)),
        "manifest": manifest,
        "distance_edges": numeric_edges + [float("inf")],
        "minimum_rows": minimum,
        "minimum_coverage": coverage,
    }


def require_outside_repository(path: Path, *, may_not_exist: bool = False) -> Path:
    resolved = Path(path).expanduser().resolve(strict=not may_not_exist)
    candidate = resolved if resolved.is_dir() else resolved.parent
    for parent in (candidate, *candidate.parents):
        if (parent / ".git").exists():
            raise TowValidationError(
                f"private/external path must stay outside every git worktree: {path}")
    return resolved


def verify_external_file(path: Path, file_spec: dict) -> dict:
    path = require_outside_repository(path)
    if path.name != file_spec["name"]:
        raise TowValidationError(
            f"external file must be named {file_spec['name']!r}")
    size = path.stat().st_size
    if size != file_spec["bytes"]:
        raise TowValidationError(
            f"external file size {size} differs from {file_spec['bytes']}")
    h = hashlib.md5(usedforsecurity=False)
    sha256 = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(8 * 1024 * 1024):
            h.update(chunk)
            sha256.update(chunk)
    if h.hexdigest() != file_spec["md5"]:
        raise TowValidationError("external file MD5 differs from the registered value")
    return {"bytes": size, "md5": h.hexdigest(),
            "sha256": sha256.hexdigest(), "verified": True}


def require_openap_version(manifest: ReleaseManifest) -> str:
    actual = package_version("openap")
    expected = str(manifest.data["configuration"]["openap_version"])
    if actual != expected:
        raise TowValidationError(
            f"OpenAP {actual} is active; release requires OpenAP {expected}")
    return actual


def aircraft_limits(typecodes, *, lookup=None) -> tuple[dict, list[str]]:
    """Return OEW/MTOW keyed by normalised ICAO type and unsupported types."""
    limits, unsupported = {}, []
    lookup = lookup or prop.aircraft
    for typecode in sorted({str(x).strip().upper() for x in typecodes if pd.notna(x)}):
        model = openap_model(typecode)
        if model is None:
            unsupported.append(typecode)
            continue
        try:
            try:
                aircraft = lookup(model, use_synonym=True)
            except TypeError:
                aircraft = lookup(model)
        except Exception:
            unsupported.append(typecode)
            continue
        oew = _finite_number(aircraft["oew"], f"{typecode} OEW")
        mtow = _finite_number(aircraft["mtow"], f"{typecode} MTOW")
        if not 0 < oew < mtow:
            raise TowValidationError(f"invalid OpenAP mass limits for {typecode}")
        limits[typecode] = {"model": model, "oew_kg": oew, "mtow_kg": mtow}
    return limits, unsupported


def _attach_limits(frame: pd.DataFrame, type_column: str, limits: dict) -> pd.DataFrame:
    out = frame.copy()
    out["aircraft_type"] = out[type_column].astype("string").str.strip().str.upper()
    out["oew_kg"] = out["aircraft_type"].map(
        {key: value["oew_kg"] for key, value in limits.items()})
    out["mtow_kg"] = out["aircraft_type"].map(
        {key: value["mtow_kg"] for key, value in limits.items()})
    return out


def prepare_prc(frame: pd.DataFrame, limits: dict, *, mtow_factor: float = 1.0
                ) -> tuple[pd.DataFrame, dict]:
    required = {"flight_id", "date", "aircraft_type", "flown_distance", "tow"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise TowValidationError(f"PRC file is missing columns: {', '.join(missing)}")
    if frame["flight_id"].duplicated().any():
        raise TowValidationError("PRC flight_id is not unique")
    out = _attach_limits(frame, "aircraft_type", limits)
    out["date_utc"] = pd.to_datetime(out["date"], errors="coerce", utc=True)
    out["distance_km"] = pd.to_numeric(out["flown_distance"], errors="coerce") * NM_TO_KM
    out["mass_kg"] = pd.to_numeric(out["tow"], errors="coerce")

    unsupported = out["mtow_kg"].isna()
    invalid_value = (out["date_utc"].isna() | ~np.isfinite(out["distance_km"]) |
                     ~np.isfinite(out["mass_kg"]))
    wrong_period = out["date_utc"].notna() & (out["date_utc"].dt.year != 2022)
    too_short = np.isfinite(out["distance_km"]) & (out["distance_km"] < 150)
    below_oew = (~unsupported & np.isfinite(out["mass_kg"]) &
                 (out["mass_kg"] < out["oew_kg"]))
    above_mtow = (~unsupported & np.isfinite(out["mass_kg"]) &
                  (out["mass_kg"] > out["mtow_kg"] * mtow_factor))
    eligible = ~(unsupported | invalid_value | wrong_period | too_short |
                 below_oew | above_mtow)
    kept = out.loc[eligible, ["flight_id", "date_utc", "aircraft_type",
                              "distance_km", "mass_kg", "oew_kg", "mtow_kg"]].copy()
    kept["mass_fraction"] = kept["mass_kg"] / kept["mtow_kg"]
    funnel = {
        "input_rows": int(len(out)),
        "unsupported_aircraft_type": int(unsupported.sum()),
        "missing_or_nonfinite_required_value": int(invalid_value.sum()),
        "outside_2022": int(wrong_period.sum()),
        "distance_below_150_km": int(too_short.sum()),
        "mass_below_openap_oew": int(below_oew.sum()),
        "mass_above_allowed_openap_mtow": int(above_mtow.sum()),
        "eligible_rows": int(eligible.sum()),
    }
    return kept, funnel


def load_prc(path: Path, required_columns: list[str]) -> pd.DataFrame:
    try:
        return pd.read_csv(path, usecols=required_columns)
    except Exception as exc:
        raise TowValidationError(f"cannot read external flight list: {exc}") from exc


def load_co2gap(flights_dir: Path, days: list[str]) -> pd.DataFrame:
    frames = []
    for day in days:
        path = Path(flights_dir) / day / "flights.parquet"
        if not path.is_file():
            raise TowValidationError(f"missing co2gap release flight file: {path}")
        try:
            table = pq.read_table(path, columns=MODEL_COLUMNS)
        except Exception as exc:
            raise TowValidationError(f"cannot read {path}: {exc}") from exc
        frame = table.to_pandas()
        if not frame["day"].eq(day).all():
            raise TowValidationError(f"day column does not match directory {day}")
        frames.append(frame)
    if not frames:
        raise TowValidationError("no co2gap release flights loaded")
    return pd.concat(frames, ignore_index=True)


def prepare_co2gap(frame: pd.DataFrame, limits: dict, design: dict
                   ) -> tuple[pd.DataFrame, dict]:
    missing = sorted(set(MODEL_COLUMNS) - set(frame.columns))
    if missing:
        raise TowValidationError(f"co2gap flights are missing: {', '.join(missing)}")
    if frame.duplicated(["day", "flight_id"]).any():
        raise TowValidationError("co2gap day plus flight_id is not unique")
    out = _attach_limits(frame, "typecode", limits)
    for column in ("gc_km", "flown_km", "coverage_frac", "init_mass_kg",
                   "load_factor", "reserve_kg"):
        out[column] = pd.to_numeric(out[column], errors="coerce")
    nominal = design["co2gap_target"]["nominal_assumptions"]
    assumption_values_are_finite = np.isfinite(
        out[["load_factor", "reserve_kg"]]).all(axis=1)
    assumptions = (assumption_values_are_finite &
                   np.isclose(out["load_factor"], nominal["load_factor"], atol=1e-6) &
                   np.isclose(out["reserve_kg"], nominal["reserve_kg"], atol=1e-3))
    if not assumptions.all():
        raise TowValidationError("co2gap rows do not share the frozen mass assumptions")
    finite = np.isfinite(out[["gc_km", "flown_km", "coverage_frac",
                              "init_mass_kg"]]).all(axis=1)
    endpoints = (out["origin_icao"].notna() & out["dest_icao"].notna() &
                 out["origin_icao"].astype("string").str.strip().ne("") &
                 out["dest_icao"].astype("string").str.strip().ne(""))
    quality = (finite & endpoints &
               (out["coverage_frac"] >= track_quality.COV_MIN) &
               out["flown_ge_09gc"].fillna(False).astype(bool) &
               (out["gc_km"] >= track_quality.GC_MIN_KM))
    unsupported = out["mtow_kg"].isna()
    too_short = finite & (out["flown_km"] < 150)
    below_oew = (~unsupported & finite & (out["init_mass_kg"] < out["oew_kg"]))
    above_mtow = (~unsupported & finite & (out["init_mass_kg"] > out["mtow_kg"]))
    eligible = quality & ~unsupported & ~too_short & ~below_oew & ~above_mtow
    kept = out.loc[eligible, ["flight_id", "day", "aircraft_type", "flown_km",
                              "init_mass_kg", "oew_kg", "mtow_kg"]].copy()
    kept = kept.rename(columns={"flown_km": "distance_km",
                                "init_mass_kg": "mass_kg"})
    kept["date_utc"] = pd.to_datetime(kept.pop("day"), utc=True)
    kept["mass_fraction"] = kept["mass_kg"] / kept["mtow_kg"]
    funnel = {
        "input_rows": int(len(out)),
        "quality_gate_failure": int((~quality).sum()),
        "unsupported_aircraft_type": int(unsupported.sum()),
        "distance_below_150_km": int(too_short.sum()),
        "mass_below_openap_oew": int(below_oew.sum()),
        "mass_above_openap_mtow": int(above_mtow.sum()),
        "eligible_rows": int(eligible.sum()),
    }
    return kept, funnel


def _band_labels(edges: list[float]) -> list[str]:
    labels = []
    for low, high in zip(edges[:-1], edges[1:]):
        labels.append(f"{low:g}-{high:g}" if math.isfinite(high) else f"{low:g}+")
    return labels


def add_cells(frame: pd.DataFrame, edges: list[float]) -> pd.DataFrame:
    out = frame.copy()
    labels = _band_labels(edges)
    out["distance_band_km"] = pd.cut(
        out["distance_km"], bins=edges, labels=labels,
        right=False, include_lowest=True).astype("string")
    if out["distance_band_km"].isna().any():
        raise TowValidationError("an eligible distance lies outside registered bands")
    out["cell"] = out["aircraft_type"] + "|" + out["distance_band_km"]
    return out


def _weighted_quantile(values, weights, quantiles) -> list[float]:
    values = np.asarray(values, dtype=float)
    weights = np.asarray(weights, dtype=float)
    if (len(values) == 0 or len(values) != len(weights) or
            not np.isfinite(values).all() or not np.isfinite(weights).all() or
            (weights <= 0).any()):
        raise TowValidationError("invalid weighted-quantile input")
    order = np.argsort(values, kind="mergesort")
    values, weights = values[order], weights[order]
    positions = (np.cumsum(weights) - 0.5 * weights) / weights.sum()
    return [float(np.interp(q, positions, values)) for q in quantiles]


def _summary(values, weights=None) -> dict:
    values = np.asarray(values, dtype=float)
    if weights is None:
        weights = np.ones(len(values), dtype=float)
    weights = np.asarray(weights, dtype=float)
    qs = _weighted_quantile(values, weights, [0.25, 0.5, 0.75])
    return {
        "rows": int(len(values)),
        "mean_mass_fraction": float(np.average(values, weights=weights)),
        "q25_mass_fraction": qs[0],
        "median_mass_fraction": qs[1],
        "q75_mass_fraction": qs[2],
    }


def _group_stats(frame: pd.DataFrame) -> pd.DataFrame:
    return (frame.groupby(["cell", "aircraft_type", "distance_band_km"],
                          observed=True)["mass_fraction"]
            .agg(["size", "mean"])
            .rename(columns={"size": "rows", "mean": "mean_mass_fraction"})
            .reset_index())


def compare_sources(prc: pd.DataFrame, model: pd.DataFrame, *, minimum_rows: int,
                    minimum_coverage: float, bootstrap_replicates: int = 0,
                    bootstrap_seed: int = 0) -> dict:
    pstats = _group_stats(prc).rename(columns={
        "rows": "prc_rows", "mean_mass_fraction": "prc_mean"})
    mstats = _group_stats(model).rename(columns={
        "rows": "co2gap_rows", "mean_mass_fraction": "co2gap_mean"})
    cells = mstats.merge(pstats, on=["cell", "aircraft_type", "distance_band_km"],
                         how="inner", validate="one_to_one")
    cells = cells.loc[(cells["prc_rows"] >= minimum_rows) &
                      (cells["co2gap_rows"] >= minimum_rows)].copy()
    cells = cells.sort_values(["aircraft_type", "distance_band_km"]).reset_index(drop=True)
    if cells.empty:
        raise TowValidationError("no common cell passes the registered minimum")
    retained = set(cells["cell"])
    model_kept = model.loc[model["cell"].isin(retained)].copy()
    prc_kept = prc.loc[prc["cell"].isin(retained)].copy()
    release_coverage = len(model_kept) / len(model)
    total_model = float(cells["co2gap_rows"].sum())
    cells["release_weight"] = cells["co2gap_rows"] / total_model
    cells["difference_pp"] = 100 * (cells["co2gap_mean"] - cells["prc_mean"])
    co2gap_mean = float(np.dot(cells["release_weight"], cells["co2gap_mean"]))
    prc_mean = float(np.dot(cells["release_weight"], cells["prc_mean"]))

    weight_by_cell = cells.set_index("cell")["release_weight"]
    count_by_cell = prc_kept.groupby("cell", observed=True).size()
    prc_weights = prc_kept["cell"].map(weight_by_cell) / prc_kept["cell"].map(count_by_cell)
    model_weights = np.full(len(model_kept), 1.0 / len(model_kept))
    result = {
        "status": "complete" if release_coverage >= minimum_coverage else "incomplete",
        "minimum_rows_per_source_per_cell": int(minimum_rows),
        "retained_cells": int(len(cells)),
        "release_rows_in_common_cells": int(len(model_kept)),
        "prc_rows_in_common_cells": int(len(prc_kept)),
        "release_row_coverage": float(release_coverage),
        "minimum_release_row_coverage": float(minimum_coverage),
        "primary": {
            "co2gap_standardised_mean_mass_fraction": co2gap_mean,
            "prc_standardised_mean_mass_fraction": prc_mean,
            "co2gap_minus_prc_pp_mtow": 100 * (co2gap_mean - prc_mean),
            "sign": "positive_means_co2gap_heavier",
        },
        "standardised_distribution": {
            "co2gap": _summary(model_kept["mass_fraction"], model_weights),
            "prc": _summary(prc_kept["mass_fraction"], prc_weights),
        },
        "cell_difference": {
            "release_weighted_mean_absolute_pp": float(
                np.dot(cells["release_weight"], cells["difference_pp"].abs())),
            "release_weighted_root_mean_square_pp": float(np.sqrt(
                np.dot(cells["release_weight"], cells["difference_pp"] ** 2))),
        },
        "cells": [{
            "aircraft_type": str(row.aircraft_type),
            "distance_band_km": str(row.distance_band_km),
            "co2gap_rows": int(row.co2gap_rows),
            "prc_rows": int(row.prc_rows),
            "release_weight": float(row.release_weight),
            "co2gap_mean_mass_fraction": float(row.co2gap_mean),
            "prc_mean_mass_fraction": float(row.prc_mean),
            "co2gap_minus_prc_pp_mtow": float(row.difference_pp),
        } for row in cells.itertuples(index=False)],
    }
    result["by_aircraft_type"] = _aggregate_cells(cells, "aircraft_type")
    result["by_distance_band"] = _aggregate_cells(cells, "distance_band_km")
    if bootstrap_replicates:
        result["day_block_sampling_precision"] = _bootstrap_precision(
            prc_kept, cells, co2gap_mean, bootstrap_replicates, bootstrap_seed)
    return result


def _aggregate_cells(cells: pd.DataFrame, dimension: str) -> list[dict]:
    output = []
    for value, group in cells.groupby(dimension, observed=True, sort=True):
        weights = group["co2gap_rows"] / group["co2gap_rows"].sum()
        output.append({
            dimension: str(value),
            "release_weight_within_reported_cells": float(group["release_weight"].sum()),
            "co2gap_rows": int(group["co2gap_rows"].sum()),
            "prc_rows": int(group["prc_rows"].sum()),
            "co2gap_mean_mass_fraction": float(np.dot(weights, group["co2gap_mean"])),
            "prc_mean_mass_fraction": float(np.dot(weights, group["prc_mean"])),
            "co2gap_minus_prc_pp_mtow": float(
                100 * np.dot(weights, group["co2gap_mean"] - group["prc_mean"])),
        })
    return output


def _bootstrap_precision(prc: pd.DataFrame, cells: pd.DataFrame,
                         co2gap_mean: float, replicates: int, seed: int) -> dict:
    cell_order = list(cells["cell"])
    day_order = sorted(prc["date_utc"].dt.strftime("%Y-%m-%d").unique())
    indexed = prc.assign(day=prc["date_utc"].dt.strftime("%Y-%m-%d"))
    counts = (indexed.pivot_table(index="day", columns="cell", values="mass_fraction",
                                  aggfunc="count", fill_value=0)
              .reindex(index=day_order, columns=cell_order, fill_value=0).to_numpy(float))
    sums = (indexed.pivot_table(index="day", columns="cell", values="mass_fraction",
                                aggfunc="sum", fill_value=0)
            .reindex(index=day_order, columns=cell_order, fill_value=0).to_numpy(float))
    rng = np.random.default_rng(seed)
    draws = rng.multinomial(len(day_order), np.full(len(day_order), 1 / len(day_order)),
                            size=replicates)
    boot_counts = draws @ counts
    if (boot_counts <= 0).any():
        raise TowValidationError("a registered bootstrap replicate loses a retained cell")
    boot_means = (draws @ sums) / boot_counts
    weights = cells["release_weight"].to_numpy(float)
    differences = 100 * (co2gap_mean - boot_means @ weights)
    qs = np.quantile(differences, [0.025, 0.5, 0.975])
    return {
        "method": "PRC UTC day blocks with replacement; fixed release cells and weights",
        "days": int(len(day_order)),
        "replicates": int(replicates),
        "seed": int(seed),
        "standard_error_pp_mtow": float(np.std(differences, ddof=1)),
        "q025_pp_mtow": float(qs[0]),
        "median_pp_mtow": float(qs[1]),
        "q975_pp_mtow": float(qs[2]),
        "interpretation": "sampling precision conditional on PRC selection; not total uncertainty",
    }


def _rounded(value):
    if isinstance(value, dict):
        return {key: _rounded(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_rounded(item) for item in value]
    if isinstance(value, float):
        return round(value, 10)
    return value


def analyse(design: dict, design_meta: dict, prc_frame: pd.DataFrame,
            model_frame: pd.DataFrame, limits: dict) -> dict:
    edges = design_meta["distance_edges"]
    prc, prc_funnel = prepare_prc(prc_frame, limits)
    model, model_funnel = prepare_co2gap(model_frame, limits, design)
    prc, model = add_cells(prc, edges), add_cells(model, edges)
    precision = design["precision"]
    primary = compare_sources(
        prc, model, minimum_rows=design_meta["minimum_rows"],
        minimum_coverage=design_meta["minimum_coverage"],
        bootstrap_replicates=precision["replicates"],
        bootstrap_seed=precision["seed"])

    robustness = {}
    for minimum in (30, 300):
        check = compare_sources(prc, model, minimum_rows=minimum,
                                minimum_coverage=design_meta["minimum_coverage"])
        robustness[f"minimum_{minimum}"] = {
            key: check[key] for key in ("status", "retained_cells",
                                        "release_row_coverage", "primary")}
    tolerant_prc, tolerant_funnel = prepare_prc(prc_frame, limits, mtow_factor=1.02)
    tolerant_prc = add_cells(tolerant_prc, edges)
    tolerant = compare_sources(
        tolerant_prc, model, minimum_rows=design_meta["minimum_rows"],
        minimum_coverage=design_meta["minimum_coverage"])
    robustness["prc_up_to_1_02_mtow"] = {
        "prc_funnel": tolerant_funnel,
        **{key: tolerant[key] for key in ("status", "retained_cells",
                                          "release_row_coverage", "primary")},
    }
    result = {
        "schema_version": 1,
        "kind": "co2gap-takeoff-mass-external-validation-result",
        "status": primary["status"],
        "design_sha256": design_meta["design_sha256"],
        "release_id": design["co2gap_target"]["release_id"],
        "external_dataset_doi": design["external_source"]["doi"],
        "funnel": {"prc": prc_funnel, "co2gap": model_funnel},
        "raw_eligible_distribution": {
            "prc": _summary(prc["mass_fraction"]),
            "co2gap": _summary(model["mass_fraction"]),
        },
        "comparison": primary,
        "robustness": robustness,
        "interpretation": {
            "permitted": "descriptive validation and redesign of combined initial-mass scenarios",
            "not_permitted": [
                "separate inference for load factor, reserve fuel or trip fuel",
                "total or physical uncertainty interval",
                "release headline correction",
                "claim of population representativeness",
            ],
            "noncomparabilities": design["known_noncomparabilities"],
        },
    }
    return _rounded(result)


def markdown_report(result: dict) -> str:
    comparison = result["comparison"]
    primary = comparison["primary"]
    precision = comparison["day_block_sampling_precision"]
    prc = result["funnel"]["prc"]
    model = result["funnel"]["co2gap"]
    lines = [
        "# External take-off-mass validation",
        "",
        f"Status: **{result['status']}**. This is a laboratory diagnostic, not a release correction.",
        "",
        "## Measured result",
        "",
        (f"Across {comparison['retained_cells']:,} common aircraft-type/distance cells "
         f"covering {comparison['release_row_coverage']:.1%} of eligible release flights, "
         f"co2gap minus PRC equals **{primary['co2gap_minus_prc_pp_mtow']:+.3f} "
         "percentage points of MTOW**."),
        "",
        (f"The release-standardised mean mass fraction is "
         f"{primary['co2gap_standardised_mean_mass_fraction']:.4f} for co2gap and "
         f"{primary['prc_standardised_mean_mass_fraction']:.4f} for PRC."),
        "",
        (f"The registered PRC day-block precision diagnostic is "
         f"{precision['q025_pp_mtow']:+.3f} to {precision['q975_pp_mtow']:+.3f} pp "
         f"(SE {precision['standard_error_pp_mtow']:.3f} pp). It is conditional on "
         "the selected airlines and is not a total uncertainty interval."),
        "",
        "## Data funnel",
        "",
        f"* PRC: {prc['eligible_rows']:,} of {prc['input_rows']:,} rows eligible.",
        f"* co2gap: {model['eligible_rows']:,} of {model['input_rows']:,} rows eligible.",
        (f"* Common retained cells: {comparison['prc_rows_in_common_cells']:,} PRC rows "
         f"and {comparison['release_rows_in_common_cells']:,} co2gap rows."),
        "",
        "## Interpretation limit",
        "",
        "TOW observes payload, carried reserve and trip fuel together. The comparison can "
        "test the combined initial-mass assumption but cannot identify those three inputs "
        "separately. The PRC sample covers 2022, only participating airlines with complete "
        "weight information, and 6.1% of EUROCONTROL traffic; post-stratification by type "
        "and distance does not remove operator, route, season or year differences.",
        "",
        "The machine-readable result contains every retained aggregate cell and all "
        "pre-registered robustness checks. No external or co2gap flight row is included.",
        "",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--design", type=Path, default=ROOT / "tow-validation-design.json")
    parser.add_argument("--manifest", type=Path, default=ROOT / "release-manifest.json")
    parser.add_argument("--prc-flight-list", type=Path, required=True)
    parser.add_argument("--flights-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--skip-release-input-verification", action="store_true",
                        help="skip the 14 GB manifest hash pass; recorded in output")
    args = parser.parse_args(argv)

    design = _json(args.design)
    meta = validate_design(design, args.design, args.manifest)
    external = verify_external_file(args.prc_flight_list,
                                    design["external_source"]["file"])
    openap_version = require_openap_version(meta["manifest"])
    release_verified = not args.skip_release_input_verification
    if release_verified:
        meta["manifest"].verify_set("flights", args.flights_dir)
    prc_frame = load_prc(args.prc_flight_list,
                         design["external_source"]["required_columns"])
    model_frame = load_co2gap(args.flights_dir, meta["manifest"].days)
    all_types = pd.concat([prc_frame["aircraft_type"], model_frame["typecode"]],
                          ignore_index=True)
    limits, unsupported = aircraft_limits(all_types)
    result = analyse(design, meta, prc_frame, model_frame, limits)
    result["input_validation"] = {
        "external_file": external,
        "release_flight_input_sha256": design["co2gap_target"]["flight_input_sha256"],
        "release_flight_input_verified": release_verified,
        "openap_version": openap_version,
        "implementation_sha256": sha256_file(Path(__file__)),
        "requirements_lock_sha256": sha256_file(ROOT / "requirements-lab.lock"),
        "aircraft_types_with_properties": len(limits),
        "unsupported_typecodes": unsupported,
    }
    out = require_outside_repository(args.out, may_not_exist=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(_rounded(result), indent=2, sort_keys=True) + "\n")
    if args.markdown_out:
        report_path = require_outside_repository(args.markdown_out, may_not_exist=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(markdown_report(result))
    print(json.dumps({
        "status": result["status"],
        "difference_pp_mtow": result["comparison"]["primary"]["co2gap_minus_prc_pp_mtow"],
        "release_row_coverage": result["comparison"]["release_row_coverage"],
        "retained_cells": result["comparison"]["retained_cells"],
        "out": str(out),
    }, sort_keys=True))
    return 0 if result["status"] == "complete" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TowValidationError as exc:
        print(f"TOW validation error: {exc}", file=sys.stderr)
        raise SystemExit(2)
