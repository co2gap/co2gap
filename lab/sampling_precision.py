"""Conditional sampling precision for paired, stratified ratio contrasts.

No model-error distribution or confidence interval is produced. Flight values
are retained only in memory until centring; the optional PRIVATE audit contains
within-cell moments, never flight keys. Public output contains overall results.
"""
from __future__ import annotations

from collections import defaultdict
import math

import numpy as np


METRICS = ("gap_total_pct", "gap_lateral_pct", "gap_vertical_pct")
FROZEN = "frozen-reference"


class SamplingPrecisionError(ValueError):
    """The sample cannot support the declared sampling variance calculation."""


def _integer(value, label):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)) or value < 1:
        raise SamplingPrecisionError(f"{label} must be a positive integer")
    return int(value)


def ratio_points_gradient(totals):
    """Percent gaps and their 3x3 Jacobian w.r.t. uncalibrated R, I, H totals."""
    t = np.asarray(totals, dtype=float)
    if t.shape != (3,) or not np.isfinite(t).all() or (t <= 0).any():
        raise SamplingPrecisionError("ratio totals must be three finite positive values")
    real, ideal, hybrid = t
    points = 100.0 * np.array([real - ideal, hybrid - ideal, real - hybrid]) / ideal
    gradient = np.array([[100.0 / ideal, -100.0 * real / ideal**2, 0.0],
                         [0.0, -100.0 * hybrid / ideal**2, 100.0 / ideal],
                         [0.0, 0.0, 0.0]])
    gradient[2] = gradient[0] - gradient[1]
    if not np.isfinite(points).all() or not np.isfinite(gradient).all():
        raise SamplingPrecisionError("ratio linearisation overflow")
    return points, gradient


