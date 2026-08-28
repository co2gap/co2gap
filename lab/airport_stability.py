"""Il segnale per aeroporto e' piu' grande della sua oscillazione stagionale?

E' il criterio che il sito stesso applica: «what is stable is the distance from
the norm, not the position». Un test che confronta due meta' del periodo
(pari/dispari) NON risponde: entrambe coprono tutti i mesi e mediano via proprio
la variazione che conta. Il test giusto e' MESE SU MESE.

Serve a giustificare che la classifica per aeroporto resti pubblicabile dopo la
correzione del rullaggio: se il rapporto ampiezza/escursione non peggiora, la
classifica corretta e' significativa quanto quella precedente.

  PYTHONPATH=pipeline lab-venv/bin/python lab/airport_stability.py \
      --decomp data/decomposition_ecac --ground data/ground_share_ecac

Sola lettura.
"""
import argparse, glob
import numpy as np, pandas as pd

BINS = [0, 200, 300, 400, 500, 650, 800, 1000, 1200, 1500, 2000, 3000, 99999]
MIN_N_CELL = 200          # come site_build.py
MIN_N_MESE = 200          # movimenti minimi per contare un mese

ap = argparse.ArgumentParser()
ap.add_argument("--decomp", required=True)
ap.add_argument("--ground", required=True)
ap.add_argument("--def-terra", default="a3000t70")
ap.add_argument("--min-mesi", type=int, default=6)
a = ap.parse_args()

d = pd.concat([pd.read_parquet(f) for f in sorted(glob.glob(f"{a.decomp}/*.parquet"))],
              ignore_index=True)
g = pd.concat([pd.read_parquet(f) for f in sorted(glob.glob(f"{a.ground}/*.parquet"))],
              ignore_index=True)
g["share"] = np.where(g.fuel_recomputed_kg > 0,
                      g[f"fuel_{a.def_terra}_kg"] / g.fuel_recomputed_kg, 0.0)
n0 = len(d)
d = d.merge(g[["day", "flight_id", "share"]], on=["day", "flight_id"], how="left")
assert len(d) == n0, "merge duplicato: flight_id e' un surrogato PER GIORNO"
d["share"] = d.share.fillna(0.0)
d["co2_corr"] = d.co2_kg_v0 * (1 - d.share)
d["mese"] = pd.to_datetime(d.day).dt.month
print(f"  {len(d):,} voli · {d.mese.nunique()} mesi · correzione su {(d.share > 0).mean()*100:.0f}%")

def per_mese(col):
    x = d.copy()
    x["et"] = (x[col] - x.ideal_gc_co2_kg) / x.ideal_gc_co2_kg * 100
    x["bin"] = pd.cut(x.gc_km, BINS).astype(str)
    c = x["bin"] + "|" + x.typecode
    e = c.map(c.value_counts()) >= MIN_N_CELL
    mb = x["bin"].map(x.groupby("bin")["et"].median()).to_numpy()
    mc = c.map(x[e].groupby(c[e])["et"].median()).to_numpy()
    x["d"] = x.et.to_numpy() - np.where(e.to_numpy() & np.isfinite(mc), mc, mb)
    b = pd.concat([x.assign(ap=x.origin_icao), x.assign(ap=x.dest_icao)])
    t = b.groupby(["ap", "mese"]).agg(n=("flight_id", "size"), d=("d", "median")).reset_index()
    return t[t.n >= MIN_N_MESE].pivot(index="ap", columns="mese", values="d") \
                               .dropna(thresh=a.min_mesi)

print(f"\n  {'':22}{'escursione fra mesi':>21}{'ampiezza':>11}{'rapporto':>10}")
for col, lab in (("co2_kg_v0", "congelato"), ("co2_corr", "corretto")):
    P = per_mese(col)
    esc = (P.max(axis=1) - P.min(axis=1))
    amp = P.mean(axis=1).std()
    print(f"  {lab:12}{len(P):4d} aeroporti  mediana {esc.median():5.2f}  p90 {esc.quantile(.9):5.2f}"
          f"  {amp:9.2f}{amp/esc.median():10.2f}")
print("\n  Il rapporto e' il segnale diviso il rumore stagionale. Se non peggiora,")
print("  la classifica corretta e' significativa quanto quella precedente.")
