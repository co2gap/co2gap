#!/usr/bin/env python3
"""
Phase-1 wind gate: does the wind-aware baseline collapse the per-direction
excess asymmetry?

For every accumulated flight we compute excess% two ways:
  * free  = vs the fase-0 wind-free great-circle nominal
  * wind  = vs the ERA5 wind-aware nominal (excess_wind)

Real CO2 is the IAS-based co2_kg_v0 the Pi already stored. We then aggregate by
unordered route and, within it, by direction, and measure

    dir_spread(route) = | mean_excess(A->B) - mean_excess(B->A) |

on routes with at least MIN_PER_DIR flights in EACH direction. The gate:

    median dir_spread over qualifying routes  <  10  points   (wind-aware)

Usage:
    lab-venv/bin/python lab/gate.py            # all days under data/flights/
    lab-venv/bin/python lab/gate.py 2026-07-13 2026-07-19
"""

from __future__ import annotations

import sys
import statistics as st
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "ingest"))
sys.path.insert(0, str(ROOT))

from excess import build_nominal_flight            # noqa: E402  (fase-0 wind-free)
from excess_wind import ideal_co2_windaware        # noqa: E402
from emissions import estimate_fuel                 # noqa: E402
from wind.era5 import WindField                     # noqa: E402

FLIGHTS_DIR = ROOT / "data/flights"
ERA5_DIR = ROOT / "data/era5"
MIN_PER_DIR = 10
LOAD_FACTOR = 0.82
RESERVE_KG = 2000.0

# cache wind-free ideal per (type, gc rounded) — it has no per-flight dependence
_free_cache: dict = {}


def ideal_co2_free(typecode, gc_km):
    key = (typecode, round(gc_km / 25) * 25)
    if key in _free_cache:
        return _free_cache[key]
    nf = build_nominal_flight(typecode, gc_km)
    val = None
    if nf is not None:
        r = estimate_fuel(nf, load_factor=LOAD_FACTOR, reserve_kg=RESERVE_KG, tas_mode="gs")
        if r.ok and r.co2_kg > 0:
            val = r.co2_kg
    _free_cache[key] = val
    return val


def load_flights(days):
    frames = []
    dirs = sorted(FLIGHTS_DIR.glob("*")) if not days else [FLIGHTS_DIR / d for d in days]
    for d in dirs:
        f = d / "flights.parquet"
        if f.exists():
            frames.append(pq.read_table(f).to_pandas())
    if not frames:
        raise SystemExit(f"no flights.parquet under {FLIGHTS_DIR}")
    df = pd.concat(frames, ignore_index=True)
    return df, [d.name for d in dirs if (d / "flights.parquet").exists()]


def route_key(o, d):
    return tuple(sorted([o, d]))


def dir_spreads(df, col):
    """Return list of (route, nA, nB, dir_spread, mean_excess) on qualifying routes."""
    by_dir = defaultdict(list)   # (o,d) -> list of excess
    for _, r in df.iterrows():
        e = r[col]
        if e is None or not np.isfinite(e):
            continue
        by_dir[(r.origin_icao, r.dest_icao)].append(e)
    out = []
    seen = set()
    for (o, d), exs in by_dir.items():
        rk = route_key(o, d)
        if rk in seen:
            continue
        seen.add(rk)
        a = by_dir.get((rk[0], rk[1]), [])
        b = by_dir.get((rk[1], rk[0]), [])
        if len(a) >= MIN_PER_DIR and len(b) >= MIN_PER_DIR:
            spread = abs(st.mean(a) - st.mean(b))
            out.append((f"{rk[0]}<->{rk[1]}", len(a), len(b),
                        spread, st.mean(a + b)))
    return out


def main():
    days = [a for a in sys.argv[1:]]
    df, loaded = load_flights(days)
    print(f"loaded {len(df)} flights from {len(loaded)} day(s): {', '.join(loaded)}")

    # quality gate for the excess analysis
    q = df[(df.origin_icao.notna()) & (df.dest_icao.notna())
           & (df.flown_ge_09gc) & (df.coverage_frac >= 0.85)
           & (df.gc_km >= 150)].copy()
    print(f"{len(q)} flights pass quality gate (O/D known, flown>=0.9GC, cov>=0.85, GC>=150km)")

    # wind field for the loaded days
    ncs = [ERA5_DIR / f"{d}.nc" for d in loaded if (ERA5_DIR / f"{d}.nc").exists()]
    print(f"ERA5 days available: {len(ncs)}/{len(loaded)}")
    wf = WindField(ncs) if ncs else None

    ex_free, ex_wind = [], []
    for _, r in q.iterrows():
        real = r.co2_kg_v0
        idf = ideal_co2_free(r.typecode, r.gc_km)
        ex_free.append((real - idf) / idf * 100 if idf else np.nan)
        if wf is not None:
            iw = ideal_co2_windaware(r.typecode, r.o_lat, r.o_lon, r.d_lat, r.d_lon,
                                     int(r.dep_ts), r.gc_km, wf,
                                     load_factor=LOAD_FACTOR, reserve_kg=RESERVE_KG)
            ex_wind.append((real - iw["ideal_co2_kg"]) / iw["ideal_co2_kg"] * 100
                           if iw else np.nan)
        else:
            ex_wind.append(np.nan)
    q["excess_free"] = ex_free
    q["excess_wind"] = ex_wind

    for label, col in [("WIND-FREE (fase 0)", "excess_free"), ("WIND-AWARE (fase 1)", "excess_wind")]:
        ds = dir_spreads(q, col)
        print(f"\n### {label}: routes with n>={MIN_PER_DIR}/direction ###")
        if not ds:
            print("  (no route reaches n>=10 per direction yet — need more days)")
            continue
        print(f"{'route':16} {'nA':>3} {'nB':>3} {'dir_spread':>11} {'mean_ex%':>9}")
        for rt, na, nb, spread, meanex in sorted(ds, key=lambda x: -x[3]):
            print(f"{rt:16} {na:>3} {nb:>3} {spread:>11.1f} {meanex:>9.1f}")
        med = st.median([x[3] for x in ds])
        print(f"  --> median dir_spread = {med:.1f} points  (n_routes={len(ds)})")

    print("\nGATE: wind-aware median dir_spread < 10 ?")


if __name__ == "__main__":
    main()
