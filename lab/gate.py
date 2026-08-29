#!/usr/bin/env python3
"""
Phase-1 wind gate: does the wind-aware baseline collapse the per-direction
excess asymmetry?

For every accumulated flight we compute excess% two ways (excess is
calibration-invariant, so no calibration is applied here):
  * free  = vs the fase-0 wind-free great-circle nominal
  * wind  = vs the ERA5 wind-aware nominal

We aggregate by unordered route and, within it, by direction, and measure

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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lab"))
from analysis import (load_flights, build_windfield, quality_gate, enrich,  # noqa: E402
                      load_calibration)

MIN_PER_DIR = 10


def route_key(o, d):
    return tuple(sorted([o, d]))


def dir_spreads(df, col):
    """Per-direction MEDIANS (robust to the long per-flight tail: go-arounds,
    extreme holding produce >100% excess outliers that a mean would smear)."""
    by_dir = defaultdict(list)
    det_dir = defaultdict(list)
    for r in df.itertuples(index=False):
        e = getattr(r, col)
        if e is None or not np.isfinite(e):
            continue
        by_dir[(r.origin_icao, r.dest_icao)].append(e)
        det_dir[(r.origin_icao, r.dest_icao)].append(r.detour_pct)
    out, seen = [], set()
    for (o, d) in by_dir:
        rk = route_key(o, d)
        if rk in seen:
            continue
        seen.add(rk)
        a = by_dir.get((rk[0], rk[1]), [])
        b = by_dir.get((rk[1], rk[0]), [])
        if len(a) >= MIN_PER_DIR and len(b) >= MIN_PER_DIR:
            # signed excess asymmetry and signed detour asymmetry (A->B minus B->A):
            # detour is pure geometry (wind cannot change flown distance), so an
            # excess asymmetry that tracks the detour asymmetry is real ATC signal.
            ex_diff = st.median(a) - st.median(b)
            det_diff = (st.median(det_dir[(rk[0], rk[1])])
                        - st.median(det_dir[(rk[1], rk[0])]))
            out.append((f"{rk[0]}<->{rk[1]}", len(a), len(b),
                        abs(ex_diff), st.median(a + b), ex_diff, det_diff))
    return out


def main():
    days = sys.argv[1:]
    df, loaded = load_flights(days)
    print(f"loaded {len(df)} flights from {len(loaded)} day(s): {', '.join(loaded)}")
    q = quality_gate(df)
    print(f"{len(q)} pass quality gate (O/D known, flown>=0.9GC, cov>=0.85, GC>=150km)")
    wf = build_windfield(loaded)
    print(f"wind field: {'built' if wf else 'MISSING (no ERA5)'}")
    q = enrich(q, wf, load_calibration())

    results = {}
    for label, col in [("WIND-FREE (fase 0)", "excess_free"),
                       ("WIND-AWARE (fase 1)", "excess_wind")]:
        ds = dir_spreads(q, col)
        results[col] = ds
        print(f"\n### {label}: routes with n>={MIN_PER_DIR}/direction ###")
        if not ds:
            print("  (no route reaches n>=10 per direction yet — need more days)")
            continue
        print(f"{'route':16} {'nA':>3} {'nB':>3} {'dir_spread':>11} {'med_ex%':>8} {'det_diff':>8}")
        for rt, na, nb, spread, medex, _exd, detd in sorted(ds, key=lambda x: -x[3]):
            print(f"{rt:16} {na:>3} {nb:>3} {spread:>11.1f} {medex:>8.1f} {detd:>+8.1f}")
        med = st.median([x[3] for x in ds])
        print(f"  --> median dir_spread = {med:.1f} points  (n_routes={len(ds)})")

    # ATC-vs-wind attribution: does the residual excess asymmetry track the
    # (wind-free, geometric) detour asymmetry?
    ds = results.get("excess_wind") or []
    if len(ds) >= 5:
        exd = np.array([x[5] for x in ds])
        detd = np.array([x[6] for x in ds])
        r = float(np.corrcoef(exd, detd)[0, 1])
        same_sign = float(np.mean(np.sign(exd) == np.sign(detd)))
        print(f"\n### residual-asymmetry attribution (wind-aware) ###")
        print(f"  corr(excess_diff, detour_diff) = {r:+.2f}   "
              f"same-sign fraction = {same_sign*100:.0f}%")
        print(f"  (high corr => residual asymmetry is real ATC routing signal, "
              f"not unmodelled wind)")

    if results.get("excess_wind"):
        med = st.median([x[3] for x in results["excess_wind"]])
        print(f"\nGATE: wind-aware median dir_spread = {med:.1f}  "
              f"-> {'PASS (<10)' if med < 10 else 'NOT YET (<10 required)'}")


if __name__ == "__main__":
    main()
