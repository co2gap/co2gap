#!/usr/bin/env python3
"""Authoritative loader for release-level decomposition headlines.

This is the only place where the gate-to-gate decomposition is joined to the
ground-fuel artefact and converted into the flight-only quantities used by the
site and reports.  Keeping that correction in a renderer made it possible for
``decompose_report.py`` to print a retired 22.06% while the site printed 12.1%.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

import track_quality  # noqa: E402
from artifact_contract import manifest_allowed_missing  # noqa: E402
from release_manifest import ReleaseManifest, optional_manifest  # noqa: E402

GROUND_DEFS = ["suolo", "a1000t40", "a1000t70", "a1000t100", "a3000t70"]
PHASE_A = ["excess_vert_climb_pct", "excess_vert_cruise_pct",
           "excess_vert_desc_pct"]
PHASE_B = ["excess_vert_dep_pct", "excess_vert_enr_pct",
           "excess_vert_arr_pct"]


@dataclass(frozen=True)
class ReleaseDataset:
    frame: pd.DataFrame
    ground_band: dict[str, float]
    ground_coverage: float


def _read_days(root: Path, days: list[str] | None, role: str) -> pd.DataFrame:
    files = ([root / f"{day}.parquet" for day in days]
             if days is not None else sorted(root.glob("*.parquet")))
    if not files:
        raise SystemExit(f"nessun parquet {role} in {root}")
    missing = [str(path) for path in files if not path.is_file()]
    if missing:
        raise SystemExit(f"{role}: {len(missing)} parquet mancanti ({missing[:3]})")
    try:
        return pd.concat([pq.read_table(path).to_pandas() for path in files],
                         ignore_index=True)
    except Exception as exc:
        raise SystemExit(f"{role}: parquet illeggibile in {root}: {exc}") from exc


def _require_columns(df: pd.DataFrame, columns: set[str], role: str) -> None:
    missing = sorted(columns - set(df.columns))
    if missing:
        raise SystemExit(f"{role}: colonne obbligatorie assenti: {missing}")


def _fuel_share(fuel: np.ndarray, total: np.ndarray) -> np.ndarray:
    """Divide without evaluating the zero-denominator branch."""
    share = np.zeros(len(total), dtype=float)
    np.divide(fuel, total, out=share, where=total > 0)
    return share


def correct_phase_basis(frame: pd.DataFrame) -> pd.DataFrame:
    """Put frozen gate-to-gate phase buckets on the flight-only basis."""
    required = set(PHASE_A + PHASE_B + [
        "ground_pct_dep", "ground_pct_arr", "excess_vertical_pct"])
    _require_columns(frame, required, "phase join")
    out = frame.copy()
    out["excess_vert_dep_pct"] -= out.ground_pct_dep
    out["excess_vert_arr_pct"] -= out.ground_pct_arr
    out["excess_vert_climb_pct"] -= out.ground_pct_dep
    out["excess_vert_desc_pct"] -= out.ground_pct_arr
    for columns, label in ((PHASE_A, "fase"), (PHASE_B, "posizione")):
        complete = out[columns + ["excess_vertical_pct"]].notna().all(axis=1)
        residual = float((out.loc[complete, columns].sum(axis=1)
                          - out.loc[complete, "excess_vertical_pct"]).abs().max())
        if not np.isfinite(residual) or residual > 1e-6:
            raise SystemExit(
                f"lo split per {label} non somma al verticale flight-only "
                f"(residuo massimo {residual:.4g} punti)")
    return out


def _verify_manifest_inputs(
    manifest: ReleaseManifest,
    dec_dir: Path,
    ground_dir: Path,
    calibration: Path,
    ground_def: str,
) -> None:
    manifest.verify_track_quality(track_quality)
    expected = manifest.data["configuration"]["ground"]["definition"]
    if ground_def != expected:
        raise SystemExit(
            f"ADSB_GROUND_DEF={ground_def!r} differs from release "
            f"{manifest.release_id}: {expected!r}")
    for role, root in (("decomposition", dec_dir), ("ground", ground_dir)):
        manifest.require_exact_output_days(root, role)
        manifest.verify_set(role, root, artifact=True)
    manifest.verify_file("calibration", calibration)


def load_release_data(
    dec_dir: Path,
    ground_dir: Path,
    calibration: Path,
    *,
    ground_def: str = "a3000t70",
    bins: list[int] | None = None,
    min_n_cell: int = 200,
    manifest: ReleaseManifest | None = None,
    verify_manifest: bool = True,
) -> ReleaseDataset:
    """Load, validate and ground-correct one decomposition population.

    ``co2_kg_v0`` and all three excess percentages in the returned frame are
    flight-only. ``co2_gate_to_gate_kg`` preserves the input inventory value.
    Absolute calibrated columns are derived only after the ground correction.
    """
    manifest = optional_manifest() if manifest is None else manifest
    if manifest and verify_manifest:
        _verify_manifest_inputs(
            manifest, dec_dir, ground_dir, calibration, ground_def)
    days = manifest.days if manifest else None
    df = _read_days(dec_dir, days, "decomposition")
    g = _read_days(ground_dir, days, "ground")

    _require_columns(df, {
        "day", "flight_id", "typecode", "gc_km", "flown_km", "co2_kg_v0",
        "ideal_gc_co2_kg", "hybrid_co2_kg",
    }, "decomposition")
    ground_columns = {
        "day", "flight_id", "fuel_recomputed_kg",
        f"fuel_{ground_def}_kg", f"fuel_{ground_def}_dep_kg",
        f"fuel_{ground_def}_arr_kg",
    }
    _require_columns(g, ground_columns, "ground")

    if manifest and sorted(df.day.astype(str).unique()) != manifest.days:
        raise SystemExit(
            f"decomposition population differs from release {manifest.release_id}")
    missing_days = sorted(set(df.day.astype(str).unique())
                          - set(g.day.astype(str).unique()))
    if missing_days:
        raise SystemExit(
            f"quota di terra assente per {len(missing_days)} giorni pubblicati "
            f"({missing_days[:3]}): quei voli entrerebbero gate-to-gate")

    dec_keys = set(zip(df.day.astype(str), df.flight_id.astype(int)))
    ground_keys = set(zip(g.day.astype(str), g.flight_id.astype(int)))
    if len(dec_keys) != len(df):
        raise SystemExit("decomposizione con chiavi (day, flight_id) duplicate")
    if len(ground_keys) != len(g):
        raise SystemExit("quota di terra con chiavi (day, flight_id) duplicate")
    allowed = (manifest_allowed_missing(manifest, "ground") & dec_keys
               if manifest else set())
    missing_keys = dec_keys - ground_keys
    if missing_keys != allowed:
        unknown = sorted(missing_keys - allowed)
        stale = sorted(allowed - missing_keys)
        raise SystemExit(
            f"keyset quota di terra non conforme: {len(unknown)} assenti non "
            f"autorizzati, {len(stale)} eccezioni dichiarate ma presenti")

    recomputed = pd.to_numeric(g.fuel_recomputed_kg, errors="coerce").to_numpy()
    if not np.isfinite(recomputed).all() or (recomputed < 0).any():
        raise SystemExit("ground: fuel_recomputed_kg non finito o negativo")
    for suffix in ("", "_dep", "_arr"):
        column = f"fuel_{ground_def}{suffix}_kg"
        fuel = pd.to_numeric(g[column], errors="coerce").to_numpy()
        if (not np.isfinite(fuel).all() or (fuel < 0).any()
                or (fuel > recomputed + 1e-6).any()):
            raise SystemExit(f"ground: valori fisicamente invalidi in {column}")
        g["share_ground" + suffix] = _fuel_share(fuel, recomputed)

    n_before = len(df)
    df = df.merge(g[["day", "flight_id", "share_ground",
                     "share_ground_dep", "share_ground_arr"]],
                  on=["day", "flight_id"], how="left", validate="one_to_one")
    if len(df) != n_before:
        raise SystemExit("il merge terra ha cambiato il numero di voli")
    unresolved = set(zip(df.loc[df.share_ground.isna(), "day"].astype(str),
                         df.loc[df.share_ground.isna(), "flight_id"].astype(int)))
    if unresolved != allowed:
        raise SystemExit("il merge terra ha prodotto valori mancanti inattesi")
    coverage = float(df.share_ground.notna().mean())
    for column in ("share_ground", "share_ground_dep", "share_ground_arr"):
        df[column] = df[column].fillna(0.0)
    split_residual = (df.share_ground_dep + df.share_ground_arr
                      - df.share_ground).abs().max()
    if not np.isfinite(split_residual) or float(split_residual) > 1e-8:
        raise SystemExit(
            "ground: le quote partenza+arrivo non sommano alla quota totale "
            f"(residuo massimo {float(split_residual):.4g})")

    # This test is intentionally one-way: it proves every stored row satisfies
    # the current distance floor. The footer contract is what records which
    # threshold produced a new artefact.
    if float(df.gc_km.min()) < track_quality.GC_MIN_KM:
        raise SystemExit(
            f"i dati contengono tratte da {df.gc_km.min():.0f} km mentre "
            f"track_quality.GC_MIN_KM dice {track_quality.GC_MIN_KM}")

    if not calibration.is_file():
        raise SystemExit(
            f"calibrazione assente: {calibration}. Una headline assoluta non "
            "puo' ripiegare silenziosamente su fattori unitari")
    content = json.loads(calibration.read_text())
    if not isinstance(content, dict):
        raise SystemExit(f"calibrazione non valida: {calibration}")
    factors: dict[str, float] = content.get("factors", content)
    if not isinstance(factors, dict):
        raise SystemExit(f"mappa dei fattori non valida: {calibration}")
    k = df.typecode.map(lambda value: factors.get(value, 1.0)).astype(float).to_numpy()
    if not np.isfinite(k).all() or (k <= 0).any():
        raise SystemExit("calibrazione con fattore non finito o non positivo")

    # Measure the headline's sensitivity before mutating the gate-to-gate CO2.
    ideal_sum = float(df.ideal_gc_co2_kg.sum())
    lateral = ((float(df.hybrid_co2_kg.sum()) - ideal_sum)
               / ideal_sum * 100.0)
    ground_band: dict[str, float] = {}
    ground_measures = g[["day", "flight_id"]].copy()
    for definition in GROUND_DEFS:
        column = f"fuel_{definition}_kg"
        if column in g.columns:
            fuel = pd.to_numeric(g[column], errors="coerce").to_numpy()
            if (np.isfinite(fuel).all() and (fuel >= 0).all()
                    and (fuel <= recomputed + 1e-6).all()):
                ground_measures[definition] = _fuel_share(fuel, recomputed)
    sensitivity = df[["day", "flight_id", "co2_kg_v0", "hybrid_co2_kg"]].merge(
        ground_measures, on=["day", "flight_id"], how="left",
        validate="one_to_one")
    for definition in GROUND_DEFS:
        if definition not in sensitivity.columns:
            continue
        share = sensitivity[definition].fillna(0.0).to_numpy()
        real = sensitivity.co2_kg_v0.to_numpy() * (1 - share)
        ground_band[definition] = (
            lateral + (real.sum() - sensitivity.hybrid_co2_kg.sum())
            / ideal_sum * 100.0)

    df["co2_gate_to_gate_kg"] = df.co2_kg_v0.to_numpy()
    df["co2_kg_v0"] = (df.co2_kg_v0.to_numpy()
                       * (1 - df.share_ground.to_numpy()))
    ideal = df.ideal_gc_co2_kg.to_numpy()
    df["excess_total_pct"] = (df.co2_kg_v0.to_numpy() - ideal) / ideal * 100.0
    df["excess_lateral_pct"] = (
        (df.hybrid_co2_kg.to_numpy() - ideal) / ideal * 100.0)
    df["excess_vertical_pct"] = (
        (df.co2_kg_v0.to_numpy() - df.hybrid_co2_kg.to_numpy())
        / ideal * 100.0)
    gate_to_gate = df.co2_gate_to_gate_kg.to_numpy()
    df["ground_pct_dep"] = (
        gate_to_gate * df.share_ground_dep.to_numpy() / ideal * 100.0)
    df["ground_pct_arr"] = (
        gate_to_gate * df.share_ground_arr.to_numpy() / ideal * 100.0)
    df["co2_ground_kg"] = gate_to_gate * df.share_ground.to_numpy() * k
    df["co2_real_kg"] = df.co2_kg_v0.to_numpy() * k
    df["co2_ideal_kg"] = ideal * k
    df["co2_hybrid_kg"] = df.hybrid_co2_kg.to_numpy() * k
    df["excess_kg"] = df.co2_real_kg - df.co2_ideal_kg

    if bins is not None:
        df["bin"] = pd.cut(df.gc_km, bins).astype(str)
        cell = df["bin"] + "|" + df.typecode
        enough = cell.map(cell.value_counts()) >= min_n_cell
        for source, target in (("excess_total_pct", "d_tot"),
                               ("excess_lateral_pct", "d_lat"),
                               ("excess_vertical_pct", "d_vert")):
            med_bin = df["bin"].map(df.groupby("bin")[source].median()).to_numpy()
            med_cell = cell.map(
                df[enough].groupby(cell[enough])[source].median()).to_numpy()
            reference = np.where(
                enough.to_numpy() & np.isfinite(med_cell), med_cell, med_bin)
            df[target] = df[source].to_numpy() - reference

    print(f"  correzione terra [{ground_def}]: {coverage*100:.1f}% dei voli, "
          f"{df.share_ground.mean()*100:.2f}% del carburante escluso dal gap")
    return ReleaseDataset(df, ground_band, coverage)
