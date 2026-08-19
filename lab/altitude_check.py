#!/usr/bin/env python3
"""Il baseline chiede quote che gli aerei volano davvero?

La domanda nasce da un'obiezione esterna — «il profilo ideale pretende una
crociera infattibile sulle tratte corte, quindi il divario piu' grande del sito
e' gonfiato» — e da un difetto noto: misurata sulla sola crociera la nostra
differenza esce leggermente NEGATIVA, cioe' l'aereo vero brucia meno del profilo
che chiamiamo ottimo.

Entrambe si rispondono con i dati invece che a parole: `max_alt_ft` nel parquet
dei voli e' la quota massima realmente raggiunta.

⚠️ NON usare `cruise_alt_ft` della scomposizione: e' la quota NOMINALE, non
quella volata. Confrontarla col baseline da' scarto zero in ogni fascia — un
"tutto a posto" che e' solo l'ideale confrontato con se' stesso.

    PYTHONPATH=pipeline:ingest ADSB_FLIGHTS_DIR=data/flights_ecac \\
      lab-venv/bin/python lab/altitude_check.py
"""
import glob
import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
from excess_wind import optimal_cruise_alt_ft            # noqa: E402

FLIGHTS = Path(os.environ.get("ADSB_FLIGHTS_DIR") or "data/flights_ecac")
EVERY = int(os.environ.get("ALT_CHECK_EVERY", "4"))      # 1 = tutti i giorni
BINS = [0, 200, 300, 400, 500, 650, 800, 1000, 1500, 99999]


def main():
    fs = sorted(glob.glob(str(FLIGHTS / "*" / "flights.parquet")))[::EVERY]
    if not fs:
        sys.exit(f"nessun parquet in {FLIGHTS}")
    d = pd.concat([pd.read_parquet(f, columns=["typecode", "gc_km", "max_alt_ft",
                                               "flown_ge_09gc"]) for f in fs],
                  ignore_index=True)
    d = d[(d.max_alt_ft > 0) & d.flown_ge_09gc]          # stesso cancello del sito
    d["band"] = pd.cut(d.gc_km, BINS)
    d["ideal"] = [optimal_cruise_alt_ft(t, k) for t, k in zip(d.typecode, d.gc_km)]
    g = d.groupby("band", observed=True).agg(
        n=("gc_km", "size"), real=("max_alt_ft", "median"),
        ideal=("ideal", "median"))
    g["delta"] = g.ideal - g.real
    print(f"{len(fs)} giorni · {len(d):,} voli · quota MASSIMA raggiunta (non il "
          f"livello di crociera)\n")
    print(f"{'banda km':14s}{'voli':>9}{'reale':>9}{'baseline':>10}{'scarto':>9}")
    for i, r in g.iterrows():
        print(f"{str(i):14s}{int(r.n):9,}{r.real:9,.0f}{r.ideal:10,.0f}{r.delta:+9,.0f}")
    short, long_ = g.iloc[0], g.iloc[-1]
    print(f"\nle due cifre citate sul sito:")
    verso = "MENO" if short.delta < 0 else "PIU'"
    print(f"  tratte piu' corte: il baseline chiede {abs(short.delta):,.0f} ft in "
          f"{verso} del volato")
    print(f"  tratte piu' lunghe: il baseline crocia {abs(long_.delta):,.0f} ft "
          f"{'SOTTO' if long_.delta < 0 else 'SOPRA'} il volato")


if __name__ == "__main__":
    main()
