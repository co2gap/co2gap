#!/usr/bin/env python3
"""Riproduce le cifre pubblicate dai dati, prima di fidarsi di qualunque rilievo.

Nato il 2026-08-28 per verificare i rilievi di una revisione esterna: nessuno
di quei sette e' stato toccato prima di aver rifatto il conto qui. Serve allo
stesso scopo la prossima volta, e come controllo che un dataset diverso non sia
entrato in silenzio.

Importa `load()` da site_build, quindi misura ESATTAMENTE cio' che il sito
misura — comprese la correzione del burn di terra e le percentuali ricalcolate.
Una seconda implementazione delle stesse formule divergerebbe al primo
aggiornamento, ed e' il modo in cui questo progetto ha gia' sbagliato.

    ADSB_ROOT=$PWD \\
    ADSB_DECOMP_DIR=$PWD/data/decomposition_ecac \\
    ADSB_PHASE_DIR=$PWD/data/decomposition_ecac_phase \\
    ADSB_GROUND_DIR=$PWD/data/ground_share_ecac \\
    ADSB_CALIB=$PWD/data/calibration_ecac.json \\
    ADSB_AIRPORTS_CSV=$PWD/data/airports_ecac.csv \\
    ../lab-venv/bin/python lab/reproduce_headline.py

SOLA LETTURA: non scrive nei dati e non tocca il sito.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(os.environ.get("ADSB_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "lab"))
sys.path.insert(0, str(ROOT))
import site_build as S  # noqa: E402

# Perimetro di Pasutto et al. (EUROCONTROL, 2021): 200-1500 NM in km.
PAS_LO, PAS_HI, PAS_LORO = 370.4, 2778.0, 4.6


def main() -> int:
    df = S.load()
    print(f"\nvoli {len(df):,} · giorni {df.day.nunique()}\n")

    idl = df.ideal_gc_co2_kg.sum()
    lat = (df.hybrid_co2_kg.sum() - idl) / idl * 100
    vert = (df.co2_kg_v0.sum() - df.hybrid_co2_kg.sum()) / idl * 100
    print("CIFRE DI TESTA  (rapporti di somme, colonne NON calibrate)")
    print(f"  laterale {lat:6.2f} · verticale {vert:6.2f} · totale {lat+vert:6.2f}")
    print(f"  CO2 in volo {df.co2_real_kg.sum()/1e9:6.2f} Mt · "
          f"gap {(df.co2_real_kg.sum()-df.co2_ideal_kg.sum())/1e9:.2f} Mt")
    print(f"  quota di terra esclusa: {df.share_ground.mean()*100:.2f}% del carburante\n")

    p = df[(df.gc_km >= PAS_LO) & (df.gc_km <= PAS_HI)]
    pid = p.ideal_gc_co2_kg.sum()
    p_lat = (p.hybrid_co2_kg.sum() - pid) / pid * 100
    p_vert = (p.co2_kg_v0.sum() - p.hybrid_co2_kg.sum()) / pid * 100
    print(f"PERIMETRO PASUTTO 200-1500 NM · {len(p):,} voli")
    print(f"  totale {p_lat+p_vert:5.2f}% = laterale {p_lat:.2f} + verticale {p_vert:.2f}")
    print(f"  loro {PAS_LORO}% (SOLA CROCIERA) → fattore sul totale "
          f"{(p_lat+p_vert)/PAS_LORO:.2f}, sul verticale {p_vert/PAS_LORO:.2f}")
    print("  ⚠️ il secondo NON e' un accordo: profilo intero contro sola crociera\n")

    both = pd.concat([df.assign(ap=df.origin_icao), df.assign(ap=df.dest_icao)])
    ga = both.groupby("ap").agg(n=("d_tot", "size"), d=("d_tot", "median"))
    ga = ga[ga.n >= S.MIN_N_AIRPORT]
    med = float(ga.d.median())
    print(f"AEROPORTI · {len(ga)} in tabella")
    print(f"  massimo {ga.d.max():+.2f} ({ga.d.idxmax()}) · minimo {ga.d.min():+.2f} "
          f"({ga.d.idxmin()}) · mediana {med:+.2f}")
    print(f"  dalla NORMA, per un «no more than»: {int(np.ceil(ga.d.max()))} sopra, "
          f"{int(np.ceil(-ga.d.min()))} sotto")
    print(f"  ⚠️ dalla MEDIANA darebbe {ga.d.max()-med:.2f}/{med-ga.d.min():.2f}: "
          f"altra base, altra frase\n")

    lungo = df[df.gc_km > 1000]
    floor = float(df.loc[(df.dist_ratio < 1.02) & (df.gc_km > 1000), "excess_vertical_pct"]
                  .median())
    print("FLOOR E MARGINE")
    print(f"  verticale mediano: tutti i voli {df.excess_vertical_pct.median():.2f} · "
          f"sopra 1.000 km {lungo.excess_vertical_pct.median():.2f}")
    print(f"  ⚠️ il floor si misura sopra i 1.000 km: sottrarre la mediana di flotta "
          f"mette nel margine anche il mix di distanza\n")

    cf = S.convention_medians(df)
    print(f"CONVENZIONI, volo mediano (lat/vert): {cf[0]:.1f}/{cf[1]:.1f} → "
          f"{cf[2]:.1f}/{cf[3]:.1f}")
    print(f"  maggiore in entrambe: "
          f"{'laterale' if cf[0] > cf[1] and cf[2] > cf[3] else 'NON costante — guardare'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
