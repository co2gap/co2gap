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


def kea_ratio(d) -> float:
    """En-route extension aggregated the way EUROCONTROL aggregates KEA: a
    ratio of SUMS over the sample, not a median of per-flight ratios."""
    gc = d.flown_enroute_km / d.dist_ratio_enroute
    return (d.flown_enroute_km.sum() - gc.sum()) / gc.sum() * 100.0


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
    d = df.dropna(subset=["dist_ratio_enroute"]).copy()
    d["gc_enroute_km"] = d.flown_enroute_km / d.dist_ratio_enroute
    kea_style = kea_ratio(d)
    print(f"  estensione EN-ROUTE, aggregata come il KEA   "
          f"{kea_style:+6.2f} %   (n={len(d):,})  <-- IL CONFRONTO")
    print(f"  KEA pubblicato EUROCONTROL (Europa)          "
          f"{KEA_REFERENCE_PCT:+6.2f} %     (riferimento)")
    print()
    print(f"  per confronto, stesse tracce ma altre aggregazioni:")
    print(f"    mediana dei rapporti per volo (en-route)   "
          f"{(d.dist_ratio_enroute.median()-1)*100:+6.2f} %")
    print(f"    mediana dei rapporti per volo (gate-gate)  "
          f"{(df.dist_ratio.median()-1)*100:+6.2f} %  <-- NON confrontabile")
    print()
    print("  Perché queste tre cifre sono diverse, e quale è quella giusta:")
    print("  * il KEA è un RAPPORTO DI SOMME (distanza totale volata su")
    print("    distanza totale 'achieved'), non una mediana di rapporti per")
    print("    volo: pesa quindi di più i voli lunghi. La distribuzione è")
    print("    asimmetrica a destra, quindi il rapporto di somme viene più")
    print("    alto della mediana. Usiamo la loro aggregazione.")
    print("  * il KEA è EN-ROUTE, cioè fuori da un cerchio di 40 NM dagli")
    print("    aeroporti. Il gate-to-gate include SID/STAR, vettoramento e")
    print("    avvicinamento, che il KEA esclude per definizione: confrontarlo")
    print("    col KEA gonfierebbe il nostro numero di tre volte.")
    print()
    print("  Differenza residua che NON possiamo togliere: il KEA è calcolato")
    print("  sull'area di riferimento EUROCONTROL con i dati radar ETFMS, noi")
    print("  su un box EU-Sud da ADS-B, su un sottoinsieme quality-gated e su")
    print("  giorni estivi. Il confronto dice 'stesso ordine di grandezza,")
    print("  costruito allo stesso modo', non 'riproduciamo il loro numero'.")

    print("\n  --- controllo: la metrica en-route è affidabile solo se la")
    print("      porzione en-route è una parte consistente del volo ---")
    d["frac_enroute"] = d.flown_enroute_km / d.flown_km
    fr = d.groupby("band", observed=True).agg(
        n=("gc_km", "size"),
        frazione_enroute=("frac_enroute", "median"),
        kea_style=("gc_km", "size"),
    )
    fr["kea_style"] = [kea_ratio(g) for _, g in d.groupby("band", observed=True)]
    print(fr.round(3).to_string())
    print("  Sulle tratte <500 km i due cilindri da 40 NM mangiano metà o più")
    print("  del volo: lì la metrica en-route è diluita e va letta con cautela.")
    print("  Sopra gli 800 km si stabilizza, ed è il regime in cui il confronto")
    print("  col KEA ha senso.")

    # ---- 4. stability preview --------------------------------------------
    banner("4. Anteprima di stabilità mese-su-mese (criterio 2, PARZIALE)")
    m = df.groupby("month").agg(
        n=("gc_km", "size"),
        excess=("excess_total_pct", "median"),
        lat=("excess_lateral_pct", "median"),
        vert=("excess_vertical_pct", "median"),
    )
    m["kea_style"] = [kea_ratio(g.dropna(subset=["dist_ratio_enroute"]))
                      for _, g in df.groupby("month")]
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
