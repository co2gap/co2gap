#!/usr/bin/env python3
"""
Fase 2a: aggregate the lateral/vertical decomposition and print the tables
that go into reports/fase2a.md.

Reads whatever data/decomposition/*.parquet exist (produced by
lab/run_decompose.py) and prints, all as MEDIANS unless stated:

  1. headline split of the excess into lateral + vertical/speed
  2. the same by distance band (the split is strongly distance-dependent)
  3. the KEA-homogeneous en-route distance ratio, which is the number to
     reconcile against EUROCONTROL's published ~3%
  4. month-by-month, as an early look at stability (fase 2b's criterion 2)

Every number printed is PROVISIONAL while the sample is not a full year:
the available days are late-spring/summer only, so nothing here says
anything about winter, and no ranking printed here is a verdict.

Usage: lab-venv/bin/python lab/decompose_report.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
DEC_DIR = ROOT / "data/decomposition"

BANDS = [150, 300, 500, 800, 1200, 2000, 20000]
BAND_LABELS = ["150–300", "300–500", "500–800", "800–1200",
               "1200–2000", ">2000"]
KEA_REFERENCE_PCT = 3.0    # EUROCONTROL published en-route extension, ~3%
MIN_N_ROUTE = 30


def load() -> pd.DataFrame:
    files = sorted(DEC_DIR.glob("*.parquet"))
    if not files:
        raise SystemExit(f"no decomposition parquet under {DEC_DIR} "
                         "— run lab/run_decompose.py first")
    df = pd.concat([pq.read_table(f).to_pandas() for f in files],
                   ignore_index=True)
    df["band"] = pd.cut(df.gc_km, BANDS, labels=BAND_LABELS)
    df["month"] = df.day.str.slice(0, 7)
    return df


def banner(s):
    print(f"\n{'='*72}\n{s}\n{'='*72}")


def main():
    df = load()
    days = sorted(df.day.unique())
    banner(f"DECOMPOSIZIONE LATERALE / VERTICALE — {len(df):,} voli, "
           f"{len(days)} giorni ({days[0]} → {days[-1]})")
    print("*** TUTTI I NUMERI SONO PROVVISORI: il campione è solo tarda "
          "primavera/estate. ***")

    # ---- 1. headline ------------------------------------------------------
    banner("1. Scomposizione dell'excess (mediane, punti percentuali)")
    tot = df.excess_total_pct.median()
    lat = df.excess_lateral_pct.median()
    ver = df.excess_vertical_pct.median()
    print(f"  excess TOTALE               {tot:8.2f} %")
    print(f"    di cui LATERALE           {lat:8.2f} %   "
          f"({lat/tot*100:.0f}% del totale)")
    print(f"    di cui VERTICALE/VELOCITÀ {ver:8.2f} %   "
          f"({ver/tot*100:.0f}% del totale)")
    print("  (le due componenti sono additive per costruzione: stesso "
          "denominatore)")
    resid = (df.excess_total_pct
             - df.excess_lateral_pct - df.excess_vertical_pct).abs().max()
    print(f"  verifica additività: max |residuo| = {resid:.2e} punti")

    # ---- 2. by distance band ---------------------------------------------
    banner("2. Per fascia di distanza (mediane)")
    t = df.groupby("band", observed=True).agg(
        n=("gc_km", "size"),
        excess_tot=("excess_total_pct", "median"),
        lat=("excess_lateral_pct", "median"),
        vert=("excess_vertical_pct", "median"),
        ratio_gate=("dist_ratio", lambda s: (s.median() - 1) * 100),
        ratio_enroute=("dist_ratio_enroute", lambda s: (s.median() - 1) * 100),
    )
    t.columns = ["n", "excess%", "laterale%", "verticale%",
                 "dist+% (gate-gate)", "dist+% (en-route)"]
    print(t.round(2).to_string())
    print("\n  Lettura: sulle tratte corte domina la componente verticale "
          "(salita/discesa\n  e vettoramento pesano su un volo breve); "
          "la componente laterale in\n  percentuale di distanza en-route è "
          "invece quasi costante.")

    # ---- 3. KEA reconciliation -------------------------------------------
    banner("3. Riconciliazione con EUROCONTROL KEA (criterio 5 del go/no-go)")
    er = df.dist_ratio_enroute.dropna()
    gg = df.dist_ratio.dropna()
    print(f"  estensione di percorso GATE-TO-GATE   "
          f"{(gg.median()-1)*100:+6.2f} %   (n={len(gg):,})")
    print(f"  estensione di percorso EN-ROUTE       "
          f"{(er.median()-1)*100:+6.2f} %   (n={len(er):,})  "
          f"<-- omogenea al KEA")
    print(f"  KEA pubblicato EUROCONTROL (Europa)   {KEA_REFERENCE_PCT:+6.2f} % "
          f"  (riferimento)")
    print()
    print("  Il KEA è definito sulla porzione EN-ROUTE (fuori da un cerchio di")
    print("  40 NM dagli aeroporti) e usa come riferimento la 'achieved")
    print("  distance', cioè una distanza great-circle — quindi la nostra")
    print("  metrica en-route è costruita nello stesso modo. Il confronto")
    print("  gate-to-gate NON sarebbe legittimo: include SID/STAR, vettoramento")
    print("  e avvicinamento, che il KEA esclude per definizione.")

    # ---- 4. stability preview --------------------------------------------
    banner("4. Anteprima di stabilità mese-su-mese (criterio 2, PARZIALE)")
    m = df.groupby("month").agg(
        n=("gc_km", "size"),
        excess=("excess_total_pct", "median"),
        lat=("excess_lateral_pct", "median"),
        vert=("excess_vertical_pct", "median"),
        enroute=("dist_ratio_enroute", lambda s: (s.median() - 1) * 100),
    )
    print(m.round(2).to_string())
    print("\n  ATTENZIONE: mesi incompleti e tutti estivi. Non è il test di")
    print("  stabilità della fase 2b, è solo un controllo che la macchina")
    print("  produca numeri coerenti da un mese all'altro.")

    # ---- 5. where the lateral component is largest -----------------------
    banner(f"5. Rotte con la maggiore estensione EN-ROUTE (n>={MIN_N_ROUTE}) "
           "— PROVVISORIO")
    df["route"] = [tuple(sorted([o, d])) for o, d in
                   zip(df.origin_icao, df.dest_icao)]
    g = df.dropna(subset=["dist_ratio_enroute"]).groupby("route").agg(
        n=("gc_km", "size"),
        gc=("gc_km", "median"),
        enroute=("dist_ratio_enroute", lambda s: (s.median() - 1) * 100),
        lat=("excess_lateral_pct", "median"),
        vert=("excess_vertical_pct", "median"),
    )
    g = g[g.n >= MIN_N_ROUTE].sort_values("enroute", ascending=False)
    print("  top 15 per estensione en-route:")
    print(g.head(15).round(2).to_string())
    print("\n  Queste NON sono classifiche definitive: servono a verificare che")
    print("  gli outlier abbiano una storia operativa plausibile (criterio 4).")


if __name__ == "__main__":
    main()
