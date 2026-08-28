#!/usr/bin/env python3
"""Le quote per fase stanno sulla stessa base del verticale pubblicato?

Il 2026-08-28 non ci stavano, e il sito ha pubblicato quote fuori dal 100% —
233% entro 40 NM, 242% in discesa. Le colonne di fase vivono in un parquet a
parte (`ADSB_PHASE_DIR`) prodotto PRIMA della correzione del burn di terra:
ricostruivano il verticale gate-to-gate mentre il denominatore arrivava
corretto da `site_build.load()`.

`site_build.phase_attribution()` ora sottrae il burn di terra dai secchielli e
ha un assert sul residuo additivo, quindi il difetto non puo' tornare in
silenzio. Questo script resta come DIAGNOSTICA: stampa le quote sotto le tre
basi affiancate, che l'assert non puo' mostrare perche' si limita a fallire.

    ADSB_ROOT=$PWD \\
    ADSB_DECOMP_DIR=$PWD/data/decomposition_ecac \\
    ADSB_PHASE_DIR=$PWD/data/decomposition_ecac_phase \\
    ADSB_GROUND_DIR=$PWD/data/ground_share_ecac \\
    ../lab-venv/bin/python lab/phase_basis_check.py

SOLA LETTURA: non scrive nei dati e non tocca il sito.
"""
from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(os.environ.get("ADSB_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "lab"))
from phase_attrib import add_mean_norm, by_airport, headline  # noqa: E402

DEC = Path(os.environ.get("ADSB_DECOMP_DIR") or ROOT / "data/decomposition_ecac")
PHA = Path(os.environ.get("ADSB_PHASE_DIR") or ROOT / "data/decomposition_ecac_phase")
GND = Path(os.environ.get("ADSB_GROUND_DIR") or ROOT / "data/ground_share_ecac")
DEF = os.environ.get("ADSB_GROUND_DEF", "a3000t70")

# Stessi valori di site_build.py: una copia che diverge misurerebbe altro.
BINS = [0, 200, 300, 400, 500, 650, 800, 1000, 1200, 1500, 2000, 3000, 99999]
MIN_N_CELL, MIN_N_AIRPORT = 200, 2000


def _cat(d: Path, cols=None) -> pd.DataFrame:
    files = sorted(glob.glob(str(d / "*.parquet")))
    if not files:
        raise SystemExit(f"nessun parquet in {d}")
    return pd.concat([pq.read_table(f, columns=cols).to_pandas() for f in files],
                     ignore_index=True)


def main() -> int:
    df = _cat(DEC, ["day", "flight_id", "typecode", "origin_icao", "dest_icao",
                    "gc_km", "co2_kg_v0", "ideal_gc_co2_kg", "hybrid_co2_kg"])
    g = _cat(GND, ["day", "flight_id", "fuel_recomputed_kg",
                   f"fuel_{DEF}_kg", f"fuel_{DEF}_dep_kg", f"fuel_{DEF}_arr_kg"])
    ph = _cat(PHA)

    fr = g.fuel_recomputed_kg.to_numpy()
    for suf in ("", "_dep", "_arr"):
        g["s" + suf] = np.where(fr > 0, g[f"fuel_{DEF}{suf}_kg"].to_numpy() / fr, 0.0)

    n0 = len(df)
    df = df.merge(g[["day", "flight_id", "s", "s_dep", "s_arr"]],
                  on=["day", "flight_id"], how="left")
    assert len(df) == n0, "il merge ha duplicato: flight_id e' un surrogato PER GIORNO"
    manca = sorted(set(df.day.unique()) - set(g.day.unique()))
    if manca:
        print(f"⚠️  quota di terra assente per {len(manca)} giorni: {manca[:3]}...")
    for c in ("s", "s_dep", "s_arr"):
        df[c] = df[c].fillna(0.0)

    gg, idl, hyb = (df.co2_kg_v0.to_numpy(), df.ideal_gc_co2_kg.to_numpy(),
                    df.hybrid_co2_kg.to_numpy())
    df["vert_gg"] = (gg - hyb) / idl * 100
    df["vert_corr"] = (gg * (1 - df.s.to_numpy()) - hyb) / idl * 100
    df["gpct_dep"] = gg * df.s_dep.to_numpy() / idl * 100
    df["gpct_arr"] = gg * df.s_arr.to_numpy() / idl * 100

    m = df.merge(ph, on=["day", "flight_id"], how="inner", validate="one_to_one")
    print(f"voli con split di fase: {len(m):,}\n")

    def giro(label: str, vert: str, sottrai: bool) -> None:
        x = m.copy()
        x["excess_vertical_pct"] = x[vert]
        if sottrai:
            x["excess_vert_dep_pct"] -= x.gpct_dep
            x["excess_vert_arr_pct"] -= x.gpct_arr
            x["excess_vert_climb_pct"] -= x.gpct_dep
            x["excess_vert_desc_pct"] -= x.gpct_arr
        parti = ["excess_vert_dep_pct", "excess_vert_enr_pct", "excess_vert_arr_pct"]
        # ⚠️ sum(axis=1) IGNORA i NaN: una riga con una fase mancante somma le
        # altre due e produce un residuo enorme che non e' una rottura, e' un
        # buco. Misurato: mediano 5,7e-14 e massimo 2,3e+02 sulla stessa base
        # coerente. Uno script che stampa «un residuo che non e' epsilon sono
        # due basi diverse» e poi mostra 228 insegna a ignorare i massimi, che
        # e' il modo migliore per non vedere la rottura vera. Si misura solo
        # dove le tre parti ci sono tutte, e si dice quante righe non lo sono —
        # e' la stessa maschera dell'assert in site_build.phase_attribution().
        ok = x[parti + [vert]].notna().all(axis=1)
        res = (x.loc[ok, parti].sum(axis=1) - x.loc[ok, vert]).abs()
        buchi = int((~ok).sum())
        h = headline(by_airport(add_mean_norm(x, BINS, MIN_N_CELL), MIN_N_AIRPORT))
        print(f"--- {label}")
        print(f"    residuo additivo: mediano {res.median():.2e} · massimo "
              f"{res.max():.2e}  (su {int(ok.sum()):,} righe complete"
              + (f", {buchi:,} incomplete escluse)" if buchi else ")"))
        print(f"    partenze {h['dep_own']:6.1f}% entro 40 NM · {h['dep_climb']:6.1f}% "
              f"in salita   ({h['n_dep']} aeroporti)")
        print(f"    arrivi   {h['arr_own']:6.1f}% entro 40 NM · {h['arr_desc']:6.1f}% "
              f"in discesa  ({h['n_arr']} aeroporti)\n")

    giro("BASI MISTE — fasi gate-to-gate su verticale corretto (il difetto del 28/08)",
         "vert_corr", False)
    giro("COERENTE GATE-TO-GATE — quel che il sito pubblicava prima della correzione",
         "vert_gg", False)
    giro("CORRETTO — le due basi allineate, quel che il sito pubblica ora",
         "vert_corr", True)
    print("I residui dicono tutto: sulle righe COMPLETE le parti sono additive per\n"
          "costruzione, quindi un residuo che non e' epsilon e' due basi diverse.\n"
          "Le righe incomplete sono escluse e contate: non sono una rottura.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