class PairedSamplingPrecision:
    def __init__(self, sample, scenario_ids, nominal):
        self.ids = tuple(scenario_ids)
        if (not self.ids or len(set(self.ids)) != len(self.ids)
                or FROZEN in self.ids or nominal not in self.ids):
            raise SamplingPrecisionError("invalid precision scenario ids/nominal")
        self.nominal = nominal
        self.all_ids = (*self.ids, FROZEN)
        self.cells = {}
        self.keys = {}
        rows = sample.get("rows", [])
        if not rows or _integer(sample.get("sample_rows"), "sample_rows") != len(rows):
            raise SamplingPrecisionError("sample row count does not close")
        for row in rows:
            name = row.get("stratum")
            if not isinstance(name, str) or not name:
                raise SamplingPrecisionError("missing stratum")
            n = _integer(row.get("sample_n"), "sample_n")
            population = _integer(row.get("population_n"), "population_n")
            if n > population or (n == 1 and population > 1):
                raise SamplingPrecisionError("non-census singleton or overfull stratum")
            weight = row.get("weight")
            if (isinstance(weight, bool) or not isinstance(weight, (int, float))
                    or not math.isfinite(weight) or weight != population / n):
                raise SamplingPrecisionError("weight differs from N_h/n_h")
            day = row.get("day")
            fid = row.get("flight_id")
            if (not isinstance(day, str) or not day or isinstance(fid, bool)
                    or not isinstance(fid, int) or fid < 0):
                raise SamplingPrecisionError("invalid flight key")
            key = (day, fid)
            if key in self.keys:
                raise SamplingPrecisionError("duplicate precision flight key")
            self.keys[key] = name
            cell = self.cells.setdefault(name, {"N": population, "n": n, "keys": []})
            if (cell["N"], cell["n"]) != (population, n):
                raise SamplingPrecisionError("inconsistent stratum counts")
            cell["keys"].append(key)
        if any(len(cell["keys"]) != cell["n"] for cell in self.cells.values()):
            raise SamplingPrecisionError("stratum sample counts do not close")
        if (_integer(sample.get("strata"), "strata") != len(self.cells)
                or _integer(sample.get("population_rows"), "population_rows")
                != sum(c["N"] for c in self.cells.values())):
            raise SamplingPrecisionError("strata/population totals do not close")
        self.population = sample["population_rows"]
        self.values = {}

    def add(self, key, scenarios, frozen):
        if key not in self.keys or key in self.values:
            raise SamplingPrecisionError("unknown or repeated precision flight")
        if set(scenarios) != set(self.ids):
            raise SamplingPrecisionError("precision requires every paired scenario")
        vector = np.asarray([*([*scenarios[sid]] for sid in self.ids), frozen], dtype=float)
        if (vector.shape != (len(self.all_ids), 3) or not np.isfinite(vector).all()
                or (vector <= 0).any()):
            raise SamplingPrecisionError("precision flight values must be finite positive R/I/H triples")
        self.values[key] = vector

    def finish(self, reference_totals):
        if set(self.values) != set(self.keys):
            raise SamplingPrecisionError("incomplete paired population: precision is unavailable")
        if set(reference_totals) != set(self.all_ids):
            raise SamplingPrecisionError("missing reference totals")
        totals = np.asarray([reference_totals[sid] for sid in self.all_ids], dtype=float)
        points, gradients = zip(*(ratio_points_gradient(t) for t in totals))
        points, gradients = np.asarray(points), np.asarray(gradients)
        nominal_index = self.all_ids.index(self.nominal)
        frozen_index = self.all_ids.index(FROZEN)
        level_cov = defaultdict(list)
        contrast_cov = defaultdict(list)
        reference_cov = []
        audit_cells = []
        expanded = []
        for name, cell in sorted(self.cells.items()):
            values = np.array([self.values[key] for key in cell["keys"]])
            # Centre before projecting and subtract paired influence values
            # before squaring: do not subtract two large estimated variances.
            mean = values[0] + np.mean(values - values[0], axis=0)
            centred = values - mean
            centred -= np.mean(centred, axis=0)
            influence = np.einsum("nsi,sji->nsj", centred, gradients)
            n, population = cell["n"], cell["N"]
            coefficient = 0.0 if n == population else population * (population - n) / (n * (n - 1))

            def covariance(projected):
                residual = projected - np.mean(projected, axis=0)
                return coefficient * (residual.T @ residual)

            for index, sid in enumerate(self.ids):
                level_cov[sid].append(covariance(influence[:, index]))
                contrast_cov[sid].append(covariance(
                    influence[:, index] - influence[:, nominal_index]))
            level_cov[FROZEN].append(covariance(influence[:, frozen_index]))
            reference_cov.append(covariance(
                influence[:, nominal_index] - influence[:, frozen_index]))
            flat = centred.reshape(n, -1)
            audit_cells.append({
                "stratum": name, "population_n": population, "sample_n": n,
                "mean_kg": mean.reshape(-1).tolist(),
                "centred_cross_products_kg2": (flat.T @ flat).tolist(),
            })
            expanded.append(population * mean)
        reconstructed = np.array([
            [math.fsum(e[s, j] for e in expanded) for j in range(3)]
            for s in range(len(self.all_ids))])
        if not np.allclose(reconstructed, totals, rtol=1e-12, atol=1e-6):
            raise SamplingPrecisionError("precision moments do not reconstruct runner totals")

        def summarise(point, contributions):
            covariance = np.array([
                [math.fsum(c[i, j] for c in contributions) for j in range(3)]
                for i in range(3)])
            if not np.isfinite(covariance).all() or (np.diag(covariance) < 0).any():
                raise SamplingPrecisionError("invalid sampling covariance")
            return {
                "metrics": {
                    metric: {
                        "estimate": float(point[i]),
                        "standard_error_pp": math.sqrt(covariance[i, i]),
                        "relative_standard_error": (
                            math.sqrt(covariance[i, i]) / abs(point[i]) if point[i] != 0 else None),
                        "largest_stratum_variance_fraction": (
                            max(c[i, i] for c in contributions) / covariance[i, i]
                            if covariance[i, i] > 0 else None),
                    } for i, metric in enumerate(METRICS)
                },
                "covariance_pp2": covariance.tolist(),
            }

        result = {
            "schema_version": 1,
            "status": "complete_conditional_sampling_precision",
            "method": "first-order Taylor linearisation; stratified SRS without replacement",
            "metric_order": list(METRICS),
            "level_estimate_unit": "percent", "contrast_estimate_unit": "percentage points",
            "sample_rows": len(self.keys), "population_rows": self.population,
            "strata": len(self.cells),
            "census_strata": sum(c["n"] == c["N"] for c in self.cells.values()),
            "two_observation_noncensus_strata": sum(c["n"] == 2 < c["N"] for c in self.cells.values()),
            "max_total_reconstruction_relative_error": float(np.max(abs(reconstructed / totals - 1))),
            "by_scenario": {
                sid: {
                    "level": summarise(points[index], level_cov[sid]),
                    "delta_from_nominal": summarise(
                        points[index] - points[nominal_index], contrast_cov[sid]),
                } for index, sid in enumerate(self.ids)
            },
            "frozen_same_sample": summarise(points[frozen_index], level_cov[FROZEN]),
            "nominal_minus_frozen": summarise(
                points[nominal_index] - points[frozen_index], reference_cov),
            "limitations": [
                "Conditional on this finite observed population and fixed model/scenarios; not physical CO2 uncertainty.",
                "First-order approximation for ratios; no interval coverage or ratio bias correction is claimed.",
                "Small within-cell samples can give unstable estimated variances; zero estimated SE is not proof of accuracy.",
                "No parameter-error, missing-flight, receiver-selection or temporal-superpopulation uncertainty is included.",
                "No confidence interval, p-value or pass/fail threshold is produced.",
            ],
        }
        audit = {
            "schema_version": 1, "kind": "co2gap-private-sampling-moments",
            "privacy": "PRIVATE: small-cell moments may disclose individual outcomes. Do not publish.",
            "columns": [[sid, name] for sid in self.all_ids for name in ("real", "ideal", "hybrid")],
            "runner_totals_kg": totals.reshape(-1).tolist(),
            "cells": audit_cells,
        }
        self.values.clear()
        return result, audit
