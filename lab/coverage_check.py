#!/usr/bin/env python3
"""La classifica per aeroporto segue la mappa dei ricevitori ADS-B?

Nato il 2026-08-29, il giorno prima della pubblicazione, da un'osservazione
dell'utente: la classifica somiglia alla carta della densita' di popolazione.
E' vera, e la domanda che apre e' seria — se i ricevitori radi ricostruissero
peggio le traiettorie, la classifica misurerebbe la rete e non i voli.

Misura la catena in tre anelli, e serve il terzo:

  1. i feeder spiegano la QUALITA' della traiettoria?
  2. la qualita' spiega il VERTICALE?
  3. ⭐ tolta la qualita', i feeder predicono ANCORA la deviazione?

Se l'artefatto esistesse, la correlazione dovrebbe passare per la qualita' e
sparire al punto 3. Il 2026-08-29 non spariva: +0,361 a traffico fermo, +0,385
togliendo anche coverage_frac. E coverage_frac ha mediana 1,0000 su tutti i 152
aeroporti, minimo 0,893: non esiste una zona d'Europa ricostruita male.

Resta una correlazione vera che NON passa dalla strumentazione, e che il sito
dichiara fra i limiti. I feeder sono un proxy migliore della congestione dello
spazio aereo di quanto lo sia il traffico del singolo scalo.

⚠️ Limiti di questa misura, da tenere presenti se si rilancia: la mappa MLAT e'
di OGGI, non del periodo pubblicato; esclude i feeder con privacy attiva; e
coverage_frac misura cio' che e' stato ricevuto, quindi non puo' escludere una
zona coperta male da tutti gli aggregatori insieme.

    ADSB_ROOT=$PWD ADSB_DECOMP_DIR=$PWD/data/decomposition_ecac \\
    ADSB_GROUND_DIR=$PWD/data/ground_share_ecac \\
    ADSB_CALIB=$PWD/data/calibration_ecac.json \\
    ADSB_AIRPORTS_CSV=$PWD/data/airports_ecac.csv \\
    ../lab-venv/bin/python lab/coverage_check.py

SOLA LETTURA: legge i dati congelati e un'API pubblica, non scrive nulla.
"""
import glob, json, os, sys, urllib.request
import numpy as np, pandas as pd, pyarrow.parquet as pq
from scipy.stats import spearmanr
R = "/Users/fmavellia/adsb-co2-lab/adsb-co2"
sys.path.insert(0, f"{R}/lab"); sys.path.insert(0, R)
os.environ.update(ADSB_ROOT=R, ADSB_DECOMP_DIR=f"{R}/data/decomposition_ecac",
  ADSB_PHASE_DIR=f"{R}/data/decomposition_ecac_phase",
  ADSB_GROUND_DIR=f"{R}/data/ground_share_ecac",
  ADSB_CALIB=f"{R}/data/calibration_ecac.json",
  ADSB_AIRPORTS_CSV=f"{R}/data/airports_ecac.csv")

QC = ["day","flight_id","n_pts_native","n_pts_stored","coverage_frac","max_gap_s"]
files = sorted(glob.glob(f"{R}/data/flights_ecac/*/flights.parquet"))
q = pd.concat([pq.read_table(x, columns=QC).to_pandas() for x in files], ignore_index=True)
print(f"giorni {len(files)} · voli con metriche {len(q):,}")

import site_build as S
df = S.load()
n0 = len(df)
df = df.merge(q, on=["day","flight_id"], how="left")
assert len(df) == n0
print(f"appaiati {df.coverage_frac.notna().mean()*100:.1f}%")
df = df[df.coverage_frac.notna()]

pts = {}
for s in ("0A","0B","0C","0D","1A","2A","2B","2C"):
    j = json.loads(urllib.request.urlopen(
        f"https://mlat.adsb.lol/api/0/mlat-server/{s}/sync.json", timeout=30).read())
    for k, v in j.items():
        if isinstance(v, dict) and isinstance(v.get("lat"), (int, float)):
            pts[f"{s}:{k}"] = (float(v["lat"]), float(v["lon"]))
