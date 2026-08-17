#!/usr/bin/env python3
"""
Fase 3: aggregate the phase split and run its validation gates.

Reads the frozen decomposition and the phase parquet produced by
lab/run_phase_split.py, joins them on day+flight_id, and prints the tables the
report is written from. It publishes nothing and writes nothing.

    ADSB_ROOT=$PWD \
    ADSB_DECOMP_DIR=$PWD/data/decomposition_ecac \
    ADSB_PHASE_DIR=$PWD/data/decomposition_ecac_phase \
    ADSB_AIRPORTS_CSV=$PWD/data/airports_ecac.csv \
    ../lab-venv/bin/python lab/phase_report.py
"""

from __future__ import annotations

import csv
import glob
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEC_DIR = Path(os.environ.get("ADSB_DECOMP_DIR") or (ROOT / "data/decomposition"))
PHASE_DIR = Path(os.environ.get("ADSB_PHASE_DIR")
                 or (ROOT / "data/decomposition_phase"))
AIRPORTS = Path(os.environ.get("ADSB_AIRPORTS_CSV") or (ROOT / "data/airports.csv"))

# The site's grid, copied deliberately rather than chosen afresh: a flight is
# compared with flights of comparable length AND type, so what is measured is
# routing and profile and not fleet choice. Using a different grid here would
# produce airport figures that look like the published ones but are not, which
# is worse than obviously different numbers.
BINS = [0, 200, 300, 400, 500, 650, 800, 1000, 1200, 1500, 2000, 3000, 99999]
MIN_N_CELL = 200
MIN_N_AIRPORT = 2000
# A coarser grid, only for the "by distance band" table, where the site's 12
# bins would be unreadable.
BANDS = [0, 300, 500, 800, 1200, 2000, 99999]

sys.path.insert(0, str(ROOT / "lab"))
from phase_attrib import (PHASE_A, PHASE_B, MIN_DEV_PP,       # noqa: E402
                          add_mean_norm, by_airport, headline)

SHORT = {"excess_vert_climb_pct": "salita", "excess_vert_cruise_pct": "crociera",
         "excess_vert_desc_pct": "discesa", "excess_vert_dep_pct": "partenza",
         "excess_vert_enr_pct": "en route", "excess_vert_arr_pct": "arrivo"}


def load() -> pd.DataFrame:
    fd = sorted(glob.glob(str(DEC_DIR / "*.parquet")))
    fp = sorted(glob.glob(str(PHASE_DIR / "*.parquet")))
    if not fd or not fp:
        raise SystemExit(f"mancano parquet: {DEC_DIR} ({len(fd)}) / "
                         f"{PHASE_DIR} ({len(fp)})")
    dec = pd.concat([pq.read_table(f).to_pandas() for f in fd], ignore_index=True)
    ph = pd.concat([pq.read_table(f).to_pandas() for f in fp], ignore_index=True)
    print(f"congelato: {len(dec):,} voli su {dec.day.nunique()} giorni")
    print(f"fasi     : {len(ph):,} voli su {ph.day.nunique()} giorni")
    df = dec.merge(ph, on=["day", "flight_id"], how="inner",
                   validate="one_to_one")
    print(f"uniti    : {len(df):,} ({len(df)/len(dec)*100:.2f}% del congelato)\n")

    df["bin"] = pd.cut(df.gc_km, BINS).astype(str)
    cell = df["bin"] + "|" + df.typecode
    enough = cell.map(cell.value_counts()) >= MIN_N_CELL
    for src in (["excess_total_pct", "excess_lateral_pct",
                 "excess_vertical_pct"] + PHASE_A + PHASE_B):
        med_bin = df["bin"].map(df.groupby("bin")[src].median()).to_numpy()
        med_cell = cell.map(df[enough].groupby(cell[enough])[src].median()).to_numpy()
        ref = np.where(enough.to_numpy() & np.isfinite(med_cell), med_cell, med_bin)
        dst = {"excess_total_pct": "d_tot", "excess_lateral_pct": "d_lat",
               "excess_vertical_pct": "d_vert"}.get(
            src, "d_" + src.replace("excess_", "").replace("_pct", ""))
        df[dst] = df[src].to_numpy() - ref

    # The mean-referenced columns used for the decomposition come from the
    # shared module, which is also what the site sentence reads: two copies of
    # this normalisation would drift, and a figure on the page that no longer
    # matches the report behind it is the worst outcome available here.
    return add_mean_norm(df, BINS, MIN_N_CELL)


