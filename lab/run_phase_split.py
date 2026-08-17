#!/usr/bin/env python3
"""
Fase 3: split the vertical excess by phase of flight and by position.

Reads the FROZEN decomposition (one row per flight, already holding the ideal,
the hybrid, the cruise altitude and the along-track wind) plus the stored
trajectory, and writes one extra parquet per day with the phase columns. It
never rewrites the decomposition it reads.

Because the frozen row already carries everything the nominal needs, this step
does NOT touch ERA5 and does NOT rebuild the great-circle baseline. That the
rebuilt hybrid reproduces the frozen `hybrid_co2_kg` exactly is checked on every
flight and reported per day: it is the gate proving this is a re-split of the
same flight rather than a differently-parameterised one.

    ADSB_ROOT=$PWD \
    ADSB_FLIGHTS_DIR=$PWD/data/flights_ecac \
    ADSB_DECOMP_DIR=$PWD/data/decomposition_ecac \
    ADSB_PHASE_DIR=$PWD/data/decomposition_ecac_phase \
    ../lab-venv/bin/python lab/run_phase_split.py

Every path comes from the environment, and the output directory has its OWN
variable rather than sharing ADSB_DECOMP_DIR. In this project a hardcoded path
has silently run a computation against the wrong dataset seven times; reusing
one variable for both the input and the output would be the eighth way.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
for _p in ("pipeline", "ingest", "lab"):
    sys.path.insert(0, str(ROOT / _p))
sys.path.insert(0, str(ROOT))

from analysis import LOAD_FACTOR, RESERVE_KG          # noqa: E402
from phase_split import phase_split_flight            # noqa: E402

FLIGHTS_DIR = Path(os.environ.get("ADSB_FLIGHTS_DIR") or (ROOT / "data/flights"))
DEC_DIR = Path(os.environ.get("ADSB_DECOMP_DIR") or (ROOT / "data/decomposition"))
OUT_DIR = Path(os.environ.get("ADSB_PHASE_DIR")
               or (ROOT / "data/decomposition_phase"))

OUT_COLS = ["day", "flight_id",
            "excess_vert_climb_pct", "excess_vert_cruise_pct",
            "excess_vert_desc_pct",
            "excess_vert_dep_pct", "excess_vert_enr_pct", "excess_vert_arr_pct",
            "nom_climb_frac", "nom_desc_frac",
            "tma_dep_frac", "tma_arr_frac",
            "real_toc_frac", "real_tod_frac",
            "real_co2_thin_kg", "hybrid_co2_rebuilt_kg", "resid_add_pct"]

# The row is only useful if the flight it describes is in the frozen file, so
# these are the frozen columns the split consumes.
NEED = ["day", "flight_id", "typecode", "co2_kg_v0", "ideal_gc_co2_kg",
        "hybrid_co2_kg", "flown_km", "cruise_alt_ft", "mean_wpar_track_ms"]

PTS_COLS = ["flight_id", "t", "lat", "lon", "alt_ft", "gs_kt", "ias_kt", "vs_fpm"]


def ready_days() -> list[str]:
    """Days that have BOTH a readable frozen decomposition and a stored track.

    Readable, not merely present: a footer-less parquet from a run killed
    mid-write reads as a file and would be taken for a finished day.
    """
    out = []
    for f in sorted(DEC_DIR.glob("*.parquet")):
        day = f.stem
        if not (FLIGHTS_DIR / day / "points.parquet").exists():
            continue
        try:
            if pq.read_metadata(f).num_rows > 0:
                out.append(day)
        except Exception:
            print(f"  {day}: frozen parquet unreadable, skipped", flush=True)
    return out


def process_day(day: str) -> tuple[int, dict]:
    dec = pq.read_table(DEC_DIR / f"{day}.parquet", columns=NEED).to_pandas()
    if dec.empty:
        return 0, {}

    keep = set(dec.flight_id.tolist())
    pts = pq.read_table(FLIGHTS_DIR / day / "points.parquet",
                        columns=PTS_COLS).to_pandas()
    pts = pts[pts.flight_id.isin(keep)]
    grouped = {fid: (g.t.to_numpy(np.float64), g.lat.to_numpy(np.float64),
                     g.lon.to_numpy(np.float64), g.alt_ft.to_numpy(np.float64),
                     g.gs_kt.to_numpy(np.float64), g.ias_kt.to_numpy(np.float64),
                     g.vs_fpm.to_numpy(np.float64))
               for fid, g in pts.groupby("flight_id", sort=False)}
    del pts

    rows = []
    for r in dec.itertuples(index=False):
        tr = grouped.get(r.flight_id)
        if tr is None or len(tr[0]) < 3:
            continue
        t, lat, lon, alt_ft, gs_kt, ias_kt, vs_fpm = tr
        d = phase_split_flight(
            r.typecode, float(r.co2_kg_v0), float(r.ideal_gc_co2_kg),
            float(r.hybrid_co2_kg), float(r.flown_km), float(r.cruise_alt_ft),
            float(r.mean_wpar_track_ms),
            t, lat, lon, alt_ft, gs_kt, ias_kt, vs_fpm,
            load_factor=LOAD_FACTOR, reserve_kg=RESERVE_KG)
        if d is None:
            continue
        rows.append({"day": day, "flight_id": int(r.flight_id), **d})

    if not rows:
        return 0, {}
    df = pd.DataFrame(rows)[OUT_COLS]

    # ---- the two gates, checked on every day, never assumed ---------------
    j = df.merge(dec[["flight_id", "hybrid_co2_kg"]], on="flight_id", how="left")
    rel = (j.hybrid_co2_rebuilt_kg - j.hybrid_co2_kg).abs() / j.hybrid_co2_kg
    checks = {
        "n": len(df),
        "hybrid_max_rel_err": float(rel.max()),
        "hybrid_exact_frac": float((rel == 0).mean()),
        "add_max_abs_resid": float(df.resid_add_pct.abs().max()),
        "thin_bias_pct": float(
            (df.real_co2_thin_kg.sum()
             / dec.set_index("flight_id").loc[df.flight_id].co2_kg_v0.sum() - 1)
            * 100.0),
        "tma_nan_frac": float(df.excess_vert_dep_pct.isna().mean()),
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OUT_DIR / f".{day}.parquet.tmp"
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), tmp)
    tmp.replace(OUT_DIR / f"{day}.parquet")
    return len(df), checks


def output_is_valid(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        return pq.read_metadata(path).num_rows > 0
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", nargs="*", default=None)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if DEC_DIR.resolve() == OUT_DIR.resolve():
        raise SystemExit("refusing to write into the decomposition being read: "
                         f"{DEC_DIR}")
    print(f"flights : {FLIGHTS_DIR}\nfrozen  : {DEC_DIR}\noutput  : {OUT_DIR}\n")

    days = args.days if args.days else ready_days()
    todo = [d for d in days
            if args.force or not output_is_valid(OUT_DIR / f"{d}.parquet")]
    print(f"{len(days)} day(s) ready, {len(todo)} to process")

    t0 = time.time()
    total = 0
    worst_hybrid = worst_add = 0.0
    for i, day in enumerate(todo, 1):
        t = time.time()
        try:
            n, c = process_day(day)
        except Exception as e:
            print(f"  {day}  FAILED: {e.__class__.__name__}: {e}", flush=True)
            continue
        total += n
        if c:
            worst_hybrid = max(worst_hybrid, c["hybrid_max_rel_err"])
            worst_add = max(worst_add, c["add_max_abs_resid"])
        el = time.time() - t0
        eta = (len(todo) - i) * el / i
        print(f"  {day}  {n:5d} flights  {time.time()-t:5.1f}s  "
              f"hybrid_err {c.get('hybrid_max_rel_err', float('nan')):.1e}  "
              f"add_resid {c.get('add_max_abs_resid', float('nan')):.1e}  "
              f"thin {c.get('thin_bias_pct', float('nan')):+.3f}%  "
              f"[{i}/{len(todo)}] ETA {eta/60:.1f} min", flush=True)

    print(f"\ndone: {total:,} flights across {len(todo)} day(s) in "
          f"{(time.time()-t0)/60:.1f} min -> {OUT_DIR}")
    print(f"worst hybrid reproduction error : {worst_hybrid:.3e}  "
          f"(gate: must be 0)")
    print(f"worst additivity residual (pp)  : {worst_add:.3e}  "
          f"(gate: must be ~0)")


if __name__ == "__main__":
    main()
