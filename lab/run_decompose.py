#!/usr/bin/env python3
"""
Fase 2a: run the lateral/vertical excess decomposition over accumulated days.

Designed so the fase-2b rerun over the whole year is ONE COMMAND with no
edits: it processes whatever days have BOTH a flights.parquet and an ERA5
file, one day at a time (points.parquet is ~44 MB/day and never all loaded
at once), and appends to a single tidy output parquet. Already-processed
days are skipped unless --force, so it can be re-run as the ERA5 backfill
and the Pi backfill fill in more days.

    lab-venv/bin/python lab/run_decompose.py                 # all ready days
    lab-venv/bin/python lab/run_decompose.py --days 2026-07-13 2026-07-19
    lab-venv/bin/python lab/run_decompose.py --force         # recompute all

Output: data/decomposition/<day>.parquet, one row per quality-gated flight.
Aggregation and reporting live in lab/decompose_report.py.
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
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "lab"))
sys.path.insert(0, str(ROOT))

from analysis import quality_gate, LOAD_FACTOR, RESERVE_KG   # noqa: E402
from decompose import decompose_flight                        # noqa: E402
from wind.era5 import WindField                               # noqa: E402

# All three follow the environment, because the box is not a property of the
# code: running the ECAC box against the default paths would read the SMALLER
# box's flights and the SMALLER box's wind, and write into the same output
# directory as a previous run — a heavy computation on the wrong dataset with no
# error anywhere. Same failure mode already found in the daily pipeline and in
# the ERA5 cache; guarded the same way.
FLIGHTS_DIR = Path(os.environ.get("ADSB_FLIGHTS_DIR") or (ROOT / "data/flights"))
ERA5_DIR = Path(os.environ.get("ERA5_DIR") or (ROOT / "data/era5"))
OUT_DIR = Path(os.environ.get("ADSB_DECOMP_DIR") or (ROOT / "data/decomposition"))

OUT_COLS = ["day", "flight_id", "typecode", "origin_icao", "dest_icao",
            "gc_km", "flown_km", "dist_ratio", "dist_ratio_enroute",
            "flown_enroute_km", "dep_ts",
            "excess_total_pct", "excess_lateral_pct", "excess_vertical_pct",
            "ideal_gc_co2_kg", "hybrid_co2_kg", "co2_kg_v0",
            "mean_wpar_gc_ms", "mean_wpar_track_ms", "cruise_alt_ft"]


def era5_is_complete(path: Path) -> bool:
    """
    A day counts as usable only if its ERA5 file is COMPLETE, not merely
    present.

    This is the same trap the Pi backfill already learned about parquet
    ("valid footer, >0 rows" rather than "the file exists"), and it bites
    harder here. ERA5T trails real time by ~5 days, and at that boundary CDS
    happily returns a PARTIAL day — 2026-07-20 came back with 15 of 24 hours.
    Nothing errors: WindField would build fine and RegularGridInterpolator,
    which extrapolates by design, would silently invent wind for every flight
    departing after the last available hour. Wrong numbers with no warning
    are worse than a missing day, so we require all 11 pressure levels and
    at least 20 of the 24 hourly steps, and skip the day otherwise.
    """
    try:
        import xarray as xr
        ds = xr.open_dataset(str(path))
        ok = (ds.sizes.get("valid_time", 0) >= 20
              and ds.sizes.get("pressure_level", 0) == 11
              and "u" in ds.variables and "v" in ds.variables)
        ds.close()
        return ok
    except Exception:
        return False


def ready_days() -> list[str]:
    days, partial = [], []
    for d in sorted(FLIGHTS_DIR.glob("*")):
        nc = ERA5_DIR / f"{d.name}.nc"
        if not ((d / "flights.parquet").exists()
                and (d / "points.parquet").exists() and nc.exists()):
            continue
        if era5_is_complete(nc):
            days.append(d.name)
        else:
            partial.append(d.name)
    if partial:
        print(f"skipping {len(partial)} day(s) with INCOMPLETE ERA5 "
              f"(re-run the ERA5 backfill once the data is published): "
              f"{', '.join(partial)}")
    return days


def process_day(day: str) -> int:
    fl = pq.read_table(FLIGHTS_DIR / day / "flights.parquet").to_pandas()
    q = quality_gate(fl)
    if q.empty:
        return 0
    wf = WindField([ERA5_DIR / f"{day}.nc"])

    keep = set(q.flight_id.tolist())
    pts = pq.read_table(FLIGHTS_DIR / day / "points.parquet",
                        columns=["flight_id", "lat", "lon"]).to_pandas()
    pts = pts[pts.flight_id.isin(keep)]
    # group once; the points table is already ordered by flight_id + time
    grouped = {fid: (g.lat.to_numpy(np.float64), g.lon.to_numpy(np.float64))
               for fid, g in pts.groupby("flight_id", sort=False)}
    del pts

    rows = []
    for r in q.itertuples(index=False):
        track = grouped.get(r.flight_id)
        if track is None or len(track[0]) < 3:
            continue
        lat, lon = track
        d = decompose_flight(r.typecode, float(r.co2_kg_v0), float(r.gc_km),
                             float(r.flown_km), lat, lon, int(r.dep_ts), wf,
                             load_factor=LOAD_FACTOR, reserve_kg=RESERVE_KG)
        if d is None:
            continue
        rows.append({
            "day": day, "flight_id": int(r.flight_id), "typecode": r.typecode,
            "origin_icao": r.origin_icao, "dest_icao": r.dest_icao,
            "gc_km": float(r.gc_km), "flown_km": float(r.flown_km),
            "dep_ts": int(r.dep_ts), "co2_kg_v0": float(r.co2_kg_v0),
            **{k: d[k] for k in (
                "dist_ratio", "dist_ratio_enroute", "flown_enroute_km",
                "excess_total_pct", "excess_lateral_pct",
                "excess_vertical_pct", "ideal_gc_co2_kg", "hybrid_co2_kg",
                "mean_wpar_gc_ms", "mean_wpar_track_ms", "cruise_alt_ft")},
        })

    if not rows:
        return 0
    df = pd.DataFrame(rows)[OUT_COLS]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False),
                   OUT_DIR / f"{day}.parquet")
    return len(df)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", nargs="*", default=None)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    days = args.days if args.days else ready_days()
    todo = [d for d in days
            if args.force or not (OUT_DIR / f"{d}.parquet").exists()]
    print(f"{len(days)} day(s) ready, {len(todo)} to process")

    t0 = time.time()
    total = 0
    for i, day in enumerate(todo, 1):
        t = time.time()
        try:
            n = process_day(day)
        except Exception as e:
            print(f"  {day}  FAILED: {e.__class__.__name__}: {e}", flush=True)
            continue
        total += n
        el = time.time() - t0
        eta = (len(todo) - i) * el / i
        print(f"  {day}  {n:5d} flights  {time.time()-t:5.1f}s   "
              f"[{i}/{len(todo)}]  ETA {eta/60:.1f} min", flush=True)

    print(f"\ndone: {total:,} flights across {len(todo)} day(s) in "
          f"{(time.time()-t0)/60:.1f} min -> {OUT_DIR}")


if __name__ == "__main__":
    main()