def names() -> dict:
    out = {}
    if AIRPORTS.exists():
        with open(AIRPORTS, newline="") as f:
            for r in csv.DictReader(f):
                out[r["icao"]] = r["name"]
    return out


def shares(df, cols, label):
    """Each bucket as a share of the vertical term. Taken on SUMS and not on
    medians: medians of parts do not add up to the median of the whole, and a
    share is only meaningful against a total that the parts actually compose."""
    tot = df.excess_vertical_pct.sum()
    print(f"  {label}")
    for c in cols:
        print(f"    {SHORT[c]:9s}  mediana {df[c].median():+7.2f} pp   "
              f"quota {df[c].sum()/tot*100:+6.1f}%")


def main():
    df = load()

    # ---- gate: nothing published moved, additivity holds -----------------
    print("=" * 72)
    print("CANCELLI")
    print("=" * 72)
    resid_a = (df.excess_vertical_pct - df[PHASE_A].sum(axis=1)).abs()
    resid_b = (df.excess_vertical_pct - df[PHASE_B].sum(axis=1)).abs()
    print(f"  additivita' taglio A (fase)     : max {resid_a.max():.2e} pp")
    print(f"  additivita' taglio B (posizione): max "
          f"{resid_b[df[PHASE_B].notna().all(axis=1)].max():.2e} pp")
    rel = (df.hybrid_co2_rebuilt_kg - df.hybrid_co2_kg).abs() / df.hybrid_co2_kg
    print(f"  ibrido ricostruito == congelato : max err rel {rel.max():.2e}, "
          f"esatti {(rel == 0).mean()*100:.2f}%")
    print(f"  bias di diradamento aggregato   : "
          f"{(df.real_co2_thin_kg.sum()/df.co2_kg_v0.sum()-1)*100:+.3f}%")
    print(f"  taglio B non definito (volo troppo corto): "
          f"{df.excess_vert_dep_pct.isna().mean()*100:.2f}%")
    # MEDIANS, not the site's headline figures: the page quotes the
    # fuel-weighted shares (7.51 / 14.55), a different statistic on the same
    # untouched column. Labelling these "the published figures" would invite
    # the reader to conclude something moved when nothing did — the proof of
    # that is that data/decomposition_ecac is never written to, and rebuilding
    # the site reproduces its headline exactly.
    w = df.ideal_gc_co2_kg
    print(f"  mediane: totale {df.excess_total_pct.median():.2f} · "
          f"laterale {df.excess_lateral_pct.median():.2f} · "
          f"verticale {df.excess_vertical_pct.median():.2f}")
    print(f"  pesate sull'ottimo (come il sito): laterale "
          f"{(df.excess_lateral_pct*w).sum()/w.sum():.2f} · verticale "
          f"{(df.excess_vertical_pct*w).sum()/w.sum():.2f}")

    # ---- headline --------------------------------------------------------
    print("\n" + "=" * 72)
    print("COME SI SPEZZA IL VERTICALE")
    print("=" * 72)
    shares(df, PHASE_A, "TAGLIO A — per fase (confini del profilo nominale)")
    b = df[df[PHASE_B].notna().all(axis=1)]
    shares(b, PHASE_B, "TAGLIO B — per posizione (cilindro 40 NM)")

    print("\n  per fascia di distanza (taglio A). Le MEDIANE in punti sono la")
    print("  lettura affidabile: la quota % divide per un verticale che sulle")
    print("  tratte lunghe e' quasi nullo, e li' esplode senza dire nulla.")
    df["band"] = pd.cut(df.gc_km, BANDS).astype(str)
    g = df.groupby("band", observed=True)
    t = pd.DataFrame({SHORT[c]: g[c].median() for c in PHASE_A})
    t["verticale"] = g.excess_vertical_pct.median()
    for c in PHASE_A:
        t["q_" + SHORT[c][:4]] = g[c].sum() / g.excess_vertical_pct.sum() * 100
    t["n"] = g.size()
    order = list(pd.cut(pd.Series(BANDS[:-1]) + 1, BANDS).astype(str))
    print(t.reindex([o for o in order if o in t.index]).round(1).to_string())

    print("\n  quote per mese (taglio A, su somme) — stabilita' dello split:")
    df["month"] = df.day.str.slice(0, 7)
    g = df.groupby("month")
    t = pd.DataFrame({SHORT[c]: g[c].sum() / g.excess_vertical_pct.sum() * 100
                      for c in PHASE_A})
    t["verticale"] = g.excess_vertical_pct.median()
    t["n"] = g.size()
    print(t.round(1).to_string())

    # ---- gate: the four known airports -----------------------------------
    print("\n" + "=" * 72)
    print("CANCELLO — I QUATTRO CASI NOTI")
    print("=" * 72)
    print("  'al proprio capo' = per le partenze la fase di partenza, per gli")
    print("  arrivi quella di arrivo. Tutto normalizzato per lunghezza e tipo.")
    nm = names()
    dep = df.assign(ap=df.origin_icao, own=df.d_vert_dep, far=df.d_vert_arr,
                    role="dep")
    arr = df.assign(ap=df.dest_icao, own=df.d_vert_arr, far=df.d_vert_dep,
                    role="arr")
    both = pd.concat([dep, arr])

    # Per ROLE, never averaged over the two. Averaging is what hides the case
    # that justifies the whole exercise: an airport can be fine on arrival and
    # bad on departure, and a single number over both roles splits the
    # difference and reports neither.
    gr = both.groupby(["ap", "role"]).agg(
        n=("d_tot", "size"), d_tot=("d_tot", "median"),
        own=("own", "median"), far=("far", "median"),
        enr=("d_vert_enr", "median"), vert=("d_vert", "median"))
    tot_n = both.groupby("ap").size()
    gr = gr[gr.index.get_level_values("ap").map(tot_n) >= MIN_N_AIRPORT]

    hdr = (f"  {'aeroporto':24s} {'ruolo':6s} {'n':>7s} {'Δnorm':>7s} "
           f"{'Δvert':>7s} | {'al capo':>8s} {'altrove':>8s} {'enroute':>8s}")
    print("\n" + hdr)
    print("  " + "-" * (len(hdr) - 2))
    for icao in ("LEBL", "EHAM", "EGSS", "LEMD"):
        for role, lab in (("dep", "parte"), ("arr", "arriva")):
            if (icao, role) not in gr.index:
                print(f"  {icao} {role}: sotto la soglia di campione")
                continue
            r = gr.loc[(icao, role)]
            print(f"  {nm.get(icao, icao)[:24]:24s} {lab:6s} {int(r.n):7,d} "
                  f"{r.d_tot:+7.1f} {r.vert:+7.1f} | {r.own:+8.1f} "
                  f"{r.far:+8.1f} {r.enr:+8.1f}")
        print()

    med = gr.groupby(level="role").median(numeric_only=True)
    print(f"  mediana sui {gr.index.get_level_values('ap').nunique()} "
          f"aeroporti sopra soglia:")
    print(f"    in partenza: al capo {med.loc['dep','own']:+.1f}  altrove "
          f"{med.loc['dep','far']:+.1f}  en route {med.loc['dep','enr']:+.1f}")
    print(f"    in arrivo  : al capo {med.loc['arr','own']:+.1f}  altrove "
          f"{med.loc['arr','far']:+.1f}  en route {med.loc['arr','enr']:+.1f}")

    print("\n  i dieci peggiori AL PROPRIO CAPO, in ciascun ruolo:")
    for role, lab in (("dep", "IN PARTENZA"), ("arr", "IN ARRIVO")):
        s = gr.xs(role, level="role").sort_values("own", ascending=False).head(10)
        print(f"    {lab}")
        for icao, r in s.iterrows():
            print(f"      {nm.get(icao, icao)[:26]:26s} {int(r.n):6,d}  "
                  f"al capo {r.own:+6.1f}  altrove {r.far:+6.1f}  "
                  f"en route {r.enr:+6.1f}  (Δnorm {r.d_tot:+6.1f})")

    # How much of what the site attributes to an airport was produced at its
    # own end: the question the whole phase is for.
    # The medians above are robust and directly comparable with the site's own
    # convention, but medians of parts do not add up to the median of the whole,
    # so they cannot be read as a decomposition. Shares of SUMS can, and they
    # answer the question the phase was opened for.
    print("\n  DECOMPOSIZIONE — normalizzazione sulla MEDIA, che somma esatta.")
    print("  Di tutto lo scarto VERTICALE dalla norma attribuito oggi a un")
    print("  aeroporto, quanto e' stato prodotto al suo capo, quanto all'altro,")
    print("  quanto in crociera lontano da entrambi?")
    dd = df[df.decomposable]
    resid = (dd.m_vert - dd[["m_vert_dep", "m_vert_enr", "m_vert_arr"]]
             .sum(axis=1)).abs().max()
    print(f"  [residuo della decomposizione: {resid:.2e} punti · "
          f"{len(dd):,} voli su {len(df):,}]")
    gs = by_airport(df, MIN_N_AIRPORT).rename(
        columns={"own": "so", "far": "sf", "enr": "se"})
    print(f"\n  {'aeroporto':24s} {'ruolo':6s} {'Δvert(m)':>9s} {'al capo':>9s} "
          f"{'altrove':>9s} {'en route':>9s}")
    for icao in ("LEBL", "EHAM", "EGSS", "LEMD"):
        for role, lab in (("dep", "parte"), ("arr", "arriva")):
            if (icao, role) not in gs.index:
                continue
            r = gs.loc[(icao, role)]
            mean_v = r.sv / r.n
            if abs(r.sv) < 1e-6:
                continue
            print(f"  {nm.get(icao, icao)[:24]:24s} {lab:6s} {mean_v:+9.1f} "
                  f"{r.so/r.sv*100:+8.1f}% {r.sf/r.sv*100:+8.1f}% "
                  f"{r.se/r.sv*100:+8.1f}%")
    print("\n  gli stessi in PUNTI medi, che e' la lettura da usare quando lo")
    print("  scarto complessivo e' piccolo e le quote diventano instabili:")
    for icao in ("LEBL", "EHAM", "EGSS", "LEMD"):
        for role, lab in (("dep", "parte"), ("arr", "arriva")):
            if (icao, role) not in gs.index:
                continue
            r = gs.loc[(icao, role)]
            print(f"  {nm.get(icao, icao)[:24]:24s} {lab:6s} "
                  f"{r.sv/r.n:+9.2f} {r.so/r.n:+8.2f} {r.sf/r.n:+8.2f} "
                  f"{r.se/r.n:+8.2f}")
    # Aggregating by summing every airport's deviation would divide by a total
    # that is nearly zero: a deviation from the norm sums to zero across the
    # whole population by construction, so the positive and negative airports
    # cancel and the ratio becomes noise dressed as a percentage. Instead each
    # airport gets its own share and the MEDIAN of those shares is reported,
    # over the airports whose deviation is large enough for a share to mean
    # something.
    h = headline(by_airport(df, MIN_N_AIRPORT))
    print(f"\n  quote per aeroporto, mediana sugli aeroporti con |Δvert| >= "
          f"{MIN_DEV_PP:.0f} punti ({h['n_airports']} aeroporti sopra soglia):")
    for role, lab in (("dep", "in partenza"), ("arr", "in arrivo")):
        print(f"    {lab} (n={h['n_'+role]}): al capo {h[role+'_own']:+.1f}%  "
              f"altrove {h[role+'_far']:+.1f}%  en route {h[role+'_enr']:+.1f}%")
    print("\n  e la stessa cosa per FASE (taglio A), stesso criterio:")
    for role, lab in (("dep", "in partenza"), ("arr", "in arrivo")):
        print(f"    {lab} (n={h['n_'+role]}): salita {h[role+'_climb']:+.1f}%"
              f"  crociera {h[role+'_cruise']:+.1f}%"
              f"  discesa {h[role+'_desc']:+.1f}%")
    print("\n  ATTENZIONE alla differenza fra le due letture: le quote sul")
    print("  LIVELLO del verticale (piu' sopra) dicono dove sta il carburante;")
    print("  queste quote sullo SCARTO DALLA NORMA dicono che cosa distingue")
    print("  un aeroporto dai suoi pari. Non sono la stessa domanda.")

    # ---- gate: the known floor -------------------------------------------
    print("\n" + "=" * 72)
    print("CANCELLO — IL PAVIMENTO NOTO (diretti, notturni, lunghi)")
    print("=" * 72)
    hour = pd.to_datetime(df.dep_ts, unit="s", utc=True).dt.hour
    m = (df.dist_ratio < 1.02) & hour.isin([1, 2, 3, 4]) & (df.gc_km > 1000)
    f = df[m]
    print(f"  {len(f):,} voli · verticale mediano {f.excess_vertical_pct.median():.1f} "
          f"(atteso 5,5) · flotta {df.excess_vertical_pct.median():.1f}")
    shares(f, PHASE_A, "  composizione del pavimento")

    # ---- external comparisons --------------------------------------------
    print("\n" + "=" * 72)
    print("CONFRONTI ESTERNI")
    print("=" * 72)
    nm_lo, nm_hi = 200 * 1.852, 1500 * 1.852
    p = df[(df.gc_km >= nm_lo) & (df.gc_km <= nm_hi)]
    print(f"  PASUTTO (AIAA 2021), 200-1500 NM, SOLA CROCIERA: 4,6% mediano / "
          f"7,5% medio")
    print(f"    nostro perimetro: {len(p):,} voli")
    print(f"    profilo intero, verticale : "
          f"{p.excess_vertical_pct.median():+.2f} mediano / "
          f"{p.excess_vertical_pct.mean():+.2f} medio")
    print(f"    SOLA CROCIERA (taglio A)  : "
          f"{p.excess_vert_cruise_pct.median():+.2f} mediano / "
          f"{p.excess_vert_cruise_pct.mean():+.2f} medio")
    print(f"    (per riferimento, excess TOTALE su questo perimetro: "
          f"{p.excess_total_pct.median():.2f})")

    print(f"\n  ALCABIN (AIAA 2009): 80% dell'eccesso verticale in discesa/arrivo")
    tot = df.excess_vertical_pct.sum()
    print(f"    nostro: discesa {df.excess_vert_desc_pct.sum()/tot*100:.1f}% "
          f"(taglio A) · arrivo "
          f"{b.excess_vert_arr_pct.sum()/b.excess_vertical_pct.sum()*100:.1f}% "
          f"(taglio B)")

    print(f"\n  CCO/CDO EUROCONTROL: ~4,3 kg per partenza contro ~35 kg per")
    print(f"  arrivo, cioe' l'arrivo vale ~8x la partenza.")
    dep_s = b.excess_vert_dep_pct.sum()
    arr_s = b.excess_vert_arr_pct.sum()
    print(f"    nostro rapporto arrivo/partenza (taglio B): "
          f"{arr_s/dep_s:.2f}x")
    print(f"    nostro rapporto discesa/salita  (taglio A): "
          f"{df.excess_vert_desc_pct.sum()/df.excess_vert_climb_pct.sum():.2f}x")

    # ---- diagnostics ------------------------------------------------------
    print("\n" + "=" * 72)
    print("DIAGNOSTICA — dove sta il volo reale rispetto al nominale")
    print("=" * 72)
    print("  frazione di percorso, mediane:")
    for c, lab in (("nom_climb_frac", "fine salita nominale"),
                   ("real_toc_frac", "cima salita REALE"),
                   ("nom_desc_frac", "inizio discesa nominale"),
                   ("real_tod_frac", "inizio discesa REALE"),
                   ("tma_dep_frac", "uscita cilindro partenza"),
                   ("tma_arr_frac", "rientro cilindro arrivo")):
        print(f"    {lab:28s} {df[c].median():.3f}")
    print(f"\n  cima salita reale OLTRE quella nominale: "
          f"{(df.real_toc_frac > df.nom_climb_frac).mean()*100:.1f}% dei voli")
    print(f"  discesa reale PRIMA di quella nominale : "
          f"{(df.real_tod_frac < df.nom_desc_frac).mean()*100:.1f}% dei voli")


if __name__ == "__main__":
    main()
