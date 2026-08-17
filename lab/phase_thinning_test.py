#!/usr/bin/env python3
"""
Fase 3, validation: how much of the phase split is an artefact of the fact that
the stored track is THINNED?

`co2_kg_v0` was integrated over the NATIVE trace; what survives on disk is a
subset at >= 10 s spacing. The phase split therefore takes only the SHARES from
the stored track and the LEVEL from the frozen figure, which makes the parts sum
to the published total exactly — but it assumes the thinning bias is spread
across the phases in proportion to their burn. That assumption is the thing to
test, because the bias is not uniform: it lives where fuel flow changes fastest,
which is climb and descent.

There is no native track left to compare against (the raw dumps are rotated
after two days), so the test goes the other way: THIN FURTHER, to 20 s and 40 s,
measure how each phase's share moves as the spacing coarsens, and extrapolate
the trend back to zero spacing. If a phase's share barely moves between 10 s and
40 s, the step from the native trace to 10 s cannot have moved it much either.
If it moves a lot, the split needs a declared uncertainty band rather than a
bare figure.

    ADSB_ROOT=$PWD ADSB_FLIGHTS_DIR=$PWD/data/flights_ecac \
    ADSB_DECOMP_DIR=$PWD/data/decomposition_ecac \
    ../lab-venv/bin/python lab/phase_thinning_test.py [day] [n_flights]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
for _p in ("pipeline", "ingest", "lab"):
    sys.path.insert(0, str(ROOT / _p))
sys.path.insert(0, str(ROOT))

from analysis import LOAD_FACTOR, RESERVE_KG                      # noqa: E402
from emissions import estimate_fuel                               # noqa: E402
from excess_wind import _build_profile                            # noqa: E402
from phase_split import (_arc_profile, _burn_between,             # noqa: E402
                         _nominal_phase_bounds, _flight_from_arrays)

FLIGHTS_DIR = Path(os.environ.get("ADSB_FLIGHTS_DIR") or (ROOT / "data/flights"))
DEC_DIR = Path(os.environ.get("ADSB_DECOMP_DIR") or (ROOT / "data/decomposition"))

SPACINGS = [0, 20, 40]      # 0 = the stored track as-is (already >= 10 s)
PHASES = ["climb", "cruise", "desc"]


def _thin(t, spacing_s):
    """Same rule as pipeline/flightproc._thin: keep first and last, drop any
    point closer than `spacing_s` to the last kept one. Returns an index mask."""
    if spacing_s <= 0:
        return np.ones(t.size, bool)
    keep = np.zeros(t.size, bool)
    keep[0] = keep[-1] = True
    last = t[0]
    for i in range(1, t.size - 1):
        if t[i] - last >= spacing_s:
            keep[i] = True
            last = t[i]
    return keep


def shares_at(typecode, arrays, u1, u2, spacing_s):
    """Fraction of the real burn falling in each phase, at a given spacing."""
    t = arrays[0]
    m = _thin(t, spacing_s)
    if m.sum() < 12:
        return None
    fl = _flight_from_arrays(typecode, *[a[m] for a in arrays])
    r = estimate_fuel(fl, load_factor=LOAD_FACTOR, reserve_kg=RESERVE_KG,
                      tas_mode="ias", with_steps=True)
    if not r.ok or r.fuel_kg <= 0:
        return None
    prof = _arc_profile(r.burn_kg_step, r.dist_km_step)
    if prof is None:
        return None
    parts = [_burn_between(prof, a, b, last)
             for a, b, last in ((0.0, u1, False), (u1, u2, False),
                                (u2, np.inf, True))]
    tot = sum(parts)
    if tot <= 0:
        return None
    return np.array(parts) / tot, r.fuel_kg, int(m.sum())


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else "2026-07-01"
    nmax = int(sys.argv[2]) if len(sys.argv) > 2 else 1200

    dec = pq.read_table(DEC_DIR / f"{day}.parquet").to_pandas()
    keep = set(dec.flight_id.head(nmax * 2).tolist())
    pts = pq.read_table(FLIGHTS_DIR / day / "points.parquet").to_pandas()
    pts = pts[pts.flight_id.isin(keep)]
    grouped = {f: g for f, g in pts.groupby("flight_id", sort=False)}
    del pts

    rows = []
    for r in dec.itertuples(index=False):
        g = grouped.get(r.flight_id)
        if g is None or len(g) < 30:
            continue
        nom = _build_profile(r.typecode, float(r.flown_km),
                             float(r.cruise_alt_ft), float(r.mean_wpar_track_ms))
        if nom is None:
            continue
        rn = estimate_fuel(nom, load_factor=LOAD_FACTOR, reserve_kg=RESERVE_KG,
                           tas_mode="gs", with_steps=True)
        if not rn.ok:
            continue
        u1, u2 = _nominal_phase_bounds(rn.vs_fpm_step, rn.dist_km_step)
        if not (np.isfinite(u1) and np.isfinite(u2)):
            continue
        arrays = tuple(g[c].to_numpy(np.float64) for c in
                       ("t", "lat", "lon", "alt_ft", "gs_kt", "ias_kt", "vs_fpm"))

        rec = {"flight_id": r.flight_id, "gc_km": r.gc_km,
               "ideal": r.ideal_gc_co2_kg, "co2_v0": r.co2_kg_v0}
        ok = True
        for sp in SPACINGS:
            res = shares_at(r.typecode, arrays, u1, u2, sp)
            if res is None:
                ok = False
                break
            sh, fuel, npt = res
            for k, v in zip(PHASES, sh):
                rec[f"s{sp}_{k}"] = v
            rec[f"fuel{sp}"] = fuel
            rec[f"npt{sp}"] = npt
        if ok:
            rows.append(rec)
        if len(rows) >= nmax:
            break

    df = pd.DataFrame(rows)
    print(f"{day}: {len(df)} voli\n")
    print("punti per volo (mediana):",
          {f"{sp} s": int(df[f'npt{sp}'].median()) for sp in SPACINGS})
    print("carburante totale relativo al passo attuale:",
          {f"{sp} s": round(df[f'fuel{sp}'].sum() / df['fuel0'].sum() - 1, 5)
           for sp in SPACINGS})

    print("\nQUOTA di carburante per fase, aggregata (pesata sul volo):")
    tab = {}
    for sp in SPACINGS:
        w = df[f"fuel{sp}"]
        tab[f"{sp if sp else 10} s"] = {
            k: float((df[f"s{sp}_{k}"] * w).sum() / w.sum()) for k in PHASES}
    t = pd.DataFrame(tab).T
    print((t * 100).round(3).to_string())

    print("\nDeriva della quota per ogni raddoppio del passo (punti di quota):")
    d1 = (t.loc["20 s"] - t.loc["10 s"]) * 100
    d2 = (t.loc["40 s"] - t.loc["20 s"]) * 100
    print(pd.DataFrame({"10->20": d1.round(3), "20->40": d2.round(3)}).to_string())

    print("\nEstrapolazione al passo NULLO e conseguenza sul verticale.")
    print("La traccia immagazzinata sta a ~10 s: se la deriva fosse lineare nel")
    print("passo, il tratto 0->10 varrebbe quanto il tratto 10->20.")
    vert_scale = float((df.co2_v0 / df.ideal * 100).mean())
    for k in PHASES:
        dshare = float(d1[k]) / 100.0
        print(f"  {k:7s}: quota estrapolata a 0 s = "
              f"{(t.loc['10 s', k] - dshare)*100:7.3f}%  "
              f"(oggi {t.loc['10 s', k]*100:7.3f}%)  -> effetto sul termine "
              f"verticale ~ {-dshare*vert_scale:+.3f} punti")
    print(f"\n[la scala usata e' co2_v0/ideale medio = {vert_scale:.1f}%, "
          f"cioe' quanto vale una quota piena in punti percentuali]")


if __name__ == "__main__":
    main()