FE = np.array(list(pts.values()))

both = pd.concat([df.assign(ap=df.origin_icao), df.assign(ap=df.dest_icao)])
ga = both.groupby("ap").agg(
    n=("d_tot","size"), dev=("d_tot","median"), vert=("d_vert","median"),
    latc=("d_lat","median"), qc=("coverage_frac","median"),
    gp=("max_gap_s","median"), pn=("n_pts_native","median"))
ga = ga[ga["n"] >= S.MIN_N_AIRPORT].reset_index()
apc = pd.read_csv(os.environ["ADSB_AIRPORTS_CSV"])
cc = {x.lower(): x for x in apc.columns}
apc = apc[[cc.get("icao") or cc.get("ident"), cc.get("lat") or cc.get("latitude_deg"),
           cc.get("lon") or cc.get("longitude_deg")]]
apc.columns = ["ap","lat","lon"]
g = ga.merge(apc.drop_duplicates("ap"), on="ap").dropna(subset=["lat","lon"])

def entro(la, lo, km=100):
    dla = np.radians(FE[:,0]-la); dlo = np.radians(FE[:,1]-lo)
    h = np.sin(dla/2)**2+np.cos(np.radians(la))*np.cos(np.radians(FE[:,0]))*np.sin(dlo/2)**2
    return int((2*6371*np.arcsin(np.sqrt(h)) <= km).sum())
g["fd"] = [entro(r.lat, r.lon) for r in g.itertuples()]
g["ln"] = np.log(g["n"].to_numpy())

def parz(x, y, z):
    rx, ry, rz = (pd.Series(np.asarray(v)).rank().to_numpy() for v in (x, y, z))
    ex = rx - np.polyval(np.polyfit(rz, rx, 1), rz)
    ey = ry - np.polyval(np.polyfit(rz, ry, 1), rz)
    return float(np.corrcoef(ex, ey)[0, 1])

fd, dev, vert, latc = (g[c].to_numpy() for c in ("fd","dev","vert","latc"))
qc, gp, pn, ln = (g[c].to_numpy() for c in ("qc","gp","pn","ln"))
print(f"\n=== {len(g)} aeroporti ===")
print("\nqualita' della traiettoria per aeroporto (mediane):")
print(f"  coverage_frac  {np.median(qc):.4f}   min {qc.min():.4f}   max {qc.max():.4f}")
print(f"  max_gap_s      {np.median(gp):.0f}s      min {gp.min():.0f}       max {gp.max():.0f}")
print(f"  punti nativi   {np.median(pn):.0f}      min {pn.min():.0f}      max {pn.max():.0f}")

print("\nANELLO 1 — i feeder spiegano la qualita'?")
for nome, col in (("coverage_frac", qc), ("max_gap_s", gp), ("punti nativi", pn)):
    print(f"  {nome:14s} ~ feeder   {spearmanr(fd, col).statistic:+.3f}")

print("\nANELLO 2 — la qualita' spiega il verticale? (a traffico fermo)")
for nome, col in (("coverage_frac", qc), ("max_gap_s", gp), ("punti nativi", pn)):
    print(f"  verticale ~ {nome:14s} {parz(col, vert, ln):+.3f}")

print("\nIL TEST — i feeder predicono ancora, tolta la qualita'?")
print(f"  deviazione ~ feeder | traffico                  {parz(fd, dev, ln):+.3f}")
for nome, col in (("coverage_frac", qc), ("punti nativi", pn)):
    z = (pd.Series(ln).rank().to_numpy() + pd.Series(col).rank().to_numpy())/2
    print(f"  deviazione ~ feeder | traffico + {nome:14s} {parz(fd, dev, z):+.3f}")
