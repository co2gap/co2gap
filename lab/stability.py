#!/usr/bin/env python3
"""
Phase-2b criterion 2: are the route rankings stable month over month?

If the observatory is measuring a structural property of the network, the routes
that look inefficient in January should still look inefficient in June. If the
ranking reshuffles every month we are measuring weather and noise, and no
"worst routes in Europe" list would be defensible.

Test: Spearman rank correlation of per-route excess between month pairs,
over the routes both months have in common. Criterion: rho > 0.6.

TWO METHOD CHOICES THAT DECIDE WHETHER THIS TEST MEANS ANYTHING
--------------------------------------------------------------
1. We rank on the DISTANCE-ADJUSTED excess, not the raw one. Raw excess
   correlates about -0.65 with sector length, so a raw ranking is largely a
   ranking by shortness — and that distance structure is IDENTICAL in every
   month. Correlating raw rankings would therefore return a high rho that says
   nothing about stability: it would just be re-measuring the same distance
   effect twice. Subtracting each month's own distance-band median removes both
   that confound and any month-wide seasonal shift, leaving exactly what we want
   to test: does a route keep its position RELATIVE to comparable flights.

2. The wind field is built and released ONE MONTH AT A TIME. A year of ERA5 held
   at once is ~4 GB of u/v arrays, which would crowd out pandas on a 16 GB
   laptop; per month it is ~640 MB.

Usage: lab-venv/bin/python lab/stability.py
"""

from __future__ import annotations

import gc
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lab"))
from analysis import (FLIGHTS_DIR, build_windfield, enrich, load_calibration,  # noqa: E402
                      load_flights, quality_gate)

MIN_PER_ROUTE_MONTH = 10      # a route-month needs this many flights to rank
MIN_COMMON_ROUTES = 30        # a month pair needs this many shared routes
RHO_TARGET = 0.6
BANDS = [150, 300, 500, 800, 1200, 2000, 20000]


def months_available() -> list[str]:
    days = sorted(d.name for d in FLIGHTS_DIR.glob("*")
                  if (d / "flights.parquet").exists())
    return sorted({d[:7] for d in days}), days


def route_excess_for_month(month: str, all_days: list[str], calib: dict):
    """Median distance-adjusted excess per route for one month."""
    days = [d for d in all_days if d.startswith(month)]
    if not days:
        return None, 0, 0
    df, loaded = load_flights(days)
    q = quality_gate(df)
    wf = build_windfield(loaded)
    if wf is None:
        print(f"  {month}: NESSUN vento ERA5 -> mese saltato")
        return None, 0, 0
    q = enrich(q, wf, calib)
    q = q[np.isfinite(q.excess_wind)].copy()

    # distance adjustment, using THIS month's own band medians (see docstring)
    q["band"] = pd.cut(q.gc_km, BANDS)
    band_med = q.groupby("band", observed=True).excess_wind.median()
    q["adj"] = q.excess_wind - q.band.map(band_med).astype(float)

    q["route"] = [tuple(sorted([o, d])) for o, d in zip(q.origin_icao, q.dest_icao)]
    g = q.groupby("route").agg(n=("adj", "size"), adj=("adj", "median"),
                               ex=("excess_wind", "median"))
    g = g[g.n >= MIN_PER_ROUTE_MONTH]

    n_flights, n_routes = len(q), len(g)
    del df, q, wf
    gc.collect()
    return g, n_flights, n_routes


def main():
    months, all_days = months_available()
    calib = load_calibration()
    print(f"mesi disponibili: {', '.join(months)}  ({len(all_days)} giorni con parquet)\n")

    per_month = {}
    for m in months:
        g, nf, nr = route_excess_for_month(m, all_days, calib)
        if g is None or nr == 0:
            continue
        per_month[m] = g
        print(f"  {m}: {nf:>7,} voli · {nr:>4} rotte con n>={MIN_PER_ROUTE_MONTH} · "
              f"excess mediano {g.ex.median():+.1f}%")

    if len(per_month) < 2:
        raise SystemExit("servono almeno 2 mesi utilizzabili")

    ms = sorted(per_month)
    print(f"\n### Spearman sull'excess NORMALIZZATO per distanza "
          f"(criterio: rho > {RHO_TARGET}) ###")
    print(f"{'coppia':17} {'rotte comuni':>13} {'rho':>7} {'p':>10}   esito")

    rows, consecutive = [], []
    for i, a in enumerate(ms):
        for b in ms[i + 1:]:
            common = per_month[a].index.intersection(per_month[b].index)
            if len(common) < MIN_COMMON_ROUTES:
                continue
            rho, p = spearmanr(per_month[a].loc[common, "adj"],
                               per_month[b].loc[common, "adj"])
            ok = "PASS" if rho > RHO_TARGET else "sotto soglia"
            rows.append((a, b, len(common), rho, p))
            if ms.index(b) == ms.index(a) + 1:
                consecutive.append(rho)
            print(f"{a}→{b:9} {len(common):>13} {rho:>7.3f} {p:>10.2e}   {ok}")

    allr = [r[3] for r in rows]
    print(f"\n  coppie valutate: {len(rows)}")
    print(f"  rho mediano (tutte le coppie): {np.median(allr):.3f}")
    print(f"  rho minimo:                   {min(allr):.3f}  ({rows[int(np.argmin(allr))][0]}"
          f"→{rows[int(np.argmin(allr))][1]})")
    if consecutive:
        print(f"  rho mediano (mesi consecutivi): {np.median(consecutive):.3f}")

    # The honest headline is the WORST pair: a ranking that survives the most
    # distant month pair is structural, one that only survives adjacent months
    # could just be slowly-drifting weather.
    verdict = "PASSA" if min(allr) > RHO_TARGET else "NON PASSA su tutte le coppie"
    print(f"\nCRITERIO 2 ({RHO_TARGET} su OGNI coppia): {verdict}")


if __name__ == "__main__":
    main()
