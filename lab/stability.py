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

3. THE WINDOW IS NOT IMPLICIT. FLIGHTS_DIR keeps accumulating every night, so a
   run without --days-from measures whatever happens to be on disk that
   evening, not the release. The published figures were produced before this
   argument existed and are reproducible only by coincidence of timing:
   re-running on 2026-09-07 read 201 days against the release's 197, and the
   two weakest month pairs in the published table both involve the month whose
   day count differed. Point --days-from at the frozen decomposition, the way
   lab/ground_share.py and lab/airport_stability.py already are, and the run
   measures the release. Without it the run says so, loudly, and continues.

Usage:
    lab-venv/bin/python lab/stability.py --days-from data/decomposition_ecac
"""

from __future__ import annotations

import argparse
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


def months_available(days_from: Path | None = None,
                     from_day: str | None = None,
                     to_day: str | None = None):
    """Months and days to measure, and the window is explicit or announced.

    --days-from takes the exact day set from a frozen artefact, the same rule
    lab/ground_share.py applies with the same argument name. Without it the
    function returns every day on disk and the caller warns, because
    FLIGHTS_DIR is four days ahead of the release the day this was written and
    will be further ahead tomorrow.
    """
    days = sorted(d.name for d in FLIGHTS_DIR.glob("*")
                  if (d / "flights.parquet").exists())
    pinned_by = None
    if days_from is not None:
        frozen = sorted(f.stem for f in Path(days_from).glob("*.parquet"))
        if not frozen:
            raise SystemExit(f"--days-from {days_from}: nessun parquet, "
                             "finestra non determinabile")
        missing = [d for d in frozen if d not in days]
        if missing:
            raise SystemExit(
                f"--days-from {days_from}: {len(missing)} giorni dell'artefatto "
                f"congelato non hanno un flights.parquet ({missing[:3]}...); "
                "la finestra non e' riproducibile da questo FLIGHTS_DIR")
        days, pinned_by = frozen, str(days_from)
    if from_day:
        days = [d for d in days if d >= from_day]
        pinned_by = pinned_by or "--from-day/--to-day"
    if to_day:
        days = [d for d in days if d <= to_day]
        pinned_by = pinned_by or "--from-day/--to-day"
    return sorted({d[:7] for d in days}), days, pinned_by


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
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--days-from", type=Path, default=None,
                    help="prendi l'insieme esatto dei giorni da questo artefatto "
                         "congelato (es. data/decomposition_ecac)")
    ap.add_argument("--from-day", default=None, help="primo giorno incluso, YYYY-MM-DD")
    ap.add_argument("--to-day", default=None, help="ultimo giorno incluso, YYYY-MM-DD")
    args = ap.parse_args()

    months, all_days, pinned_by = months_available(args.days_from, args.from_day,
                                                   args.to_day)
    calib = load_calibration()
    print(f"mesi disponibili: {', '.join(months)}  ({len(all_days)} giorni con parquet)")
    print(f"finestra: {all_days[0]} -> {all_days[-1]}")
    if pinned_by:
        print(f"finestra FISSATA da {pinned_by}\n")
    else:
        # Non fatale, ma non silenzioso: e' esattamente il difetto che l'audit
        # metodologico ha trovato, e un numero prodotto cosi' non e'
        # riproducibile se non per coincidenza di tempistica.
        print("*** ATTENZIONE: finestra NON fissata. Questa corsa misura i "
              f"{len(all_days)} giorni presenti in {FLIGHTS_DIR} stasera, non "
              "una release.\n*** Per un numero riproducibile: "
              "--days-from data/decomposition_ecac\n")

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
