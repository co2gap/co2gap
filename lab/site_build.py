#!/usr/bin/env python3
"""
Build the public page from the ALREADY COMPUTED decomposition.

Why this exists next to lab/report.py: report.py recomputes everything from
scratch — it reloads every flight, rebuilds the ERA5 wind field and redoes the
per-flight excess. On the ECAC box that is an hour of work and several GB of
RAM, to recompute numbers that data/decomposition_ecac already holds for all
197 days in 220 MB. This reads those and aggregates, which takes seconds and is
reproducible from a committed artefact.

It also publishes what report.py structurally could not: the lateral/vertical
decomposition, which is the part of this work that is actually distinctive.

    ADSB_DECOMP_DIR=... ADSB_AIRPORTS_CSV=... ADSB_CALIB=... \
        lab-venv/bin/python lab/site_build.py

Nothing here deploys. Publication is an explicit decision.
"""

from __future__ import annotations

import csv
import glob
import html
import json
import os
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
DEC_DIR = Path(os.environ.get("ADSB_DECOMP_DIR") or (ROOT / "data/decomposition"))
CALIB = Path(os.environ.get("ADSB_CALIB") or (ROOT / "data/calibration.json"))
AIRPORTS = Path(os.environ.get("ADSB_AIRPORTS_CSV") or (ROOT / "data/airports.csv"))
OUT = Path(os.environ.get("ADSB_SITE_OUT") or (ROOT / "site/index.html"))
OUT_METH = OUT.parent / "methodology.html"
COVERAGE = Path(os.environ.get("ADSB_COVERAGE_JSON") or (ROOT / "data/coverage.json"))
# Optional. When the phase split has been produced, the airport note can say
# WHERE in the flight the gap happened instead of admitting it cannot. Absent,
# the page falls back to the earlier wording, so building the site never depends
# on a step that may not have run.
PHASE_DIR = Path(os.environ.get("ADSB_PHASE_DIR") or (ROOT / "data/decomposition_phase"))
# Quota di carburante bruciata A TERRA, per volo. Corregge il difetto trovato il
# 2026-08-28: il consumo reale era integrato gate-to-gate e i punti a terra
# venivano prezzati da FuelFlow.enroute, un modello aerodinamico di volo, che a
# 20-60 kt restituisce ~7.750 kg/h contro i ~700 di un rullaggio reale. La
# baseline ideale non rulla (parte da 250 kt), quindi il numeratore conteneva un
# termine che il denominatore non aveva. Vale ~8% del carburante modellato.
GROUND_DIR = Path(os.environ.get("ADSB_GROUND_DIR") or (ROOT / "data/ground_share_ecac"))
# Definizione di "a terra". a3000t70 e' la scelta motivata: 3000 ft e' la stessa
# soglia che trajectories.py usa gia' (GROUND_ALT_FT) e copre gli aeroporti in
# quota anche quando manca il flag di superficie; sotto 70 kt nessun velivolo
# della flotta modellata e' in volo. La sensibilita' rispetto a "suolo" (il solo
# flag del transponder) vale ~1 punto sulla cifra di testa, ed e' dichiarata.
GROUND_DEF = os.environ.get("ADSB_GROUND_DEF", "a3000t70")


def pct0(v) -> str:
    """Percentuale intera col segno, senza «-0%» e senza «+-0%».

    Sotto il mezzo punto il segno non e' informazione: e' un artefatto di
    stampa. E «-0%» accanto a un gap contro un ottimo irraggiungibile dice al
    lettore ostile che quei voli battono l'ottimo, che e' l'opposto di cio' che
    il numero misura. Il grafico per banda di distanza stampava perfino
    «+-0%», perche' il segno era scritto a mano davanti a un formato che gia'
    ne produce uno.
    """
    return "0%" if abs(v) < 0.5 else f"{v:+.0f}%"


def phase_attribution(df):
    """Where the vertical gap of an airport was produced, or None.

    Numbers derived in code, never typed: this project's rule, and the reason
    the finding paragraphs elsewhere on the page read their own figures out of
    the data. The computation itself lives in lab/phase_attrib.py, shared with
    lab/phase_report.py, so the sentence on the page cannot drift away from the
    report it came from.
    """
    sys.path.insert(0, str(ROOT / "lab"))
    try:
        from phase_attrib import load_phase, add_mean_norm, by_airport, headline
    except Exception:
        return None
    ph = load_phase(PHASE_DIR)
    if ph is None:
        return None
    # A partial phase run would describe a different population from the one
    # the table shows, so it is refused rather than quietly averaged in.
    if set(ph.day.unique()) != set(df.day.unique()):
        print(f"phase split covers {ph.day.nunique()} days against "
              f"{df.day.nunique()} published: attribution note omitted")
        return None
    m = df.merge(ph, on=["day", "flight_id"], how="inner", validate="one_to_one")

    # ---- il rullaggio esce anche dalle FASI -------------------------------
    # Le colonne di fase sono congelate e ricostruiscono il verticale
    # GATE-TO-GATE; il verticale che arriva da df e' gia' corretto. Dividere le
    # prime per il secondo dava quote oltre il 100% (231% entro 40 NM, 245% in
    # discesa, pubblicate il 2026-08-28). Il burn di terra sta nei secchielli
    # che lo contengono: la TMA del capo dov'e' avvenuto, e la salita o la
    # discesa della partizione per fase, perche' il rullaggio cade a frazione
    # d'arco ~0 e ~1 e i confini di CUT A vengono dal nominale, che parte in
    # salita e finisce in discesa. Crociera ed en-route non lo vedono mai.
    m["excess_vert_dep_pct"] = m.excess_vert_dep_pct - m.ground_pct_dep
    m["excess_vert_arr_pct"] = m.excess_vert_arr_pct - m.ground_pct_arr
    m["excess_vert_climb_pct"] = m.excess_vert_climb_pct - m.ground_pct_dep
    m["excess_vert_desc_pct"] = m.excess_vert_desc_pct - m.ground_pct_arr

    # La prova che le due partizioni e il totale stanno sulla stessa base. E' il
    # controllo che avrebbe fatto fallire il build invece di lasciarlo stampare
    # 245%: additive per costruzione, quindi il residuo e' epsilon o e' un bug.
    for cols, nome in ((["excess_vert_dep_pct", "excess_vert_enr_pct",
                         "excess_vert_arr_pct"], "posizione"),
                       (["excess_vert_climb_pct", "excess_vert_cruise_pct",
                         "excess_vert_desc_pct"], "fase")):
        ok = m[cols + ["excess_vertical_pct"]].notna().all(axis=1)
        res = float((m.loc[ok, cols].sum(axis=1)
                     - m.loc[ok, "excess_vertical_pct"]).abs().max())
        if res > 1e-6:
            raise SystemExit(
                f"lo split per {nome} non somma al verticale pubblicato "
                f"(residuo massimo {res:.4f} punti). Numeratore e denominatore "
                f"stanno su basi diverse: le colonne di fase sono gate-to-gate "
                f"e il verticale e' corretto per il rullaggio, oppure "
                f"{PHASE_DIR} e' stato rigenerato con altre convenzioni.")

    return headline(by_airport(add_mean_norm(m, BINS, MIN_N_CELL),
                               MIN_N_AIRPORT))


def convention_medians(df):
    """The two decomposition conventions, for the median flight.

    Convention A is the one this site publishes: the route is corrected first
    and the extra kilometres are charged at the IDEAL profile. Convention B
    charges them at the flight's own CO2 per kilometre, which moves weight
    towards the lateral term. The total is identical under both by construction,
    so only the split moves.

    Derived here rather than typed. A sentence whose whole job is to show how
    sensitive the split is to a modelling choice is the last place in the site
    where a number should be allowed to go stale without anything failing.
    """
    lat_b = (df.co2_kg_v0 / df.ideal_gc_co2_kg) * (1 - df.gc_km / df.flown_km) * 100
    vert_b = df.excess_total_pct - lat_b
    return (float(df.excess_lateral_pct.median()), float(df.excess_vertical_pct.median()),
            float(lat_b.median()), float(vert_b.median()))


def coverage_note(days) -> str:
    """The days inside the published period whose source data is incomplete.

    Generated from lab/coverage_audit.py rather than written by hand, so that it
    cannot drift away from the data the way a typed sentence would. A day is
    listed only when whole hours are missing from the source dump — a defect
    that passes every check in the pipeline, because the file downloads
    cleanly, reads to the last byte and parses.
    """
    if not COVERAGE.exists():
        return ""
    try:
        cov = json.loads(COVERAGE.read_text())
    except Exception:
        return ""
    lo, hi = days[0], days[-1]
    inside = [d for d in cov.get("days", [])
              if d.get("status") == "incomplete" and lo <= d["day"] <= hi]
    if not inside:
        return ("<li>Every day in the period was checked for whole hours missing "
                "from the source data; <b>none were found</b>.</li>")
    lost = sum(d.get("flights_missing_estimate", 0) for d in inside)
    shutil.copyfile(COVERAGE, OUT.parent / "coverage.json")   # so the link resolves
    n = len(inside)
    subject = "One day" if n == 1 else f"{n} days"
    verb = "is" if n == 1 else "are"
    items = "; ".join(
        f"{esc(d['day'])} (hours {', '.join(f'{h:02d}' for h in d['missing_hours_utc'])} UTC)"
        for d in inside)
    return (f"<li><b>{subject} in the period {verb} incomplete at the source</b>, "
            f"with whole hours absent from the dump: {items}. An estimated "
            f"{lost:,} flights are missing as a result. Such days are kept and "
            f"labelled rather than removed, and the complete record is published "
            f'as <a href="coverage.json">coverage.json</a>.</li>')

# Release identity. A figure on this site is only citable if the reader can say
# WHICH version produced it, so the release name, the methodology version and the
# covered period are printed on both pages and must be bumped together with the
# Zenodo release. "generated" (a timestamp) is not a version: regenerating the
# page without new data must not look like a new release.
RELEASE = "2026-09-01"
METHOD_VERSION = "1.0"
# Il DOI del concetto Zenodo, che risolve sempre all'ultima versione. Resta None
# finche' il primo archivio non esiste: la pagina si adatta e NON promette un
# identificativo che non c'e'. La metodologia dice che ogni release e' archiviata
# con un DOI, ed e' l'unica frase del sito che dipende da qualcosa fuori da qui.
ZENODO_DOI = None
# Va scritto NUDO — "10.5281/zenodo.1234567" — perche' le tre interpolazioni piu'
# sotto lo infilano dentro un href che porta gia' il prefisso. Zenodo pero' lo
# mostra anche come URL intero, ed e' quella la forma che finisce negli appunti:
# incollata qui produce href="https://doi.org/https://doi.org/10.5281/..." in
# metodologia, releases e feed, cioe' tre link morti e nessun errore. Nemmeno
# freeze_check se ne accorge — non guarda releases.html, e la frase che il DOI
# fa sparire non e' nella fotografia. Misurato il 25/08 in una prova a secco:
# senza questa riga l'unico errore realistico del giorno del lancio passa
# inosservato dentro la finestra di un'ora.
if ZENODO_DOI is not None and not re.fullmatch(r"10\.\d{4,9}/\S+", ZENODO_DOI):
    raise SystemExit(
        f"ZENODO_DOI={ZENODO_DOI!r} non e' un DOI: serve la forma nuda "
        "10.xxxx/suffisso, senza 'https://doi.org/' e senza 'doi:'")
# Cosa e' cambiato fra una versione e l'altra. Esiste perche' la release del
# 31/01/2027 muove DUE cose insieme — la finestra passa da 197 giorni a 12 mesi,
# e se si corregge optimal_cruise_alt_ft anche la baseline — e senza un posto in
# cui sia scritto, il lettore leggera' quel movimento come "l'Europa ha volato
# peggio". Il posto va costruito prima che serva, non dopo.
RELEASES = [
    dict(date=RELEASE, version=METHOD_VERSION, doi=ZENODO_DOI, published=True,
         what="First release. The gap against an ideal flight, computed for every "
              "flight that passes the quality gates in the ECAC area over the covered "
              "period, split into a lateral "
              "and a vertical component, with the phase attribution of the vertical "
              "part and a context page for the external figures."),
]
PLANNED = dict(
    date="2027-01-31", window="the whole of 2026, twelve months",
    what=("<b>Two things move at once, and neither is Europe flying worse.</b> "
          "The window changes from the {days} days of this release to twelve "
          "months, so every headline figure shifts for reasons of perimeter. And "
          "the cruise baseline is under revision: measured on its own, the cruise "
          "segment currently comes out <i>negative</i>, meaning real aircraft burn "
          "less than what this model calls optimal — a baseline that is too "
          "generous. Correcting it makes the ideal flight cheaper and therefore "
          "the gap <i>larger</i>. Both changes push the headline in the same "
          "direction, and a reader who is not told will read the sum as a "
          "deterioration."))
# Releases are twice a year, at the end of January and the end of July, and
# each one carries 12 MONTHS — not the calendar half it follows. January carries
# the calendar year just ended (so it doubles as "the 2026 figures", which is
# the form a citation takes); July carries the 12 months to the end of June.
# The window matters more than the date: this site's own figures are seasonal
# (an airport's margin moves between its strongest and weakest month), so
# consecutive releases covering Jan-Jun and then Jul-Dec would differ for
# reasons of season and be read as a change in efficiency. A rolling 12-month
# window contains every season, which is also why EUROCONTROL computes KEA over
# a rolling 12 months. The cost, stated on the page: two consecutive releases
# share six months of data, so movements between them are damped.
# End of the month, not the 1st: ERA5T lags ~5 days and the pipeline needs a
# few more, so the margin is taken once instead of chased twice a year.
# The cadence stays deliberately slower than the data: month-to-month rank
# correlation is 0.92 (lab/stability.py), so republishing a structurally stable
# signal quarterly would present noise as news.
NEXT_RELEASE = "31 January 2027"
# The window of the NEXT release, named on the page. It is not derivable from
# the date: January names a calendar year, July names a straddling 12 months.
NEXT_WINDOW = "the whole of 2026"

# Results produced by other steps of the pipeline and quoted on the methodology
# page. They are constants here because they come from runs this script does not
# perform; each is reproducible with the command named beside it.
GATE = {"January": (8.6, 5.1, 1798), "February": (9.9, 5.2, 1781),
        "July": (8.5, 4.6, 2458)}          # lab/gate.py
STAB = {"pairs": 21, "median": 0.867, "worst": 0.789,
        "worst_pair": "Feb→Jul", "consec": 0.924}   # lab/stability.py
# Verified against primary sources on 2026-07-27, see reports/.
BENCH = {"cco_cdo_kg": 39, "cco_cdo_pct": 1.1,
         "pasutto_pct": 4.6, "pasutto_kg": 60, "pasutto_avg_pct": 7.5,
         "pasutto_avg_kg": 85, "kea_published": 3.0,
         # RIMOSSO il 2026-08-28: "alcabin_desc_pct": 80. Il numero veniva da
         # una bibliografia altrui (rif. [14] in reports/), non dal paper, che
         # e' a pagamento (AIAA, 10.2514/6.2009-6959) e non e' mai stato letto.
         # In piu' il sito lo accostava al nostro 90% (quota della discesa sulla
         # deviazione in ARRIVO) mentre la cifra confrontabile e' 75,7%
         # (reports/fase3.md), che non pubblichiamo. Sostituito con il rapporto
         # CDO:CCO ~10:1 di EUROCONTROL, verificato alla fonte e citato in forma
         # direzionale, non numerica.
         # Quota massima realmente raggiunta contro quella che il baseline chiede,
         # per fascia di distanza. Riproducibile con:
         #   PYTHONPATH=pipeline:ingest lab-venv/bin/python lab/altitude_check.py
         # 51 giorni campionati, 558.883 voli. Serve a due cose opposte: mostrare
         # che sulle tratte corte il riferimento NON pretende l'infattibile, e
         # ammettere dove sbaglia sulle lunghe.
         "alt_short_below_ft": 2000, "alt_long_below_ft": 1000,
         # GAIA, l'inventario globale di emissioni da ADS-B piu' vicino a questo
         # lavoro: ACP 24, 725 (2024), UCL, peer-reviewed, dati su Zenodo.
         # Citarlo non ci indebolisce, ci colloca: risponde a una domanda diversa.
         "gaia_flights_m": 103.7, "gaia_year": 2024,
         # SES performance scheme, reference period 4. These are TARGETS, not
         # measurements: the binding Union-wide values Member States are held
         # to, which measured performance has been exceeding. Kept distinct
         # from kea_published above, which is the measured order of magnitude —
         # comparing our measurement against a target would be a category error.
         "kea_rp4_start": 2.80, "kea_rp4_end": 2.66}

# Never aggregate below this. The published product is always aggregate: no row
# of this page may describe an individual flight or aircraft.
MIN_N = 10
# ...but MIN_N is a privacy floor, NOT a statistical sufficiency threshold, and
# the two must not be confused. At n=10 the head of the ranking fills up with
# 30-flight routes showing +200 point deviations that are sampling noise: the
# first draft of this page led with "Isle of Man <-> Stansted, 28 flights,
# +220". Rankings therefore require n>=100, the same threshold used for the
# magnitude criterion in the phase-2b report.
RANK_MIN_N = 100
# ~10 movements a day over the published period. At 200 the ranking — and worse,
# the prose above it — filled with 260-flight airports presented as "Europe's
# largest gap": the first draft of the findings section led with Guernsey.
MIN_N_AIRPORT = 2000
# Minimum flights in a (distance band x aircraft type) cell for that cell to be
# used as the norm; below it the distance-only norm is used instead.
MIN_N_CELL = 200
# Floor for the closed-airspace claim in the findings section.
MIN_N_CLOSED = 30

# Tipi NON di linea presenti nel dataset: aviazione d'affari. Sono lo 0,16% dei
# voli e il modello di prestazioni ne copre solo due, quindi la loro presenza e'
# gia' accidentale — ma su una rotta sottile una riga fatta di questi voli puo'
# descrivere UNO O DUE aeromobili, cioe' un operatore o un proprietario.
#
# La soglia di pubblicazione conta i VOLI, non gli aeromobili, e non possiamo
# contare gli aeromobili perche' per scelta non conserviamo `icao24`: la
# garanzia strutturale sulla privacy toglie proprio la verifica che
# dimostrerebbe sicure quelle righe. L'unica leva rimasta e' escludere la classe
# dove il rischio si concentra.
#
# Si escludono le ROTTE a maggioranza non di linea, non i voli dagli aggregati:
# lo 0,16% non e' un rischio di privacy quando e' diluito in una somma europea,
# e togliere voli muoverebbe cifre gia' comunicate a chi e' stato preavvisato.
NON_AIRLINER = {"GLF6", "C550"}
NON_AIRLINER_MAX = 0.5

# Fine distance bins for the norm. The raw excess correlates about -0.74 with
# distance, so a raw ranking sorts by shortness, not by inefficiency; every
# ranking below is on the deviation from the median of flights of comparable
# length.
BINS = [0, 200, 300, 400, 500, 650, 800, 1000, 1200, 1500, 2000, 3000, 99999]

# Great-circle corridors that cross airspace closed or systematically avoided.
# Verified geometrically against the flown great circle and, for the legal
# basis, against the EASA CZIB list (see reports/spazi_aerei_chiusi_verificato.md).
CLOSED = {
    "Kaliningrad": (54.3, 55.3, 19.6, 22.9),
    "Bielorussia": (51.2, 56.2, 23.2, 32.8),
    "Ucraina": (44.3, 52.4, 22.1, 40.2),
}


def esc(s):
    return html.escape(str(s))


def vertical_kg_per_flight(df) -> float:
    """Carburante per volo del solo gap VERTICALE, in kg.

    real - hybrid: l'ibrido e' la traccia reale col profilo ottimo, quindi la
    differenza esclude il laterale per costruzione. E' la grandezza da mettere
    accanto ai ~39 kg di CCO/CDO, che sono anch'essi salita e discesa: il totale
    (~788 kg) includerebbe la lunghezza della rotta e il confronto sarebbe fra
    cose diverse.

    Calibrata, come ogni massa assoluta di questo sito e del rapporto 2b: le
    percentuali sono invarianti alla calibrazione, i chilogrammi no, e i due
    artefatti non devono citare cifre diverse per la stessa quantita'.

    Sta in una funzione perche' la usano DUE pagine -- la tabella dei benchmark
    in metodologia e la frase-scudo in home. Nella home era scritta a mano come
    "520 kg di gap totale": sbagliata di etichetta (e' il verticale) e non
    derivata, cioe' l'unica cifra del sito fuori dalla regola del file. Le due
    cose sono legate: una cifra digitata non ha un nome che la corregge.
    """
    return (df.co2_real_kg.sum() - df.co2_hybrid_kg.sum()) / 3.16 / len(df)


def total_kg_per_flight(df) -> float:
    """Carburante per volo del gap TOTALE, in kg: verticale + laterale.

    Sta qui accanto al verticale perche' la coppia si legge insieme e perche' e'
    la divisione che un lettore fa da solo: 4,57 Mt / 1.833.127 voli / 3,16 da'
    questo numero, e chi la fa deve ritrovarlo scritto invece di scoprire uno
    scarto contro i ~521 kg del verticale. Derivata, mai digitata: il ~520
    scritto a mano nell'hero era etichettato "total" ed era il verticale.
    """
    return (df.co2_real_kg.sum() - df.co2_ideal_kg.sum()) / 3.16 / len(df)


def load() -> pd.DataFrame:
    files = sorted(glob.glob(str(DEC_DIR / "*.parquet")))
    if not files:
        raise SystemExit(f"nessun parquet in {DEC_DIR}")
    df = pd.concat([pq.read_table(f).to_pandas() for f in files], ignore_index=True)
    calib = {}
    if CALIB.exists():
        c = json.loads(CALIB.read_text())
        calib = c.get("factors", c) if isinstance(c, dict) else {}
    k = df.typecode.map(lambda t: calib.get(t, 1.0)).astype(float).to_numpy()

    # ---- correzione del carburante bruciato a terra -----------------------
    # Il gap confronta volo con volo: la baseline ideale non rulla, quindi il
    # consumo reale non deve rullare. FALLISCE RUMOROSAMENTE se le quote non
    # ci sono: due variabili di questa pipeline gia' falliscono in silenzio e
    # il runbook le documenta, non se ne aggiunge una terza.
    gfiles = sorted(glob.glob(str(GROUND_DIR / "*.parquet")))
    if not gfiles:
        raise SystemExit(
            f"nessuna quota di terra in {GROUND_DIR}. Senza, il gap conterrebbe "
            f"il rullaggio prezzato a ~7.750 kg/h: e' il difetto corretto il "
            f"2026-08-28. Genera con lab/ground_share.py oppure passa "
            f"ADSB_GROUND_DIR.")
    g = pd.concat([pq.read_table(f).to_pandas() for f in gfiles], ignore_index=True)
    col = f"fuel_{GROUND_DEF}_kg"
    if col not in g.columns:
        raise SystemExit(f"{GROUND_DIR} non contiene {col}: definizione "
                         f"ADSB_GROUND_DEF={GROUND_DEF!r} non disponibile")
    # La quota si tiene anche SPEZZATA fra i due capi: serve allo split per
    # fase, dove il burn di terra va sottratto al secchiello che lo contiene e
    # non al totale. Senza, il numeratore resterebbe gate-to-gate sopra un
    # denominatore corretto e le quote uscirebbero dal 100%.
    for suf in ("", "_dep", "_arr"):
        c = f"fuel_{GROUND_DEF}{suf}_kg"
        if c not in g.columns:
            raise SystemExit(f"{GROUND_DIR} non contiene {c}")
        g["share_ground" + suf] = np.where(g.fuel_recomputed_kg > 0,
                                           g[c] / g.fuel_recomputed_kg, 0.0)
    # Un GIORNO senza quota entrerebbe nelle cifre con share 0, cioe' col
    # rullaggio dentro, e il fillna lo renderebbe invisibile: e' successo con il
    # 2026-02-14. Il buco di un giorno intero e' un errore, non una lacuna.
    manca = sorted(set(df.day.unique()) - set(g.day.unique()))
    if manca:
        raise SystemExit(
            f"quota di terra assente per {len(manca)} giorni pubblicati "
            f"({manca[:3]}...): quei voli entrerebbero gate-to-gate. "
            f"Genera con lab/ground_share.py --days.")
    n_before = len(df)
    df = df.merge(g[["day", "flight_id", "share_ground",
                     "share_ground_dep", "share_ground_arr"]],
                  on=["day", "flight_id"], how="left")
    assert len(df) == n_before, "il merge ha duplicato: flight_id e' un surrogato PER GIORNO"
    cov = df.share_ground.notna().mean()
    for c in ("share_ground", "share_ground_dep", "share_ground_arr"):
        df[c] = df[c].fillna(0.0)
    print(f"  correzione terra [{GROUND_DEF}]: {cov*100:.1f}% dei voli, "
          f"{df.share_ground.mean()*100:.2f}% del carburante escluso dal gap")

    # co2_kg_v0 is UNCALIBRATED. Percentages are calibration-invariant (the
    # factor multiplies real and ideal alike and cancels), tonnages are not.
    # La correzione va applicata alla colonna GREZZA, non solo a quella
    # calibrata: le percentuali (lat/vert/totale), le colonne excess_*_pct e la
    # deviazione per aeroporto derivano tutte da co2_kg_v0. Correggerne una sola
    # lascia il sito in uno stato incoerente che non fallisce, stampa e mente.
    df["co2_gate_to_gate_kg"] = df.co2_kg_v0.to_numpy()      # conservata: inventario
    df["co2_kg_v0"] = df.co2_kg_v0.to_numpy() * (1 - df.share_ground.to_numpy())
    # le percentuali precalcolate nel parquet sono ora obsolete: si rifanno qui,
    # con le stesse formule di pipeline/decompose.py:376-378
    _id = df.ideal_gc_co2_kg.to_numpy()
    df["excess_total_pct"] = (df.co2_kg_v0.to_numpy() - _id) / _id * 100.0
    df["excess_lateral_pct"] = (df.hybrid_co2_kg.to_numpy() - _id) / _id * 100.0
    df["excess_vertical_pct"] = (df.co2_kg_v0.to_numpy() - df.hybrid_co2_kg.to_numpy()) / _id * 100.0

    # Il burn di terra in PUNTI dell'ideale, per capo: e' l'unita' delle colonne
    # di fase, che sono anch'esse percentuali di ideal_gc_co2_kg. Si sottrae
    # cosi' dai secchielli in phase_attribution().
    _gg = df.co2_gate_to_gate_kg.to_numpy()
    df["ground_pct_dep"] = _gg * df.share_ground_dep.to_numpy() / _id * 100.0
    df["ground_pct_arr"] = _gg * df.share_ground_arr.to_numpy() / _id * 100.0

    df["co2_ground_kg"] = df.co2_gate_to_gate_kg.to_numpy() * df.share_ground.to_numpy() * k
    df["co2_real_kg"] = df.co2_kg_v0.to_numpy() * k
    df["co2_ideal_kg"] = df.ideal_gc_co2_kg.to_numpy() * k
    df["co2_hybrid_kg"] = df.hybrid_co2_kg.to_numpy() * k
    df["excess_kg"] = df.co2_real_kg - df.co2_ideal_kg
    df["bin"] = pd.cut(df.gc_km, BINS).astype(str)
    # The norm is per distance AND aircraft type. Distance alone leaves a real
    # confounder: an A320 and a B767 on the same sector are not comparable, so
    # part of what a distance-only norm charges to the route is really the type
    # flying it. Since the question here is routing and profile efficiency and
    # not fleet choice, the type has to be normalised out.
    # Cells thinner than this fall back to the distance-only norm, so a rare
    # type is never ranked against a handful of its own flights.
    cell = df["bin"] + "|" + df.typecode
    enough = cell.map(cell.value_counts()) >= MIN_N_CELL
    for src, dst in (("excess_total_pct", "d_tot"),
                     ("excess_lateral_pct", "d_lat"),
                     ("excess_vertical_pct", "d_vert")):
        med_bin = df["bin"].map(df.groupby("bin")[src].median()).to_numpy()
        med_cell = cell.map(df[enough].groupby(cell[enough])[src].median()).to_numpy()
        ref = np.where(enough.to_numpy() & np.isfinite(med_cell), med_cell, med_bin)
        df[dst] = df[src].to_numpy() - ref
    return df


def airport_names() -> dict:
    names = {}
    if AIRPORTS.exists():
        with open(AIRPORTS, newline="") as f:
            for r in csv.DictReader(f):
                names[r["icao"]] = r["name"]
    return names


def gc_crosses_closed(a, b, coords, n=40) -> list:
    if a not in coords or b not in coords:
        return []
    la1, lo1 = np.radians(coords[a])
    la2, lo2 = np.radians(coords[b])
    d = 2 * np.arcsin(np.sqrt(np.sin((la2 - la1) / 2) ** 2
                              + np.cos(la1) * np.cos(la2) * np.sin((lo2 - lo1) / 2) ** 2))
    if d == 0:
        return []
    f = np.linspace(0, 1, n)
    A, B = np.sin((1 - f) * d) / np.sin(d), np.sin(f * d) / np.sin(d)
    x = A * np.cos(la1) * np.cos(lo1) + B * np.cos(la2) * np.cos(lo2)
    y = A * np.cos(la1) * np.sin(lo1) + B * np.cos(la2) * np.sin(lo2)
    z = A * np.sin(la1) + B * np.sin(la2)
    lat = np.degrees(np.arctan2(z, np.hypot(x, y)))
    lon = np.degrees(np.arctan2(y, x))
    return [name for name, (s, nn, w, e) in CLOSED.items()
            if ((lat >= s) & (lat <= nn) & (lon >= w) & (lon <= e)).any()]


SITE_URL = "https://co2gap.org"

# rel="me" is what makes Mastodon show the profile link as verified: the profile
# points at this site, this site points back, and the pair proves the same
# person controls both. It only works on the exact URL the profile links to, so
# these links belong in the footer of the page served at the domain root.
# Bluesky needs none of this — the handle *is* the domain — but the link is
# listed for symmetry.
# LinkedIn carries no rel="me" — it does not support the mechanism — so it is a
# plain link, listed because it is where an organisation looks for someone to
# talk to.
SOCIAL = (
    '<a rel="me" href="https://mastodon.social/@co2gap">Mastodon</a> · '
    '<a rel="me" href="https://bsky.app/profile/co2gap.org">Bluesky</a> · '
    '<a href="https://www.linkedin.com/company/co2gap/">LinkedIn</a> · '
    '<a href="https://github.com/co2gap/co2gap">source</a>'
)


OG_ALT = "co2gap"   # ricomposto in main() con le cifre correnti: era
                    # cablato con 25,4 Mt e 4,57 Mt, sopravvissuti a ogni
                    # rigenerazione perche' il cancello non guarda le meta.


def meta(title, desc, page=""):
    """Head tags for link previews and icons.

    Without these, every link shared to Bluesky, Mastodon, LinkedIn or Slack
    renders as a bare URL. The preview is what most people see, since most
    people do not click, so the caveat travels inside the image itself
    (site/og.png) and inside the description below — never the headline
    percentage on its own.
    """
    url = f"{SITE_URL}/{page}"
    return f"""<meta name=description content="{esc(desc)}">
<link rel=canonical href="{url}">
<meta property=og:type content=website>
<meta property=og:site_name content=co2gap>
<meta property=og:url content="{url}">
<meta property=og:title content="{esc(title)}">
<meta property=og:description content="{esc(desc)}">
<meta property=og:image content="{SITE_URL}/og.png">
<meta property=og:image:width content=1200>
<meta property=og:image:height content=630>
<meta property=og:image:alt content="{esc(OG_ALT)}">
<meta name=twitter:card content=summary_large_image>
<meta name=twitter:title content="{esc(title)}">
<meta name=twitter:description content="{esc(desc)}">
<meta name=twitter:image content="{SITE_URL}/og.png">
<link rel=alternate type="application/atom+xml" title="co2gap releases" \
href="{SITE_URL}/feed.xml">
<link rel=icon href=favicon.svg type="image/svg+xml">
<link rel=icon href=favicon-32.png sizes=32x32>
<link rel=apple-touch-icon href=apple-touch-icon.png>"""


DESC_INDEX = (
    "CO2 and flight inefficiency in Europe, computed from the ADS-B trajectory "
    "of 1,833,127 flights against a wind-corrected great-circle baseline. "
    "It measures the distance from a theoretical optimum, not recoverable fuel. "
    "Open method, open code, aggregate data only."
)
DESC_METHOD = (
    "How co2gap computes emissions and the gap against an ideal flight: "
    "trajectory processing, OpenAP fuel model, wind-corrected baseline, "
    "lateral/vertical decomposition, and the limits of what the figures mean."
)
DESC_FAQ = (
    "Straight answers to the questions this site invites: whether it says fuel is "
    "being wasted, what an airport's number does and does not mean, who pays for "
    "it, what it leaves out, and how to reuse the figures."
)
DESC_DATA = (
    "Every route, airport and distance band behind the co2gap figures, "
    "searchable by airport name or ICAO code. Aggregate data only."
)

STYLE = """
@font-face{font-family:Inter;src:url(inter.woff2) format('woff2');
font-weight:100 900;font-style:normal;font-display:swap}
:root{--bg:#0e1216;--card:#161d23;--fg:#e8eef3;--mut:#8ea3b2;--line:#243039;
--pos:#ff8a6b;--neg:#5fd0a8;--hi:#5ac8fa;--warn:#f0b429}
@media(prefers-color-scheme:light){:root{--bg:#fbfcfd;--card:#fff;--fg:#16212b;
--mut:#5b6b78;--line:#e2e8ee;--pos:#c2410c;--neg:#0f766e}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.65 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:36px 20px 90px}
.top{position:sticky;top:0;z-index:9;background:var(--bg);
border-bottom:1px solid var(--line)}
.top .wrap{display:flex;align-items:center;gap:26px;height:58px;
max-width:760px;padding:0 20px}
.brand{display:flex;align-items:center;gap:9px;font-weight:640;letter-spacing:-.01em;
text-decoration:none;color:var(--fg);margin-right:auto;font-size:1.02rem}
.brand svg{width:26px;height:26px;display:block}
.top nav{display:flex;gap:20px}
.top nav a{color:var(--mut);text-decoration:none;font-size:.92rem}
.top nav a:hover{color:var(--fg)}
.toc{margin:22px 0 34px;padding:18px 22px;background:var(--card);
border:1px solid var(--line);border-radius:12px}
.toc ol{margin:0;padding-left:0;list-style:none;columns:2;column-gap:28px}  /* le sezioni hanno gia' il proprio numero: contarle due volte confonde */
.toc li{margin:5px 0;font-size:.92rem}
.toc a{text-decoration:none}
.toc a:hover{text-decoration:underline}

/* Su carta questo sito ci finisce davvero: un analista lo salva in PDF e lo
   allega a una nota interna. Senza queste regole si porta dietro la testata
   fissa ripetuta, i fondi scuri e i link come testo cieco — un lettore che
   stampa perde proprio i rimandi che rendono verificabile ciò che legge. */
@media print{
 :root{--bg:#fff;--card:#fff;--fg:#111;--mut:#444;--line:#bbb;--grid:#ddd;
 --axis:#888;--hi:#111;--warnbg:#fff}
 .top,.more,.dl,.search,.count,.toc{display:none}
 body{font-size:10.5pt}
 .wrap{max-width:none;padding:0}
 section{padding:14pt 0;break-inside:avoid}
 .card,.note,.shield,.howto{border:1px solid #bbb;break-inside:avoid}
 .figure .n{font-size:32pt}
 h1{font-size:20pt}h2{font-size:13pt}
 h2,h3{break-after:avoid}
 .viz{min-width:0}
 table{font-size:9pt}
 thead{display:table-header-group}
 tr{break-inside:avoid}
 /* l'indirizzo di un link non e' recuperabile da chi legge su carta */
 a[href^="http"]::after{content:" (" attr(href) ")";font-size:8.5pt;color:#555;
 word-break:break-all}
 a[href^="#"]::after,a[href^="mailto"]::after{content:""}
 .foot{border-top:1px solid #bbb}
}

@media(max-width:600px){.toc ol{columns:1}}
.gloss{display:grid;grid-template-columns:1fr;gap:0;margin:18px 0}
.gloss div{padding:16px 0;border-bottom:1px solid var(--line)}
.gloss div:last-child{border-bottom:none}
.gloss dt{font-weight:640;margin-bottom:4px}
.gloss dd{margin:0;color:var(--mut);font-size:.95rem}
.gloss div:target dt{color:var(--hi)}
h1{font-size:1.7rem;margin:0 0 8px;letter-spacing:-.02em}
h2{font-size:1.12rem;margin:40px 0 8px;letter-spacing:-.01em}
h3{font-size:.98rem;margin:24px 0 6px;color:var(--fg)}
.sub{color:var(--mut);margin:0 0 22px}
p{margin:12px 0}
ul,ol{margin:12px 0;padding-left:22px}
li{margin:6px 0}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;margin:12px 0;font-size:.88rem}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:500;font-size:.75rem;text-transform:uppercase;
letter-spacing:.05em}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.note{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--hi);
border-radius:8px;padding:14px 18px;color:var(--mut);font-size:.9rem;margin:18px 0}
.note.warn{border-left-color:var(--warn)}
.note b{color:var(--fg)}
.foot{color:var(--mut);font-size:.8rem;margin-top:44px;border-top:1px solid var(--line);
padding-top:16px}
a{color:var(--hi)}
code{font-family:ui-monospace,monospace;font-size:.85em;background:var(--card);
padding:1px 5px;border-radius:4px}
"""

STYLE_INDEX = """
@font-face{font-family:Inter;src:url(inter.woff2) format('woff2');
font-weight:100 900;font-style:normal;font-display:swap}
:root{color-scheme:light dark;
--bg:#fbfcfd;--card:#ffffff;--fg:#111c25;--mut:#5b6b78;--line:#e4e9ee;
--grid:#eceff3;--axis:#c8d1d9;--s1:#2a78d6;--s2:#eb6834;--up:#e34948;--dn:#2a78d6;
--pos:#c2410c;--neg:#0f766e;--hi:#1b64c0;--warn:#b45309;--warnbg:#fdf6ec}
@media(prefers-color-scheme:dark){:root{
--bg:#0d1216;--card:#161d23;--fg:#e9eef2;--mut:#93a4b1;--line:#232d35;
--grid:#1f2830;--axis:#3a4650;--s1:#3987e5;--s2:#d95926;--up:#e66767;--dn:#3987e5;
--pos:#ff8a6b;--neg:#5fd0a8;--hi:#68b0ff;--warn:#f0b429;--warnbg:#1d1c14}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.65 Inter,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
-webkit-font-smoothing:antialiased}
.wrap{max-width:820px;margin:0 auto;padding:0 22px}
body.data .wrap,body.data .top .wrap{max-width:1080px}
a{color:var(--hi)}
.top{position:sticky;top:0;z-index:9;background:var(--bg);
border-bottom:1px solid var(--line)}
.top .wrap{display:flex;align-items:center;gap:26px;height:58px}
.brand{display:flex;align-items:center;gap:9px;font-weight:640;letter-spacing:-.01em;
text-decoration:none;color:var(--fg);margin-right:auto;font-size:1.02rem}
.brand svg{width:26px;height:26px;display:block}
.top nav{display:flex;gap:20px}
.top nav a{color:var(--mut);text-decoration:none;font-size:.92rem}
.top nav a:hover{color:var(--fg)}
.hero{padding:64px 0 8px}
.eyebrow{color:var(--mut);font-size:.83rem;letter-spacing:.06em;text-transform:uppercase;
margin:0 0 14px}
h1{font-size:2.45rem;line-height:1.15;letter-spacing:-.025em;margin:0 0 18px;max-width:16em;
text-wrap:balance}
.lede{font-size:1.12rem;color:var(--mut);margin:0;max-width:34em}
.figure{display:flex;align-items:baseline;gap:16px;margin:38px 0 6px}
.figure .n{font-size:4.4rem;font-weight:660;letter-spacing:-.04em;line-height:1}
.figure .u{font-size:1rem;color:var(--mut);max-width:15em;line-height:1.45}
.shield{border-left:3px solid var(--warn);background:var(--warnbg);padding:12px 16px;
border-radius:0 8px 8px 0;font-size:.94rem;margin:18px 0 0;max-width:38em}
.shield b{color:var(--fg)}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line);
border:1px solid var(--line);border-radius:12px;overflow:hidden;margin:40px 0 0}
.stat{background:var(--card);padding:16px 18px}
.stat .v{font-size:1.5rem;font-weight:640;letter-spacing:-.02em}
.stat .l{color:var(--mut);font-size:.8rem;margin-top:3px;line-height:1.35}
section{padding:52px 0;border-top:1px solid var(--line)}
h2{font-size:1.55rem;letter-spacing:-.02em;margin:0 0 10px;line-height:1.25;
text-wrap:balance}
h3{font-size:1.02rem;margin:0 0 6px;letter-spacing:-.01em}
p.hint,.sub{color:var(--mut);margin:0 0 26px;max-width:36em;font-size:.96rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:22px 24px;margin:20px 0}
.card p{margin:0;font-size:.96rem} .card p+p{margin-top:10px}
.caveat{color:var(--mut);font-size:.9rem}
.cap{color:var(--mut);font-size:.86rem;margin:14px 0 0}
.vizwrap{overflow-x:auto}
.viz{width:100%;min-width:600px;height:auto;display:block;overflow:visible}
.findings{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.findings .card{margin:0}
.more{display:inline-block;margin-top:12px;font-size:.9rem;text-decoration:none}
.more:hover{text-decoration:underline}
.dl{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.dl a{display:block;background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:18px 20px;text-decoration:none;color:var(--fg)}
.dl a:hover{border-color:var(--axis)}
.dl b{display:block;font-size:.98rem}
.dl span{color:var(--mut);font-size:.88rem}
.term{color:inherit;text-decoration:none;border-bottom:1px dotted var(--axis);cursor:help}
.term:hover{color:var(--hi);border-bottom-color:var(--hi)}
.howto{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:20px 24px 22px;margin:34px 0 0}
.howto h3{font-size:.78rem;text-transform:uppercase;letter-spacing:.07em;
color:var(--mut);font-weight:600;margin:0 0 12px}
.howto dl{margin:0;display:grid;grid-template-columns:auto 1fr;gap:8px 16px;
font-size:.94rem;align-items:baseline}
.howto dt{font-weight:640;white-space:nowrap}
.howto dd{margin:0;color:var(--mut)}
.howto dd b{color:var(--fg);font-weight:560}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:12px;background:var(--card)}
table{width:100%;border-collapse:collapse;font-size:.9rem}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--mut);font-weight:500;font-size:.72rem;text-transform:uppercase;
letter-spacing:.06em;background:var(--card)}
td.num{text-align:right;font-variant-numeric:tabular-nums}
td.big{font-weight:650}
td.pos{color:var(--pos)} td.neg{color:var(--neg)}
td.r{font-weight:500;white-space:normal}
.code{color:var(--mut);font-size:.76rem;margin-left:8px;font-family:ui-monospace,monospace}
.flag{color:var(--warn);margin-left:6px;cursor:help}
.note{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--hi);
border-radius:8px;padding:16px 20px;color:var(--mut);font-size:.92rem;margin:20px 0}
.note.warn{border-left-color:var(--warn)}
.note b{color:var(--fg)}
.note p{margin:10px 0}
.search{width:100%;padding:12px 14px;font-size:1rem;border:1px solid var(--line);
border-radius:10px;background:var(--card);color:var(--fg);margin-bottom:8px;
font-family:inherit}
.count{color:var(--mut);font-size:.85rem;margin:0 0 18px}
.gloss{display:grid;grid-template-columns:1fr;gap:0}
.gloss div{padding:16px 0;border-bottom:1px solid var(--line)}
.gloss div:last-child{border-bottom:none}
.gloss dt{font-weight:640;margin-bottom:4px}
.gloss dd{margin:0;color:var(--mut);font-size:.95rem}
.gloss div:target dt{color:var(--hi)}
.foot{color:var(--mut);font-size:.85rem;border-top:1px solid var(--line);
padding:32px 0 60px;margin-top:0}
@media(max-width:720px){
 h1{font-size:1.9rem}.figure .n{font-size:3.2rem}
 .stats,.findings,.dl{grid-template-columns:1fr}
 .top nav{gap:15px}.top .wrap{gap:14px}.hero{padding-top:40px}
}
@media(max-width:560px){
 .top nav a[href$="#download"]{display:none}
 .figure{display:block}.figure .u{margin-top:8px;max-width:none}
}

/* Su carta questo sito ci finisce davvero: un analista lo salva in PDF e lo
   allega a una nota interna. Senza queste regole si porta dietro la testata
   fissa ripetuta, i fondi scuri e i link come testo cieco — un lettore che
   stampa perde proprio i rimandi che rendono verificabile ciò che legge. */
@media print{
 :root{--bg:#fff;--card:#fff;--fg:#111;--mut:#444;--line:#bbb;--grid:#ddd;
 --axis:#888;--hi:#111;--warnbg:#fff}
 .top,.more,.dl,.search,.count,.toc{display:none}
 body{font-size:10.5pt}
 .wrap{max-width:none;padding:0}
 section{padding:14pt 0;break-inside:avoid}
 .card,.note,.shield,.howto{border:1px solid #bbb;break-inside:avoid}
 .figure .n{font-size:32pt}
 h1{font-size:20pt}h2{font-size:13pt}
 h2,h3{break-after:avoid}
 .viz{min-width:0}
 table{font-size:9pt}
 thead{display:table-header-group}
 tr{break-inside:avoid}
 /* l'indirizzo di un link non e' recuperabile da chi legge su carta */
 a[href^="http"]::after{content:" (" attr(href) ")";font-size:8.5pt;color:#555;
 word-break:break-all}
 a[href^="#"]::after,a[href^="mailto"]::after{content:""}
 .foot{border-top:1px solid #bbb}
}

"""

# Il marchio: stessa curva della favicon. Arco ideale tratteggiato, arco reale
# pieno, l'area fra i due e' il gap.
LOGO = ('<svg viewBox="0.2 6.5 63.6 63.6" aria-hidden="true">'
        '<path d="M6 50 A26 25 0 0 1 58 50 A26 11 0 0 0 6 50 Z" fill="#ff7a55" fill-opacity=".28"/>'
        '<path d="M6 50 A26 11 0 0 1 58 50" fill="none" stroke="#8ea3b2" stroke-width="3.6"'
        ' stroke-linecap="round" stroke-dasharray="6.5 5.5"/>'
        '<path d="M6 50 A26 25 0 0 1 58 50" fill="none" stroke="#ff7a55" stroke-width="4.4"'
        ' stroke-linecap="round"/>'
        '<circle cx="6" cy="50" r="3.8" fill="#8ea3b2"/>'
        '<circle cx="58" cy="50" r="3.8" fill="#8ea3b2"/></svg>')

def citation(days) -> str:
    """Come si cita questo sito, con l'onesta' sul DOI che ancora non esiste.

    Il citabile e' il SOFTWARE, non uno studio: e' la posizione dichiarata in
    CITATION.cff e sulla pagina del metodo, e la citazione qui la ripete invece
    di contraddirla.
    """
    doi_line = (f'<br>DOI: <a href="https://doi.org/{ZENODO_DOI}">{ZENODO_DOI}</a>'
                if ZENODO_DOI else "")
    doi_note = ("" if ZENODO_DOI else
                "<p class=hint>The Zenodo archive is created when the release is "
                "published, and the identifier appears here as soon as it exists. "
                "Until then, cite the release name and the URL: they identify the "
                "version just as precisely.</p>")
    bib_doi = f"\n  doi          = {{{ZENODO_DOI}}}," if ZENODO_DOI else ""
    return f"""<div class=note>
<p><b>How to cite.</b> What is citable here is the <i>tool</i>, not a study: the
figures are outputs of running it, and anyone can re-run it.</p>
<p style="font-family:ui-monospace,monospace;font-size:.86rem;line-height:1.6">
co2gap ({RELEASE[:4]}). <i>co2gap — an open pipeline for measuring the CO<sub>2</sub>
of European flights against a fuel-optimal ideal, from ADS-B trajectories.</i> Release {RELEASE},
methodology v{METHOD_VERSION}, covering {esc(days[0])} to {esc(days[-1])}.
{SITE_URL}{doi_line}</p>
{doi_note}
<details><summary>BibTeX</summary>
<pre style="overflow-x:auto;font-size:.82rem">@software{{co2gap_{RELEASE[:4]},
  author       = {{co2gap}},
  title        = {{co2gap: an open pipeline for measuring the CO2 of European
                  flights against a fuel-optimal ideal, from ADS-B trajectories}},
  version      = {{{RELEASE}}},
  url          = {{{SITE_URL}}},{bib_doi}
  year         = {{{RELEASE[:4]}}}
}}</pre></details>
<p class=hint>The machine-readable form is
<a href="https://github.com/co2gap/co2gap/blob/master/CITATION.cff">CITATION.cff</a>
in the repository.</p>
</div>"""


# Il menu in alto e' il percorso di lettura e si ferma a sei voci. Releases e
# Replies sono pagine di consultazione: si cercano dopo una domanda precisa, e nel
# testo sono linkate proprio nel punto in cui quella domanda nasce. Ma un lettore
# che quel punto non lo attraversa non deve poter concludere che non esistono, e
# per questo il piede le elenca tutte, identico su ogni pagina.
FOOTNAV = ('<a href="index.html">Findings</a> · <a href="context.html">Context</a> · '
           '<a href="data.html">Data</a> · <a href="methodology.html">Method</a> · '
           '<a href="faq.html">FAQ</a> · <a href="releases.html">Releases</a> · '
           '<a href="replies.html">Replies</a> · <a href="feed.xml">Updates feed</a>')

NAV = f"""<div class=top><div class=wrap>
<a class=brand href="index.html">{LOGO}co2gap</a>
<nav><a href="index.html#findings">Findings</a><a href="context.html">Context</a>
<a href="data.html">Data</a>
<a href="methodology.html">Method</a><a href="faq.html">FAQ</a><a href="index.html#download">Download</a></nav>
</div></div>"""

# ---------------------------------------------------------------- glossario --
# Il lettore che decide se questo sito viene capito non e' il controllore di
# volo: e' il giornalista che ha due ore. Ogni voce e' UNA frase e non usa gergo
# di secondo livello. Verificate alla fonte il 2026-08-17: ECAC 44 stati
# (ecac-ceac.org); i 39 kg CCO/CDO sono CARBURANTE (4,3 per partenza + 35 per
# arrivo, EUROCONTROL); di KEA EUROCONTROL non pubblica lo scioglimento
# dell'acronimo, quindi si da' la definizione e non l'espansione.
GLOSSARY = [
    ("ecac", "ECAC area",
     "The European Civil Aviation Conference: 44 member states, from Iceland and "
     "Norway to Turkey, Armenia and Azerbaijan — a wider Europe than the European "
     "Union. It is the area this site covers, and the same one EUROCONTROL uses "
     "for its own published figures."),
    ("point", "Point",
     "One percentage point of the ideal flight's CO&#8322;. An airport at +10 points "
     "emits about 10% more than comparable flights. Points are always relative: they "
     "never say how much fuel was burnt, only how far from the reference it is."),
    ("norm", "The norm (Δ norm)",
     "The median of flights of the same length and the same aircraft type. The raw "
     "gap grows as flights get shorter, so ranking it would sort by shortness; every "
     "ranking here measures the distance from the norm instead."),
    ("lateral", "Lateral and vertical",
     "The two parts the gap splits into, which add up to the total. Lateral is the "
     "cost of flying more kilometres than the direct route; vertical is the cost of "
     "flying the same route on a less efficient climb, cruise and descent profile."),
    ("movements", "Movements",
     "Take-offs and landings counted together. A flight counts once at the airport it "
     "leaves and once at the airport it reaches."),
    ("adsb", "ADS-B",
     "The position, altitude and speed each aircraft broadcasts about twice a second. "
     "Anyone with a receiver can pick it up; every trajectory here comes from those "
     "messages, collected by volunteers and published by adsb.lol."),
    ("gc", "Great circle",
     "The shortest path between two points on the globe. It is the direct route each "
     "flight is compared against — a geometric reference, not a route anyone is "
     "allowed to fly."),
    ("enroute", "En route",
     "The part of a flight beyond 40 NM from either airport, outside the terminal "
     "areas where departures and arrivals are sequenced. EUROCONTROL's efficiency "
     "indicator covers only this portion, so our comparison with it does too."),
    ("nm", "NM (nautical mile)",
     "1,852 metres, the standard distance unit in aviation. 40 NM is about 74 km."),
    ("kea", "KEA",
     "EUROCONTROL's indicator of horizontal en-route flight inefficiency: how much "
     "further flights actually fly than the direct route, over the en-route portion "
     "only. It is built from radar data, as a rolling 12-month average discarding the "
     "ten best and ten worst days of each area; ours is not. Same construction, not "
     "the same number."),
    ("cco", "Continuous climb and descent (CCO/CDO)",
     "Climbing or descending without level-off segments. Level segments burn extra "
     "fuel, and removing them is one of the few savings the industry quantifies "
     "publicly: EUROCONTROL puts the network-wide potential at about 4 kg of fuel per "
     "departure and 35 kg per arrival."),
    ("mt", "Mt and kt",
     "Million tonnes and thousand tonnes. Burning one kg of jet fuel releases about "
     "3.16 kg of CO&#8322;."),
    ("era5", "ERA5",
     "The weather reanalysis published by the European Centre for Medium-Range Weather "
     "Forecasts: the wind that was actually blowing, hour by hour. Without it the same "
     "route measures differently in the two directions."),
    ("openap", "OpenAP",
     "An open aircraft performance model from TU Delft. It turns a trajectory and an "
     "aircraft type into fuel burnt; it is what makes this computable without any "
     "airline data."),
    ("odbl", "ODbL",
     "The Open Database Licence covering the source trajectories. Reuse is free, with "
     "attribution, but a database derived from it must carry the same licence, which "
     "is why the figures on this site do."),
]
GTERMS = {k: (t, d) for k, t, d in GLOSSARY}


def term(slug, text=None, page="methodology.html"):
    """Termine collegato al glossario: punteggiato, non azzurro.

    Dentro una frase un collegamento azzurro invita ad andarsene; il punteggiato
    dice "puoi insistere qui" senza rompere la lettura.
    """
    t, d = GTERMS[slug]
    return (f'<a class=term href="{page}#g-{slug}" '
            f'title="{esc(d.replace("&#8322;", "2"))}">{text or t}</a>')


# ----------------------------------------------------------------- grafici ---
# SVG generati qui: nessuna libreria, nessuna richiesta esterna, e i numeri sono
# gli stessi delle tabelle perche' vengono dallo stesso dataframe.
# Regole applicate: marchi sottili, griglia hairline solida, 2px di superficie
# fra segmenti che si toccano, etichette diritte solo sugli estremi, il testo
# non porta mai il colore della serie, e ogni grafico ha il suo gemello in
# tabella su data.html. Arancio e rosso non compaiono mai insieme: la coppia
# fallisce i controlli per daltonismo (blu-arancio e blu-rosso li passano).


def viz_concept():
    """I due termini disegnati, non descritti.

    E' il concetto centrale del sito e finora esisteva solo a parole: chi non ha
    mai pensato a un profilo di volo legge "climb, cruise and descent" e non
    vede niente, mentre quel termine e' due terzi del gap. Il linguaggio e'
    quello del marchio — tratteggiato l'ideale, pieno il reale, l'area fra i due
    e' il gap — e i colori sono quelli della barra della scomposizione, cosi'
    il disegno e il numero si riconoscono l'un l'altro.
    SCHEMATICO, e la didascalia lo dice: non e' un volo reale.
    """
    W, H = 700, 250
    o = []
    # ---- pannello sinistro: vista in pianta, il termine laterale -----------
    ox, oy, dx_, dy = 40, 170, 300, 96
    o.append(f'<text x="0" y="16" font-size="12" font-weight="600" fill="var(--fg)">'
             f'Lateral — the route on the map</text>')
    # l'area fra percorso diretto e traccia reale
    o.append(f'<path d="M{ox} {oy} L{dx_} {dy} Q{230} {40} {ox} {oy} Z" '
             f'fill="var(--s1)" fill-opacity=".13"/>')
    o.append(f'<line x1="{ox}" y1="{oy}" x2="{dx_}" y2="{dy}" stroke="var(--mut)" '
             f'stroke-width="2" stroke-dasharray="6 5" stroke-linecap="round"/>')
    o.append(f'<path d="M{ox} {oy} Q{230} {40} {dx_} {dy}" fill="none" '
             f'stroke="var(--s1)" stroke-width="2.5" stroke-linecap="round"/>')
    for cx, cy in ((ox, oy), (dx_, dy)):
        o.append(f'<circle cx="{cx}" cy="{cy}" r="5" fill="var(--fg)" '
                 f'stroke="var(--card)" stroke-width="2"/>')
    o.append(f'<text x="{ox-6}" y="{oy+20}" font-size="11" fill="var(--mut)">departure</text>')
    o.append(f'<text x="{dx_+6}" y="{dy+4}" font-size="11" fill="var(--mut)">arrival</text>')
    o.append(f'<text x="150" y="152" font-size="11" fill="var(--mut)">direct route</text>')
    o.append(f'<text x="150" y="72" font-size="11" fill="var(--s1)" font-weight="600">'
             f'flown track</text>')
    o.append(f'<text x="0" y="{H-26}" font-size="11.5" fill="var(--fg)">'
             f'The shaded area is the <tspan font-weight="600">extra distance</tspan>.</text>')
    o.append(f'<text x="0" y="{H-9}" font-size="11.5" fill="var(--mut)">'
             f'It is the smaller of the two terms.</text>')
    # ---- pannello destro: vista di profilo, il termine verticale -----------
    L, G = 380, 196          # margine sinistro del pannello, quota del suolo
    o.append(f'<line x1="360" y1="8" x2="360" y2="{H-40}" stroke="var(--line)"/>')
    o.append(f'<text x="{L}" y="16" font-size="12" font-weight="600" fill="var(--fg)">'
             f'Vertical — the profile flown along it</text>')
    # I due profili come liste di punti: il riempimento si chiude percorrendo
    # l'ideale in avanti e il reale all'indietro. Concatenarli nello stesso
    # verso produce un poligono che si autointerseca — e si vede.
    ideal_pts = [(L, G), (450, 70), (610, 70), (690, G)]
    # Il reale deve restare SOTTO l'ideale per tutto il tracciato: se i due si
    # incrociano il disegno dice che il volo e' rimasto piu' alto dell'ottimo,
    # cioe' il contrario di cio' che i dati mostrano. Il livellamento in discesa
    # va quindi tenuto piu' basso della dashed alla stessa ascissa.
    real_pts = [(L, G), (432, 116), (474, 116), (520, 86),
                (596, 86), (620, 155), (650, 155), (690, G)]
    pth = lambda pts: "M" + " L".join(f"{x} {y}" for x, y in pts)
    ideal, real = pth(ideal_pts), pth(real_pts)
    o.append(f'<path d="{pth(ideal_pts + real_pts[::-1])} Z" '
             f'fill="var(--s2)" fill-opacity=".13"/>')
    o.append(f'<line x1="{L}" y1="{G}" x2="690" y2="{G}" stroke="var(--axis)"/>')
    o.append(f'<path d="{ideal}" fill="none" stroke="var(--mut)" stroke-width="2" '
             f'stroke-dasharray="6 5" stroke-linecap="round" stroke-linejoin="round"/>')
    o.append(f'<path d="{real}" fill="none" stroke="var(--s2)" stroke-width="2.5" '
             f'stroke-linecap="round" stroke-linejoin="round"/>')
    o.append(f'<text x="470" y="62" font-size="11" fill="var(--mut)">ideal profile</text>')
    o.append(f'<text x="530" y="106" font-size="11" fill="var(--s2)" font-weight="600">'
             f'flown profile</text>')
    o.append(f'<text x="434" y="134" font-size="10.5" fill="var(--mut)">levelling off</text>')
    o.append(f'<text x="576" y="184" font-size="10.5" fill="var(--mut)">early descent</text>')
    o.append(f'<text x="{L}" y="{H-26}" font-size="11.5" fill="var(--fg)">'
             f'The shaded area is <tspan font-weight="600">two thirds of the gap</tspan>, '
             f'and</text>')
    o.append(f'<text x="{L}" y="{H-9}" font-size="11.5" fill="var(--mut)">'
             f'most of it sits in the descent.</text>')
    return (f'<svg class=viz viewBox="0 0 {W} {H}" role="img" aria-label="Two schematic '
            f'diagrams: on the left a flown track bowing away from the direct route between '
            f'two airports; on the right a flown altitude profile that levels off during the '
            f'climb and starts its descent early, against a continuous ideal profile">'
            + "".join(o) + "</svg>")

def viz_split(lat_w, vert_w):
    """Anatomia del numero di testa: quanto e' percorso e quanto e' profilo."""
    W, H = 700, 104
    tot = lat_w + vert_w
    w1 = W * lat_w / tot
    w2 = W - w1 - 2
    y, h, r = 6, 26, 4
    return f'''<svg class=viz viewBox="0 0 {W} {H}" role="img"
 aria-label="The {tot:.1f} point gap splits into {lat_w:.1f} lateral and {vert_w:.1f} vertical">
<title>Lateral {lat_w:.1f} points · vertical {vert_w:.1f} points</title>
<rect x="0" y="{y}" width="{w1:.1f}" height="{h}" rx="{r}" fill="var(--s1)"/>
<rect x="{w1+2:.1f}" y="{y}" width="{w2:.1f}" height="{h}" rx="{r}" fill="var(--s2)"/>
<g font-size="13">
 <text x="0" y="{y+h+24}" fill="var(--fg)" font-weight="600">{lat_w:.1f} pts lateral</text>
 <text x="0" y="{y+h+42}" fill="var(--mut)">extra kilometres flown</text>
 <text x="{w1+14:.1f}" y="{y+h+24}" fill="var(--fg)" font-weight="600">{vert_w:.1f} pts vertical</text>
 <text x="{w1+14:.1f}" y="{y+h+42}" fill="var(--mut)">climb, cruise and descent profile</text>
</g></svg>'''


def viz_bands(band):
    """Colonne: gap mediano per fascia di distanza. Serie unica, niente legenda."""
    rows = [(str(i), int(r.n), float(r.med)) for i, r in band.iterrows()]
    W, H = 700, 300
    L, R, TOP, BOT = 34, 8, 16, 62
    pw, ph = W - L - R, H - TOP - BOT
    ymax = max(80, max(v for _, _, v in rows) + 8)
    sy = lambda v: TOP + ph - ph * v / ymax
    slot = pw / len(rows)
    bw = min(24, slot - 14)
    out = []
    for v in range(0, int(ymax) + 1, 20):
        out.append(f'<line x1="{L}" x2="{W-R}" y1="{sy(v):.1f}" y2="{sy(v):.1f}" '
                   f'stroke="var(--grid)"/>'
                   f'<text x="{L-8}" y="{sy(v)+4:.1f}" text-anchor="end" font-size="11" '
                   f'fill="var(--mut)" style="font-variant-numeric:tabular-nums">{v}%</text>')
    for i, (lab, n, v) in enumerate(rows):
        x = L + slot * i + (slot - bw) / 2
        out.append(f'<g><title>{esc(lab)} km · {n:,} flights · median gap {pct0(v)}</title>'
                   f'<path d="M{x:.1f} {sy(0):.1f} V{sy(v)+4:.1f} a4 4 0 0 1 4 -4 '
                   f'h{bw-8:.1f} a4 4 0 0 1 4 4 V{sy(0):.1f} Z" fill="var(--s1)"/></g>'
                   f'<text x="{x+bw/2:.1f}" y="{H-44}" text-anchor="middle" font-size="10.5" '
                   f'fill="var(--mut)" style="font-variant-numeric:tabular-nums">'
                   f'{esc(lab.split(",")[0].strip("( "))}</text>')
    for i in (0, len(rows) - 1):
        lab, n, v = rows[i]
        out.append(f'<text x="{L+slot*i+slot/2:.1f}" y="{sy(v)-8:.1f}" text-anchor="middle" '
                   f'font-size="12" font-weight="600" fill="var(--fg)">{pct0(v)}</text>')
    return (f'<svg class=viz viewBox="0 0 {W} {H}" role="img" aria-label="Median gap by '
            f'distance band: {pct0(rows[0][2])} on the shortest sectors, falling to '
            f'{pct0(rows[-1][2])} on the longest">' + "".join(out) +
            f'<line x1="{L}" x2="{W-R}" y1="{sy(0):.1f}" y2="{sy(0):.1f}" stroke="var(--axis)"/>'
            f'<text x="{L}" y="{H-16}" font-size="11" fill="var(--mut)">'
            f'lower bound of the great-circle distance band, km</text></svg>')


def viz_routes(g, aname, n=12):
    """Barre orizzontali: le rotte piu' lontane dalla norma."""
    rows = list(g.sort_values("d", ascending=False).head(n).iterrows())
    W, LAB, VAL, row_h, bw = 700, 266, 46, 26, 14
    H = row_h * len(rows) + 34
    pw = W - LAB - VAL
    vmax = max(float(r.d) for _, r in rows)
    out = []
    for i, (pair, r) in enumerate(rows):
        a, b = pair
        y = 8 + row_h * i
        w = pw * float(r.d) / vmax
        name = f"{aname(a)} ↔ {aname(b)}"
        short = name if len(name) <= 34 else name[:33] + "…"
        out.append(
            f'<g><title>{esc(name)} ({esc(a)}–{esc(b)}) · {int(r.n):,} flights · '
            f'{r.d:+.0f} points vs the norm · lateral {r.lat:.0f}% · '
            f'vertical {r.vert:.0f}%</title>'
            f'<text x="0" y="{y+bw/2+4}" font-size="12" fill="var(--fg)">{esc(short)}'
            f'{" ⚑" if r.closed else ""}</text>'
            f'<path d="M{LAB} {y} h{max(w-4,1):.1f} a4 4 0 0 1 4 4 v{bw-8} '
            f'a4 4 0 0 1 -4 4 H{LAB} Z" fill="var(--up)"/>'
            f'<text x="{LAB+w+8:.1f}" y="{y+bw/2+4}" font-size="12" font-weight="600" '
            f'fill="var(--fg)" style="font-variant-numeric:tabular-nums">{r.d:+.0f}</text></g>')
    out.append(f'<text x="{LAB}" y="{H-8}" font-size="11" fill="var(--mut)">'
               f'points above the norm for flights of the same length and aircraft type</text>')
    return (f'<svg class=viz viewBox="0 0 {W} {H}" role="img" aria-label="The {len(rows)} '
            f'routes furthest above the European norm">' + "".join(out) + '</svg>')


def viz_airports(ga, aname, nw=10, nb=6):
    """Dot plot divergente attorno alla norma: rosso sopra, blu sotto."""
    top = ga.sort_values("d", ascending=False)
    rows = ([(i, r) for i, r in top.head(nw).iterrows()] + [None] +
            [(i, r) for i, r in top.tail(nb).iterrows()])
    W, LAB, MOV, row_h, TOP = 700, 210, 62, 24, 26
    H = row_h * len(rows) + 46 + TOP
    pw = W - LAB - MOV
    vals = [float(r.d) for x in rows if x for _, r in [x]]
    lo, hi = min(vals) - 2, max(vals) + 2
    sx = lambda v: LAB + pw * (v - lo) / (hi - lo)
    out = []
    for v in range(int(lo // 5) * 5, int(hi) + 5, 5):
        if lo < v < hi:
            out.append(f'<line x1="{sx(v):.1f}" x2="{sx(v):.1f}" y1="{TOP}" y2="{H-40}" '
                       f'stroke="var(--grid)"/>'
                       f'<text x="{sx(v):.1f}" y="{H-24}" text-anchor="middle" font-size="11" '
                       f'fill="var(--mut)" style="font-variant-numeric:tabular-nums">'
                       f'{v:+d}</text>')
    out.append(f'<line x1="{sx(0):.1f}" x2="{sx(0):.1f}" y1="{TOP}" y2="{H-40}" '
               f'stroke="var(--axis)"/>'
               f'<text x="{sx(0)-8:.1f}" y="{TOP-9}" text-anchor="end" font-size="11" '
               f'fill="var(--mut)">← closer to the norm</text>'
               f'<text x="{sx(0)+8:.1f}" y="{TOP-9}" font-size="11" fill="var(--mut)">'
               f'further above it →</text>'
               f'<text x="{W}" y="{TOP-9}" text-anchor="end" font-size="11" '
               f'fill="var(--mut)">movements</text>')
    for i, item in enumerate(rows):
        y = 18 + TOP + row_h * i
        if item is None:
            out.append(f'<line x1="0" x2="{W}" y1="{y-6}" y2="{y-6}" stroke="var(--line)"/>')
            continue
        icao, r = item
        d = float(r.d)
        name = aname(icao)
        short = name if len(name) <= 26 else name[:25] + "…"
        col = "var(--up)" if d > 0 else "var(--dn)"
        out.append(
            f'<g><title>{esc(name)} ({esc(icao)}) · {int(r.n):,} movements · '
            f'{d:+.1f} points vs the norm · {r.dep:+.1f} on departure · '
            f'{r.arr:+.1f} on arrival</title>'
            f'<rect x="0" y="{y-11}" width="{W}" height="{row_h}" fill="transparent"/>'
            f'<text x="0" y="{y+4}" font-size="12" fill="var(--fg)">{esc(short)}</text>'
            f'<line x1="{sx(0):.1f}" x2="{sx(d):.1f}" y1="{y}" y2="{y}" stroke="{col}" '
            f'stroke-width="2" opacity=".45"/>'
            f'<circle cx="{sx(d):.1f}" cy="{y}" r="5" fill="{col}" stroke="var(--card)" '
            f'stroke-width="2"/>'
            f'<text x="{W}" y="{y+4}" text-anchor="end" font-size="11.5" fill="var(--mut)" '
            f'style="font-variant-numeric:tabular-nums">{int(r.n):,}</text></g>')
    out.append(f'<text x="{LAB}" y="{H-6}" font-size="11" fill="var(--mut)">'
               f'points from the European norm</text>')
    return (f'<svg class=viz viewBox="0 0 {W} {H}" role="img" aria-label="Airports furthest '
            f'above and below the European norm">' + "".join(out) + '</svg>')



def add_toc(html: str) -> str:
    """Indice della metodologia, derivato dai suoi stessi h2.

    Tredici sezioni e circa tremila parole senza una mappa: chi cerca i limiti
    dichiarati li cerca scorrendo. Gli id gia' presenti non si rigenerano —
    #independence, #licence e #glossary sono linkati da altre pagine e da
    corrispondenza gia' spedita, quindi cambiarli romperebbe rimandi esterni.
    """
    items, out = [], []
    def slug(t):
        t = re.sub(r"^\d+\.\s*", "", re.sub(r"<[^>]+>", "", t)).lower()
        return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", t)).strip("-")[:40]

    def repl(m):
        attrs, text = m.group(1), m.group(2)
        hid = re.search(r"id=([\w-]+)", attrs)
        hid = hid.group(1) if hid else slug(text)
        items.append((hid, re.sub(r"<[^>]+>", "", text)))
        return f'<h2 id={hid}{attrs if "id=" not in attrs else ""}>{text}</h2>'

    html = re.sub(r"<h2([^>]*)>(.*?)</h2>", repl, html, flags=re.S)
    if not items:
        return html
    toc = ('<nav class=toc aria-label="Sections"><ol>'
           + "".join(f'<li><a href="#{i}">{esc(t)}</a></li>' for i, t in items)
           + "</ol></nav>")
    return html.replace("<h1>Methodology</h1>", "<h1>Methodology</h1>\n" + toc, 1)

def build_methodology(df, days, months, lat_w, vert_w, kea, co2_t, excess_t,
                      n_routes_all, n_routes_rank, n_airports, gen,
                      sc_a, sc_b, sc_a_fuel_kt,
                      vert_floor, vert_fleet, vert_oper, n_floor,
                      vert_long, vert_oper_eq,
                      conv_fleet, conv_ap1,
                      finding_sections="", n_biz_routes=0,
                      non_airliner_pct=0.0) -> str:
    """The page that has to be right even when nobody reads it.

    Written comparative-first: the defensible product of this work is that one
    route deviates more than comparable ones, not that European aviation wastes
    N megatonnes. The absolute figure is context and is labelled as such.
    """
    # Confronto con Pasutto sullo STESSO perimetro (200-1500 NM), calcolato
    # e non battuto a mano: era 13,1% fisso nel template e dopo la correzione
    # del rullaggio sarebbe rimasto li' a mentire.
    # Le due correlazioni erano DIGITATE. Quella grezza (-0,74) oggi coincide
    # per combinazione col valore post-correzione; la residua era stampata
    # +0,08 mentre il segno vero e' negativo. Una cifra digitata che per caso
    # torna giusta e' piu' pericolosa di una sbagliata: non la ricontrolla piu'
    # nessuno.
    # ⚠️ E vanno calcolate SULLE ROTTE, che e' l'unita' di cui parla la frase:
    # "a raw ranking would order routes by shortness". Per volo la stessa
    # correlazione da' -0,54, ed e' un'altra grandezza. Sbagliare unita' qui
    # sarebbe lo stesso errore che il paragrafo denuncia.
    _rt = df.groupby("pair").agg(gc=("gc_km", "median"),
                                 raw=("excess_total_pct", "median"),
                                 res=("d_tot", "median"), n=("d_tot", "size"))
    _rt = _rt[_rt.n >= RANK_MIN_N]
    _r_raw = float(np.corrcoef(_rt.gc.to_numpy(), _rt.raw.to_numpy())[0, 1])
    _r_res = float(np.corrcoef(_rt.gc.to_numpy(), _rt.res.to_numpy())[0, 1])

    _pas = df[(df.gc_km >= 370.4) & (df.gc_km <= 2778.0)]
    _pas_id = _pas.ideal_gc_co2_kg.sum()
    pas_ours = float((_pas.co2_kg_v0.sum() - _pas_id) / _pas_id * 100)
    # La cifra sopra e' il gap TOTALE, laterale incluso, mentre quella di
    # Pasutto e' di sola crociera. Il fattore fra le due era attribuito a tre
    # differenze dichiarate, e la causa maggiore non era fra quelle: e' la
    # rotta. Si scompone qui, cosi' il testo puo' nominarla.
    pas_lat = float((_pas.hybrid_co2_kg.sum() - _pas_id) / _pas_id * 100)
    pas_vert = float((_pas.co2_kg_v0.sum() - _pas.hybrid_co2_kg.sum())
                     / _pas_id * 100)

    # Quale componente sia la maggiore sotto le DUE convenzioni e' un fatto che
    # si e' invertito il 2026-08-28 con la correzione del rullaggio. Il testo
    # diceva "The vertical component still dominates" accanto a quattro coppie
    # lat/vert in cui il laterale era il maggiore in tutte e quattro.
    _coppie = [(conv_fleet[0], conv_fleet[1]), (conv_fleet[2], conv_fleet[3]),
               (conv_ap1[0], conv_ap1[1]), (conv_ap1[2], conv_ap1[3])]
    if all(l > v for l, v in _coppie):
        conv_ord = ("The lateral component is the larger one in all four pairs")
    elif all(v > l for l, v in _coppie):
        conv_ord = ("The vertical component is the larger one in all four pairs")
    else:
        conv_ord = ("Which of the two is the larger one changes between the "
                    "conventions, so that ordering is a property of the "
                    "convention and not of the flights")
    biz_types = ", ".join(sorted(NON_AIRLINER))
    glossary_rows = "".join(
        f"<div id=g-{k}><dt>{t}</dt><dd>{d}</dd></div>"
        for k, t, d in GLOSSARY)
    gate_rows = "\n".join(
        f"<tr><td>{m}</td><td class=num>{r:,}</td><td class=num>{wf:.1f}</td>"
        f"<td class=num><b>{wa:.1f}</b></td></tr>"
        for m, (wf, wa, r) in GATE.items())
    per_flight_vert = vertical_kg_per_flight(df)
    return f"""<!doctype html>
<html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Methodology — co2gap</title>
{meta("Methodology — co2gap", DESC_METHOD, "methodology.html")}
<style>{STYLE}</style></head><body>
{NAV}
<div class=wrap>

<h1>Methodology</h1>

<h2>What the data shows, in full</h2>
<p>The four findings summarised on the home page, with the qualifications that
belong to them.</p>
{finding_sections}
<p class=sub>How the figures on this site are computed, what they mean and —
above all — what they do <b>not</b> mean.<br>
<b>Release {RELEASE}</b> · methodology v{METHOD_VERSION} · generated {esc(gen)}.</p>

<h2>1. The question being answered</h2>
<p>For every flight we compare the CO&#8322; actually emitted with that of an
<b>ideal</b> flight: same aircraft type, direct great-circle route, the most
efficient altitude and speed for that distance, and <b>the same real wind</b>.</p>
<p>The difference is split into two parts that add up to the total:</p>
<ul>
<li><b>lateral</b> — the cost of having flown more kilometres than necessary;</li>
<li><b>vertical</b> — the cost of having flown the <i>same</i> route on a less
efficient altitude and speed profile.</li>
</ul>
<p>The separation comes from an intermediate baseline: the <i>real</i> ground
track, but an <i>optimal</i> altitude and speed profile. The two components are
additive by construction, because they share a denominator.</p>

<h2>2. What is NOT being measured</h2>
<div class="note warn">
<p><b>This is not wasted, recoverable fuel.</b> The ideal flight is a
theoretical limit no real flight can reach: separation between aircraft, route
structure, constrained airspace, arrival sequencing and weather put it out of
reach for reasons that are not inefficiency.</p>
<p>Estimates of <i>avoidable</i> inefficiency published by bodies in the field
are much smaller than the gap measured here, and rightly so:</p>
<table><thead><tr><th>measure</th><th class=num>per flight</th></tr></thead><tbody>
<tr><td>EUROCONTROL — level-offs in climb and descent, recoverable through
CCO/CDO procedures</td><td class=num>~{BENCH['cco_cdo_kg']} kg</td></tr>
<tr><td>Pasutto et al. (EUROCONTROL, 2021) — cruise, against the best profile
actually flown</td><td class=num>{BENCH['pasutto_kg']}–{BENCH['pasutto_avg_kg']} kg</td></tr>
<tr><td><b>this site</b> — gap from the theoretical optimum, whole profile</td>
<td class=num><b>~{per_flight_vert:.0f} kg</b></td></tr>
</tbody></table>
<p>Over the same distance range Pasutto uses (200–1500 NM), their
{BENCH['pasutto_pct']}% median for cruise alone compares with our {pas_ours:.1f}% for
the whole flight: a factor of <b>{pas_ours/BENCH['pasutto_pct']:.1f}</b>, explained
by four differences, of which the first is the largest.
<b>Ours includes the route</b> — {pas_lat:.1f} of those {pas_ours:.1f} points are
lateral — and theirs excludes it by construction; that one difference accounts for
most of the factor. Then: their reference is the <i>best observed profile</i>,
ours a physical optimum; they cover cruise only, we also cover climb, descent and
speed; they assume nominal mass and no wind, we use estimated mass and real wind.</p>
<p>Our vertical component alone over the same range is {pas_vert:.1f}%, which lands
next to their {BENCH['pasutto_pct']}%. <b>We do not present that as agreement.</b>
Ours covers the whole profile — climb and descent included, which is where our gap
concentrates — while theirs is cruise only, so the two numbers are close without
measuring the same thing. Read it as a coincidence worth knowing, not as a
validation.</p>
<p><b>Practical consequence:</b> multiplying our total by a carbon price and
calling it "waste" would be wrong. We do not do it, and we ask that it not be
done.</p>
</div>

<h3>How much is compressible — measured, not assumed</h3>
<p>A flight going direct, departing at night into an empty sky, on a long
sector, is about as close to the ideal trajectory as an airliner actually gets.
Across {n_floor:,} such flights the vertical gap still stands at
<b>{vert_floor:.1f}%</b>, against a median across all flights of
<b>{vert_fleet:.1f}%</b>.</p>
<p><b>What is measured is that the gap does not disappear under near-ideal
conditions.</b> The reading we place on it — that {vert_floor:.1f} points are a
floor set by cost index, step climbs imposed by weight and discrete flight levels,
and that the remaining {vert_oper:.1f} move with traffic, routing and profile — is
an interpretation of that measurement, not a second measurement. Nothing here
separates those causes from one another, and the residual may also carry
variability of the model itself.</p>
<p><b>And the subtraction compares two groups of different length.</b> The floor
is measured on sectors above 1,000 km; across <i>all</i> flights above 1,000 km
the median vertical gap is {vert_long:.1f} points, not {vert_fleet:.1f}. At equal
distance the margin is about <b>{vert_oper_eq:.1f} points</b> rather than
{vert_oper:.1f}, and the difference between the two is the distance mix, which is
not an operational quantity. The direction of that error is conservative for the
reading above: on short sectors the incompressible share is likely larger than
{vert_floor:.1f}, not smaller.</p>
<p>The floor nearly coincides with the value a EUROCONTROL study obtains for
cruise by comparing each flight with the <i>best observed profile</i>, a
reference that already contains those constraints. Two independent routes, the
same destination: which is why the apparent gap against external references does
not indicate a model error, but the difference between a fleet median and a
best-in-class reference.</p>

<h3>A number that can be given</h3>
<p>There is a way to quantify the margin without leaning on an unreachable
optimum: compare each flight not with perfection but with <b>what flights of the
same length already achieve</b>. Half of comparable flights already fly at or
below that level, which shows it is attainable by some — not that it is attainable
by all. The flights above it may differ systematically in mass, type, airport, hour
or constraint.</p>
<ul>
<li>If flights above the median of comparable ones flew like that median:
<b>{sc_a:.1f} Mt of CO&#8322; a year</b> ({sc_a_fuel_kt:,.0f} kt of fuel).</li>
<li>Bringing only the worst quartile to the 75th percentile, a far more cautious
assumption: <b>{sc_b:.1f} Mt a year</b>.</li>
</ul>
<p>EUROCONTROL independently estimates <b>1.1 Mt of CO&#8322; a year</b> as
recoverable in the ECAC area through continuous climb and descent procedures
alone. <b>The two figures coincide, and that is not a confirmation.</b> They
count different things: the spread between comparable flights on one side, what
two named procedures recover on the other. A coincidence between measurements
of different quantities is worth no more than a difference between them would
have been.</p>
<p><b>It remains counterfactual arithmetic.</b> It assumes the median level is
reachable everywhere, and it is not: part of the spread is due to structural
constraints — closed airspace, terrain, congestion — that no procedure removes.
The figure measures <i>what the observed spread between comparable flights is
worth</i>, not what is achievable. It is the upper bound of a margin, not a
target.</p>

<h2>3. Why the comparison between routes still holds</h2>
<p>Because <b>an unreachable reference cancels out in a comparison</b>. Two
airports measured against the <i>same</i> impossible optimum remain comparable
with each other: the distance between them does not depend on the
unreachability, which is common to both.</p>
<p>That is why none of the rankings on this site use the absolute value; they
use the <b>Δ norm</b>: the deviation from the European median of flights of
<i>the same length and the same aircraft type</i>. The theoretical optimum only
serves as a shared unit of measurement.</p>
<p>This correction is necessary, not cosmetic: across the ranked routes the raw
gap correlates about <b>{_r_raw:+.2f}</b> with sector length, so a raw ranking
would order routes by shortness rather than by inefficiency. After normalisation
the residual correlation with distance is about <b>{_r_res:+.2f}</b>.</p>
<p>The comparison is also made <b>at equal aircraft type</b>. An A320 and a B767
on the same sector are not comparable, and without this second normalisation
part of what the method charges to the route would really be the aircraft
serving it: the question here is route and profile efficiency, not fleet choice.
Where a distance–type combination has fewer than 200 flights, the distance-only
norm is used instead.</p>

<h2>4. Data and tools</h2>
<ul>
<li><b>Trajectories</b>: public daily dumps from
<a href="https://adsb.lol">adsb.lol</a>, ODbL licence. Every flight is
reconstructed from the ADS-B messages broadcast by the aircraft themselves.</li>
<li><b>Wind</b>: <b>ERA5</b> reanalysis (Copernicus/ECMWF), 11 pressure levels,
hourly resolution.</li>
<li><b>Fuel burn</b>: <a href="https://openap.dev">OpenAP</a> (TU Delft), an open
aircraft performance model.</li>
<li><b>Anchoring</b>: cruise fuel flows per type are anchored to the <b>ICAO
Carbon Emissions Calculator Methodology v13.1</b>, Appendix C.</li>
</ul>

<h3>Wind is what makes the two directions comparable</h3>
<p>Actual fuel burn is already wind-correct, because it derives from measured
airspeed. The ideal flight is not: timed without wind, the same route comes out
artificially efficient one way and inefficient the other. The ideal flight is
therefore timed at the ground speed corrected with ERA5 wind along the path, and
the asymmetry between the two directions cancels.</p>

<h3>Two baseline choices that change the result</h3>
<ul>
<li>The optimal cruise altitude is the one for the <b>great-circle</b> distance,
not for the distance actually flown: otherwise a detour would quietly earn
itself a better cruise level and the lateral component would deflate.</li>
<li>The wind along the real track is weighted by <b>distance</b>, not by time:
the track is time-sampled, so it is dense where the aircraft is slow, and a
time-weighted average would over-weight the terminal areas.</li>
</ul>

<h3>Prior work on the same data and model</h3>
<p>Open ADS-B trajectories and an open performance model have been combined for
European traffic before. In <i>PLOS ONE</i> in 2023,
<a href="https://doi.org/10.1371/journal.pone.0287612">Olive, Sun, Basora and
Spinielli</a> took two months of 2019 arrivals at five European airports and
measured what holding patterns, point merge procedures and continuous descent
operations cost in fuel; OpenAP, the performance model used here, is the work of
one of them.</p>
<p><b>The quantity is not the same as the one on this site.</b> They compare real
flights with and without a given procedure at the same airport, so their
reference is other traffic. Here the reference is a wind-corrected optimal
profile that no flight flies. The two sets of figures do not sit next to each
other.</p>

<h2>5. Calibration</h2>
<p>OpenAP fuel flows are compared per type against values derived from ICAO, and
corrected with a factor for types deviating by more than 10% with at least 100
observed flights. The factor multiplies both the real flight and its ideal, so
it <b>cancels in the percentages</b>: it affects tonnages, not percentage
gaps.</p>
<p>The check that matters is not on calibrated types — for those it is
tautological — but on the <b>uncalibrated</b> ones: A320, A321, B738 and A319,
which alone are the majority of flights, land within 5% of the ICAO reference
with no correction at all.</p>

<h2>6. Which flights are included</h2>
<p>A flight enters the analysis only if its track is sufficiently complete:
adequate coverage and no large time gaps.</p>
<p>There is then a criterion that is often misread, so it is worth being
explicit. We discard flights whose flown distance comes out <b>smaller</b> than
90% of the great circle. Flying less than the direct route is geometrically
impossible: when it happens it is because the track is <b>truncated</b> by a
reception gap, and that flight would look more efficient than possible.
<b>We do not discard heavily diverted flights</b> — those have flown distance
<i>greater</i> than the great circle and all remain in the sample, including the
routes at the top of the rankings.</p>
<p>Over the published period: <b>{len(df):,} flights</b> across {len(days)} days
and {len(months)} months, ECAC area.</p>

<h2>7. Validations</h2>

<h3>Is the wind modelled correctly?</h3>
<p>If it were not, the same route would come out different in the two
directions. We therefore measure the spread between outbound and return on every
route with at least 10 flights per direction. With wind modelled, the median of
that spread collapses, and it stays <b>stable across seasons</b>. That is the
real test, because winter jet streams are far stronger.</p>
<div class=scroll><table><thead><tr><th>month</th><th class=num>routes</th>
<th class=num>without wind</th><th class=num>with wind</th></tr></thead><tbody>
{gate_rows}
</tbody></table></div>

<h3>Is the signal structural, or is it weather?</h3>
<p>If the rankings were noise, they would reshuffle every month. Comparing the
route ranking across all <b>{STAB['pairs']} available month pairs</b>, rank
correlation stays high throughout: median <b>{STAB['median']:.3f}</b>, worst
<b>{STAB['worst']:.3f}</b> ({STAB['worst_pair']}), consecutive months
{STAB['consec']:.3f}.</p>
<p><b>These two checks were computed before the ground-fuel exclusion</b>, on the
gate-to-gate figures this site no longer publishes, and they are re-run at the
next release. The direction is known: taxi burn is structural per airport and
stable month to month, so leaving it in could only have flattered a stability
measured across months — the corrected figures are, if anything, harder to keep
stable than the ones these numbers describe.</p>
<p>The more informative detail is that the correlation <b>decays in order</b>
with the time distance between months. That is the signature of a structural
signal with modest seasonal drift: noise would give low correlations everywhere,
an artefact would give uniformly high ones.</p>

<h3>Do the numbers survive an external comparison?</h3>
<p>Aggregating our trajectories <b>the way EUROCONTROL aggregates its own KEA
indicator</b> — a ratio of sums, over the en-route portion beyond 40 NM from the
airports — we obtain <b>+{kea:.2f}%</b> against the
<b>~{BENCH['kea_published']:.0f}%</b> published. Same order of magnitude and
same construction.</p>
<p>KEA is not merely a published statistic: it is the only environmental
indicator on which the Single European Sky performance scheme sets binding
targets for Member States. For the current reference period, RP4, the
Union-wide target falls from <b>{BENCH['kea_rp4_start']:.2f}% in 2025 to
{BENCH['kea_rp4_end']:.2f}% in 2029</b>, and measured performance has been
running above target.</p>
<p><b>Our figure is lower than the published one, and the reason is the flight
population rather than the arithmetic.</b> KEA covers <i>every</i> flight
crossing the reference area, including overflights, counted over their in-area
portion; the 40 NM exclusion applies only around departure and arrival airports,
so an overflight has none removed. We count only flights that both take off and
land inside our area, so every flight we measure has had both terminal cylinders
cut out — precisely the phase where route extension is greatest. EUROCONTROL
also discards the ten best and ten worst days of the year, and we do not. These
differences all push the same way, and they are enough to explain the gap
without either figure being wrong.</p>
<p>Further differences we cannot remove: they use radar data over the
EUROCONTROL reference area, we use ADS-B over a quality-filtered subset, with
our own baseline and criteria. The comparison says "consistent", not
"identical", and it should not be read as reproducing their number.</p>

<h3>Where this baseline sits among EUROCONTROL's reference trajectories</h3>
<p>The kind of reference used here is not particular to this project.
EUROCONTROL's Performance Review Report 2024 is developing a ladder of
comparison trajectories: the great circle route; then a <i>theoretical
fuel-optimal trajectory</i>, which "follows the great circle route but is
further optimised for wind conditions"; then a realistic optimal trajectory,
which adds weather phenomena such as thunderstorms and turbulence; and finally
an <i>ATM fuel-optimal trajectory</i>, which folds in ATM and network
constraints.</p>
<p><b>The baseline on this site is not any one of those rungs.</b> It sits
between the first two: it holds to the great circle laterally, like the first,
and optimises the profile in real wind, like the second. The difference is that
their wind optimisation may also move the route sideways to use the wind. Their
own worked example does exactly that, gaining time over a longer path; ours may
not. The consequence has a known direction, and it does not rest on that
example: an optimiser free to leave the great circle can always choose to stay
on it, so its optimum can only burn the same or less. Measured against a
laterally free version of this reference, the gap reported here could only
grow.</p>
<p>What matters for the caveat at the top of this site is where the reference
sits rather than which rung it is. It sits upstream of the levels at which
weather and network constraints enter, which is why the figure is a distance
from a theoretical optimum rather than an estimate of recoverable fuel. Those
constraints are real, and something that measures against a reference placed
before them is not measuring what could be recovered.</p>
<p>The same report idealises the cruise, and in the opposite direction to this
one. Its optimiser lets the cruise climb continuously as the aircraft burns off
mass, off the discrete levels flights are actually assigned, and EUROCONTROL
notes an intention to constrain it to the Flight Level Allocation System in
future updates. A reference built that way is cheaper than anything a flight can
be given, so a gap measured against it would run correspondingly large, which is
presumably why they plan to constrain it. The baseline here errs the other way,
cruising below what aircraft reach on the longest sectors, which makes the
figure on this site too small. Correcting it would make the headline figure
larger, and that correction is still owed.</p>

<h2>8. Stated limitations</h2>
<ul>
<li>We measure the gap from a <b>theoretical</b> optimum, not avoidable
inefficiency (§2).</li>
<li><b>Taxi and ground movement are outside every figure here.</b> The model
that prices fuel is an aerodynamic model of flight, and an aircraft on the
ground is far outside its domain: asked for an airliner at taxi speed it
returns several times the fuel flow it gives for cruise. The reference
trajectory never taxis either, so the comparison is flight against flight and
both sides exclude the ground. One consequence is that the CO&#8322; total on
this site is <b>CO&#8322; emitted in flight</b>, and understates what the same
traffic actually emitted: taxi burns real fuel, and pricing it needs reference
values this method does not have.</li>
<li><b>Only the tails of the rankings are reliable.</b> Half the routes sit
within a few points of the norm, inside the uncertainty of the method: between
900th and 1000th place the ordering means nothing. Rankings show only routes
with at least {RANK_MIN_N} flights.</li>
<li>The period is <b>2026 only, January to July</b>: no year-on-year comparison,
and December is not covered.</li>
<li><b>Four days are missing</b> inside the period, all absent at the source.
The window ends on 20 July because the four days that follow have flight data
but not yet the wind data the comparison needs.</li>
{coverage_note(days)}
<li>Routes flagged ⚑ <b>cannot</b> fly the direct path because the airspace is
closed. The ban applies to European carriers and not to third-country ones, so
the figure shown is an average between those who must divert and those who need
not.</li>
<li>ADS-B coverage <b>does not include oceanic sectors</b>.</li>
<li>Aircraft mass is estimated, not known: it is the main physical uncertainty
in the model. The baseline also does not impose the <b>altitude reachable at
full load</b>: a heavy aircraft must climb in steps, whereas the ideal
trajectory flies the whole cruise at a single level. How much this weighs is
measured by the floor in §2.</li>
<li>The ideal trajectory flies at <b>minimum-fuel speed</b>. Airlines fly faster
on purpose, to meet schedules: that is an economic choice, not an inefficiency,
and it still ends up counted in the vertical component. It is one of the items
making up the incompressible floor.</li>
<li><b>The split between lateral and vertical is a convention. The total does not
depend on it; the split itself does.</b> We correct the route first and the profile second,
charging the extra kilometres at the <i>optimal</i> profile. The opposite
convention charges them at the flight's <i>actual</i> CO<sub>2</sub> per kilometre,
which moves weight towards the lateral component: for the <b>median flight</b> the
split goes from {conv_fleet[0]:.1f} / {conv_fleet[1]:.1f} to
{conv_fleet[2]:.1f} / {conv_fleet[3]:.1f} points. <b>{conv_ord}</b>, including for
the airport named in the findings
({conv_ap1[0]:.1f} / {conv_ap1[1]:.1f} becomes {conv_ap1[2]:.1f} / {conv_ap1[3]:.1f},
on raw medians rather than deviations from the norm). The total is identical under
both conventions <i>for every individual flight</i>. The four figures above are
medians, and medians do not add: summing each pair gives a different number again,
and neither sum is the median total. That is a property of medians, not a
discrepancy.<br>
<b>Those four pairs describe the median flight, and are not the
{lat_w:.1f} / {vert_w:.1f} of the headline.</b> The headline is a ratio of sums over
the whole period, where a long flight weighs more than a short one; the median
flight is a different quantity and comes out lower. Both appear on this site, and
every comparison in the rankings uses the aggregate.
Anything resting on the <i>size</i> of the split should be read with this
sensitivity in mind; the ordering does not change.</li>
<li>The ideal trajectory's flight time uses the <b>harmonic mean of ground
speed</b> along the path, which is the quantity that reproduces the correct
flight time when wind varies. An earlier version used the arithmetic mean of
wind, which understated the baseline's fuel and inflated the published gap by
about 0.35%; the figures on this site are computed after that correction.</li>
</ul>

<h2 id=privacy>9. Privacy</h2>
<p>Every published row aggregates <b>at least {MIN_N} flights</b>. We do not
publish, and will not publish, data about an individual flight, aircraft or
operator. The rankings concern routes and airports, never people or
identifiable aircraft.</p>

<p><b>That floor counts flights, not aircraft — and on one class of traffic the
difference matters.</b> This site covers commercial air transport. Business and
general aviation are not its subject, and only two such aircraft types survive
the performance model at all ({biz_types}, together {non_airliner_pct:.2f}% of
flights). But on a thin route a row made of those flights can describe one or two
aircraft — that is, one operator or one owner — even while clearing a floor of
{MIN_N} flights.</p>

<p>We cannot rule that out by counting aircraft, because <b>we deliberately do
not store aircraft identity</b>: the same choice that makes this dataset unable
to identify a flight also removes the check that would prove those rows safe. So
the class is removed instead. <b>{n_biz_routes} routes whose traffic is majority
business aviation are excluded from every table and chart on this site.</b> Their
flights remain in the European totals, where they are diluted across {len(df):,}
flights and identify nobody; what is suppressed is the row that would have
singled them out. No airport comes close to the same threshold: the most exposed
sits below 6%.</p>

<h2>10. Who made this, and how to report an error</h2>
<div class=note>
<p><b>Everything here is reproducible.</b> The trajectory data is public, the
performance model is open source, and the code that turns one into the other is
published: any figure on this site can be recomputed and any choice made along
the way can be inspected. What you find here is a <i>tool</i> with its
limitations stated, not an authored study.</p>
<p>I am not an aviation professional or a climate scientist; I run an ADS-B
receiver and I care about this. <b>The method, the modelling and the code were
built with AI assistance</b> (Claude); the constraints are mine — what the figures cover, when they change, and what this project declines to claim. The analytical
decisions it implements are documented on this page precisely so they can be
checked rather than taken on trust.</p>
<p><b>Right of reply.</b> If a figure looks wrong to you, or if you represent
an airport, an airline or an air navigation service provider named here, write
to <a href="mailto:hello@co2gap.org">hello@co2gap.org</a>. Corrections and
replies are published in full on the <a href="replies.html">replies page</a>,
alongside the figure each one concerns. It is why this work is public rather than
private.</p>
</div>

<h2 id=independence>11. Independence</h2>
<p>This site names airports and air navigation service providers. Some of them
may one day ask for the detail behind their own figures, and that would put the
measurement and the interest of the measured party in the same hands. The rules
below exist so that the arrangement can be checked rather than trusted, and they
apply from the first release, while the number of such arrangements is zero.</p>
<ol>
<li><b>The public figure is never a deliverable.</b> What could be provided to an
organisation is analysis <i>of</i> a published figure — never a change <i>to</i>
it, and never its removal or postponement.</li>
<li><b>No privileged access, embargo or preview.</b> Where material is provided
before publication — as it was, on request, to one of the organisations notified —
the same is available on the same terms to anyone named here, and it confers no
ability to alter what is published. The two airports
whose figures the findings single out, and the air navigation service provider
responsible for each, were written to before the first publication, on the same
terms and with the same lead time, with no ability to alter what was published.
Every other organisation named anywhere on this site — in a finding, a chart or
a table — is covered by the right of reply below, which is unconditional and
carries no notice period. Notice is given because being singled out deserves
warning, never as a commercial courtesy.</li>
<li><b>Right of reply is free and unconditional</b>, published in full and on
identical terms whether or not there is any other relationship.</li>
<li><b>Any commercial relationship with a named organisation is disclosed next
to that organisation's figure</b>, for as long as the figure is published.</li>
<li><b>No grades, tiers or composite scores are sold or published.</b> What this
project produces is measured quantities; a score would
compress exactly the caveats that section 8 says must travel with the number.</li>
</ol>
<p>If you operate an airport, an ANSP or an airline and want the detail behind
these figures for your own traffic — by hour of day, by origin, by aircraft type,
month by month — write to
<a href="mailto:hello@co2gap.org">hello@co2gap.org</a>. That detail exists in the
pipeline but is not published, because a page that showed every cut of the data
would state far more than the sample in each cut can support.</p>

<h2 id=licence>12. Licence and reuse</h2>
<p>Three different things on this site carry three different licences, and the
distinction matters if you intend to republish.</p>
<ul>
<li><b>The figures, tables and charts on these pages</b> are a <i>Produced Work</i>
in the sense of ODbL: reuse them freely, including commercially, with attribution
to this site and to
<a href="https://adsb.lol">adsb.lol</a> contributors. Share-alike is not
triggered.</li>
<li><b>A dataset extracted or reconstructed from these pages</b> — a table of
routes or airports with their figures, redistributed as data — is a
<i>Derivative Database</i>. ODbL requires it to be published under
<a href="https://opendatacommons.org/licenses/odbl/">ODbL</a> as well. This is the
upstream licence's requirement, not an additional condition imposed here.</li>
<li><b>The text and the charts on these pages</b> are additionally offered under
<a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a> — a grant
over this project's own expression, not over the underlying data, whose terms are
the ODbL ones above. The <b>tables</b> are not included: extracting them as data
makes a Derivative Database. The
<a href="https://github.com/co2gap/co2gap">pipeline source code</a> is
Apache-2.0. The project name and domain are not covered by either.</li>
</ul>
<p>Wind data: ERA5, Copernicus Climate Change Service. Airport names and
positions: OurAirports (CC0). Fuel references: ICAO CEC Methodology v13.1.
Performance model: OpenAP, TU Delft (LGPL-3.0). Each release is
archived on Zenodo as they are published, so that a figure can be cited against the version that produced
it; the identifier for a given release, and what changed in it, are on the
<a href="releases.html">release history</a> page.</p>
{citation(days)}

<h2 id=glossary>13. Glossary</h2>
<p>Every term used on this site, in one sentence each. No prior knowledge of aviation
is assumed — if something here is still unclear, that is a fault of this page and worth
an email.</p>
<dl class=gloss>{glossary_rows}</dl>

<p class=foot>
{FOOTNAV}<br>
Trajectory data © <a href="https://adsb.lol">adsb.lol</a> contributors, licensed
under <a href="https://opendatacommons.org/licenses/odbl/">ODbL</a>. These pages are
a Produced Work: reuse with attribution, no share-alike. A dataset extracted from
them is a Derivative Database and stays under ODbL —
<a href="methodology.html#licence">what that means</a>.
Wind: ERA5, Copernicus Climate Change Service.
Airports: OurAirports (CC0).
Fuel references: ICAO CEC Methodology v13.1.
Performance model: OpenAP, TU Delft.<br>
<b>Release {RELEASE}</b> · methodology v{METHOD_VERSION} · updated twice a year over a 12-month window ·
next update {NEXT_RELEASE}, covering {NEXT_WINDOW} — <a href="releases.html">what changes</a>.<br>
{len(df):,} flights · {len(days)} days · {n_routes_all:,} publishable routes ·
{n_routes_rank:,} ranked · {n_airports:,} airports · generated {esc(gen)}.<br>
Contact <a href="mailto:hello@co2gap.org">hello@co2gap.org</a> ·
{SOCIAL}
</p>

</div></body></html>
"""


def main():
    df = load()
    names = airport_names()
    coords = {}
    if AIRPORTS.exists():
        with open(AIRPORTS, newline="") as f:
            for r in csv.DictReader(f):
                coords[r["icao"]] = (float(r["lat"]), float(r["lon"]))

    def aname(icao):
        n = names.get(icao, icao)
        for junk in (" International Airport", " Airport", " International"):
            n = n.replace(junk, "")
        return n

    days = sorted(df.day.unique())
    months = sorted({d[:7] for d in days})

    # ---- headline -------------------------------------------------------
    co2_t = df.co2_real_kg.sum() / 1000
    ideal_t = df.co2_ideal_kg.sum() / 1000
    excess_t = co2_t - ideal_t
    # Same formulas as lab/decompose_report.py, deliberately: the site and the
    # phase-2b report must not print different numbers for the same quantity.
    # Both use the UNCALIBRATED fuel here — the calibration re-weights aircraft
    # types inside a ratio of sums and shifts the result by ~0.2 points. Which
    # basis is the right one is an open question recorded in the report; what is
    # not open is that the two artefacts must agree.
    real_u, ideal_u, hyb_u = (df.co2_kg_v0.sum(), df.ideal_gc_co2_kg.sum(),
                              df.hybrid_co2_kg.sum())
    lat_w = (hyb_u - ideal_u) / ideal_u * 100
    vert_w = (real_u - hyb_u) / ideal_u * 100
    # Quale delle due componenti sia la maggiore e' un FATTO che cambia con i
    # dati: affermarlo nel testo significa vederlo invertirsi in silenzio.
    _hi, _lo = ('vertical', 'lateral') if vert_w > lat_w else ('lateral', 'vertical')
    _ratio = max(vert_w, lat_w) / max(min(vert_w, lat_w), 1e-9)
    _where = ('how flights climb, cruise and descend' if vert_w > lat_w
              else 'how far flights go')
    vert_vs_lat = (f'The {_hi} component is about {_ratio:.1f} times the {_lo} one: '
                   f'the gap is mostly in {_where}.')
    # KEA is weighted by the EN-ROUTE great circle, not the full one: the
    # indicator only describes the portion outside the 40 NM cylinders.
    enr = df.dropna(subset=["dist_ratio_enroute"])
    gc_enr = enr.flown_enroute_km / enr.dist_ratio_enroute
    kea = (enr.flown_enroute_km.sum() - gc_enr.sum()) / gc_enr.sum() * 100

    # ---- structural floor vs operational margin -------------------------
    # Empirical attribution, from the falsification tests suggested by an
    # adversarial review: a flight routed direct, departing into an empty
    # night sky, on a sector where cruise dominates, is about as close to the
    # baseline as a real airliner gets. Whatever gap REMAINS there is not
    # congestion and not routing — it is the baseline being unreachable (cost
    # index, step climbs imposed by mass, discrete flight levels). The rest is
    # the operationally influenced margin.
    hour = pd.to_datetime(df.dep_ts, unit="s", utc=True).dt.hour
    floor_mask = (df.dist_ratio < 1.02) & hour.isin([1, 2, 3, 4]) & (df.gc_km > 1000)
    vert_floor = float(df.loc[floor_mask, "excess_vertical_pct"].median())
    vert_fleet = float(df.excess_vertical_pct.median())
    vert_oper = vert_fleet - vert_floor
    # ⚠️ Le due coorti non hanno la stessa lunghezza: il floor si misura sopra i
    # 1.000 km, la mediana di flotta su tutte le tratte. Sottrarle mette nel
    # "margine operativo" anche il mix di distanza, che non e' operativo. La
    # differenza a parita' di distanza e' molto piu' piccola, e va detta: senza,
    # la stat card afferma piu' di quanto sia stato misurato.
    vert_long = float(df.loc[df.gc_km > 1000, "excess_vertical_pct"].median())
    vert_oper_eq = vert_long - vert_floor
    conv_fleet = convention_medians(df)
    n_floor = int(floor_mask.sum())

    # ---- peer-based counterfactual --------------------------------------
    # The theoretical optimum is unreachable, so a "savings" figure built on it
    # would be wrong. This one is built on what comparable flights ALREADY
    # achieve: the median of same-length flights. Only flights ABOVE it count —
    # you cannot bank the surplus of the ones already below.
    ideal_cal = df.co2_ideal_kg.to_numpy()
    above = np.clip(df.d_tot.to_numpy(), 0, None) / 100.0 * ideal_cal
    q75 = float(np.quantile(df.d_tot, 0.75))
    worst_q = np.where(df.d_tot > q75, (df.d_tot - q75) / 100.0 * ideal_cal, 0.0)
    yr = 365 / len(df.day.unique())
    sc_a = above.sum() / 1e9 * yr          # Mt CO2/anno
    sc_b = worst_q.sum() / 1e9 * yr
    sc_a_fuel_kt = above.sum() / 3.16 / 1e6 * yr

    # ---- routes ---------------------------------------------------------
    df["pair"] = [tuple(sorted(x)) for x in zip(df.origin_icao, df.dest_icao)]
    g = df.groupby("pair").agg(
        n=("d_tot", "size"), gc=("gc_km", "median"),
        d=("d_tot", "median"), lat=("excess_lateral_pct", "median"),
        vert=("excess_vertical_pct", "median"), co2_t=("co2_real_kg", "sum"),
    )
    # Rotte a maggioranza aviazione d'affari: si sopprimono le RIGHE, non i voli.
    df["_nb"] = df.typecode.isin(NON_AIRLINER)
    g["biz"] = df.groupby("pair")._nb.mean()
    n_biz_routes = int(((g.biz >= NON_AIRLINER_MAX) & (g.n >= MIN_N)).sum())
    non_airliner_pct = float(df._nb.mean() * 100)
    g = g[g.biz < NON_AIRLINER_MAX]
    g_all = g[g.n >= MIN_N]      # everything publishable (privacy floor)
    g = g[g.n >= RANK_MIN_N]     # only these enter the rankings
    g["co2_t"] /= 1000
    g["closed"] = [", ".join(gc_crosses_closed(a, b, coords)) for a, b in g.index]

    # ---- airports -------------------------------------------------------
    # A flight is counted at BOTH ends, and its gap is measured over the WHOLE
    # flight — so a share of what appears under one airport was produced at the
    # other, and a share of it in cruise, far from either. No arithmetic here
    # removes that: the pipeline has no phase-resolved excess, only totals per
    # flight. What it can do is TEST it, by splitting the same figure by the
    # role the airport played. If a figure were inherited from the airports it
    # connects to, one of the two sides would sit near the norm; when both are
    # high, the deviation travels with the airport and not with its partners.
    # (Same split already produced per airport by lab/export_airport.py.)
    # Where inside the flight the gap sits, when the phase split has been run.
    # The fallback text is the older admission that we could not tell, so the
    # page never claims more than the data behind it supports.
    pa = phase_attribution(df)
    if pa is None:
        phase_note = (
            "<b>What no column here can do is locate the gap inside the "
            "flight.</b> These data do not say how many points happened in the "
            "descent, or in the departure sequence, or in the cruise between "
            "them. A high figure says that flights touching this airport "
            "deviate from comparable ones; it does not say who or what "
            "produced the deviation.")
    else:
        # La chiusa INTERPRETA i quattro numeri appena dati, quindi non puo'
        # essere una frase fissa. Fino al 2026-08-28 diceva che il resto del
        # volo conta poco, ed era vera quando i due capi si somigliavano (79% e
        # 84%, col rullaggio dentro). Tolto il rullaggio gli arrivi tengono e le
        # partenze no, e una frase scritta a mano sarebbe rimasta vera per
        # abitudine: qui la direzione e' letta dai dati, e se si inverte di
        # nuovo si inverte anche il testo.
        hi, lo = (("arrivals", "departures") if pa["arr_own"] >= pa["dep_own"]
                  else ("departures", "arrivals"))
        # Due rami, tre casi. `min < 50` NON garantisce `max >= 50`: se un
        # domani scendessero entrambi, il primo ramo direbbe "most ... is
        # produced within 40 NM" del capo maggiore, che sarebbe falso. E il
        # ramo simmetrico non segue da `min >= 50`. Si separano.
        _hi_own = max(pa["arr_own"], pa["dep_own"])
        _lo_own = min(pa["arr_own"], pa["dep_own"])
        if _lo_own >= 50.0:
            asimmetria = (
                "Both ends behave alike: what happens at the far end of the "
                "flight, and in the cruise between the two, accounts for very "
                "little of what separates one airport from another.")
        elif _hi_own >= 50.0:
            asimmetria = (
                f"The two ends are not alike: most of what appears on an "
                f"airport's {hi} is produced within 40 NM of it, while most of "
                f"what appears on its {lo} is not, and is carried in from "
                f"further along the flight. EUROCONTROL's own figures point "
                f"the same way, putting the fuel recoverable by continuous "
                f"descent at around ten times that recoverable by continuous "
                f"climb.")
        else:
            asimmetria = (
                f"Neither end accounts for most of what it is charged with: on "
                f"both roles the larger share of an airport's deviation is "
                f"produced elsewhere in the flight, {hi} included.")

        phase_note = (
            "<b>Where inside the flight does it sit?</b> Splitting the same "
            "vertical gap by the part of the path it was burnt on gives a "
            "sharper answer than the two columns alone. Measured across the "
            f"{pa['n_dep']} airports whose departures deviate by at least two "
            "points, a median of "
            f"<b>{pa['dep_own']:.0f}%</b> of that deviation was produced within "
            "40 NM of the airport itself, and "
            f"<b>{pa['dep_climb']:.0f}%</b> of it in the climb. For arrivals "
            f"({pa['n_arr']} airports) it is "
            f"<b>{pa['arr_own']:.0f}%</b> within 40 NM and "
            f"<b>{pa['arr_desc']:.0f}%</b> in the descent. {asimmetria}<br><br>"
            "<b>That is a location, not a cause.</b> Where it does sit near an "
            "airport, it says the fuel was burnt there, in the climb out of it "
            "or the descent into it. It does not say whether the profile was "
            "chosen by the operator or imposed by the traffic, and nothing here "
            "distinguishes the two.")

    both = pd.concat([df.assign(ap=df.origin_icao, role="dep"),
                      df.assign(ap=df.dest_icao, role="arr")])
    ga = both.groupby("ap").agg(
        n=("d_tot", "size"), d=("d_tot", "median"),
        lat=("d_lat", "median"), vert=("d_vert", "median"),
    )
    by_role = both.pivot_table(index="ap", columns="role", values="d_tot",
                               aggfunc="median")
    ga["dep"], ga["arr"] = by_role["dep"], by_role["arr"]
    ga = ga[ga.n >= MIN_N_AIRPORT]

    # ---- numbers behind the findings section ----------------------------
    # Computed, never typed by hand: a findings paragraph that drifts from the
    # table under it is the fastest way to lose a reader.
    top_ap = ga.sort_values("d", ascending=False)
    best_ap = ga.sort_values("d")
    ap1, ap1r = top_ap.index[0], top_ap.iloc[0]
    apb, apbr = best_ap.index[0], best_ap.iloc[0]
    top20 = g.sort_values("d", ascending=False).head(20)
    ap1_routes = sum(1 for a, b in top20.index if ap1 in (a, b))
    # Base rate: a hub with hundreds of qualifying routes is over-represented in
    # ANY tail, so the count above means nothing without the share it starts from.
    ap1_all_routes = sum(1 for a, b in g.index if ap1 in (a, b))
    ap1_share = 100.0 * ap1_all_routes / len(g) if len(g) else 0.0
    conv_ap1 = convention_medians(df[(df.origin_icao == ap1) | (df.dest_icao == ap1)])
    ap2, ap2r = top_ap.index[1], top_ap.iloc[1]
    ap3, ap3r = top_ap.index[2], top_ap.iloc[2]
    ap_med = float(ga.d.median())
    # Two examples for the attribution note, chosen by the PROPERTY they show
    # and never by rank: one airport whose two roles agree (the deviation
    # travels with the airport) and one where they diverge (the combined figure
    # alone would not have told you which side it came from). Picking these by
    # position instead would eventually put an airport under a sentence that
    # says the opposite of its own numbers.
    shown_ap = top_ap.head(15).assign(spread=lambda x: (x.dep - x.arr).abs())
    sym_ap, asym_ap = shown_ap.spread.idxmin(), shown_ap.spread.idxmax()
    symr, asymr = ga.loc[sym_ap], ga.loc[asym_ap]
    # Monthly behaviour of the two leaders.
    #
    # The rank alone is misleading and nearly cost us a wrong claim: the second
    # airport moves between 1st and 12th across the months, which reads as an
    # unstable signal. It is not — it is an unstable RANK. At the top of this
    # table ten places are separated by a handful of points, so the ordinal
    # position carries far less information than its movement suggests. What is
    # stable is the distance from the median, which is what we report.
    mrank, m2rank, m2pctile, m2margin, m2val, head_span = [], [], [], [], [], []
    for mth, sm in both.assign(month=both.day.str[:7]).groupby("month"):
        tm = (sm.groupby("ap").agg(n=("d_tot", "size"), d=("d_tot", "median"))
              .query("n >= 300").sort_values("d", ascending=False))
        if not len(tm):
            continue
        idx, vals = list(tm.index), tm.d.values
        mmed = float(np.median(vals))
        if len(vals) >= 10:
            head_span.append(float(vals[0] - vals[9]))
        if ap1 in idx:
            mrank.append(idx.index(ap1) + 1)
        if ap2 in idx:
            r2 = idx.index(ap2) + 1
            m2rank.append(r2)
            m2pctile.append(100.0 * r2 / len(idx))
            m2margin.append(float(tm.loc[ap2, "d"]) - mmed)
            m2val.append(float(tm.loc[ap2, "d"]))
    ap1_worst_rank = max(mrank) if mrank else 0
    ap2_worst_rank = max(m2rank) if m2rank else 0
    ap2_best_rank = min(m2rank) if m2rank else 0
    ap2_worst_pctile = max(m2pctile) if m2pctile else 0.0
    ap2_min_margin = min(m2margin) if m2margin else 0.0
    ap2_hi, ap2_lo = (max(m2val), min(m2val)) if m2val else (0.0, 0.0)
    # Points separating 1st place from 10th, in the month where the head of the
    # table is most compressed — the honest measure of how little an individual
    # position at the top means.
    head_span_min = min(head_span) if head_span else 0.0
    n_months_above = sum(1 for m in m2margin if m > 0)
    # the single most detoured route whose great circle crosses closed airspace
    # Routes across closed airspace are thin by nature — the Baltic pairs run a
    # few dozen flights over the period — so they get their own floor rather
    # than the ranking one, and the count is shown next to the claim.
    gx = df.groupby("pair").agg(n=("d_tot", "size"),
                                enr=("dist_ratio_enroute", "median"))
    gx = gx[gx.n >= MIN_N_CLOSED]
    gx["closed"] = [", ".join(gc_crosses_closed(a, b, coords)) for a, b in gx.index]
    gcl = gx[gx.closed != ""].sort_values("enr", ascending=False)
    if len(gcl):
        kal_pair, kal_pct = gcl.index[0], (gcl.iloc[0]["enr"] - 1) * 100
        kal_n = int(gcl.iloc[0]["n"])
    else:
        kal_pair, kal_pct, kal_n = ("", ""), 0.0, 0

    band = df.groupby("bin").agg(n=("d_tot", "size"),
                                 med=("excess_total_pct", "median"),
                                 lo=("gc_km", "min")).sort_values("lo")

    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ap1_name, apb_name = esc(aname(ap1)), esc(aname(apb))
    ap1_d, ap1_n = ap1r.d, int(ap1r.n)
    ap1_v, ap1_l = ap1r.vert, ap1r.lat
    apb_d = apbr.d
    kal_a = esc(aname(kal_pair[0])) if kal_pair[0] else ""
    kal_b = esc(aname(kal_pair[1])) if kal_pair[1] else ""

    def rrow(r, pair):
        a, b = pair
        note = (f"<span class=flag title='the direct path crosses "
                f"{esc(r.closed)}'>⚑</span>" if r.closed else "")
        return (f"<tr><td class=r>{esc(aname(a))} ↔ {esc(aname(b))}{note}"
                f"<span class=code>{esc(a)}–{esc(b)}</span></td>"
                f"<td class=num>{int(r.n):,}</td><td class=num>{r.gc:,.0f}</td>"
                f"<td class='num big {'pos' if r.d>0 else 'neg'}'>{r.d:+.0f}</td>"
                f"<td class=num>{r.lat:.0f}%</td><td class=num>"
                f"{'0%' if abs(r.vert) < 0.5 else f'{r.vert:.0f}%'}</td>"
                f"<td class=num>{r.co2_t:,.0f}</td></tr>")

    def arow(icao, r):
        return (f"<tr><td class=r>{esc(aname(icao))}"
                f"<span class=code>{esc(icao)}</span></td>"
                f"<td class=num>{int(r.n):,}</td>"
                f"<td class='num big {'pos' if r.d>0 else 'neg'}'>{r.d:+.1f}</td>"
                f"<td class=num>{r.dep:+.1f}</td>"
                f"<td class=num>{r.arr:+.1f}</td>"
                f"<td class=num>{r.lat:+.1f}</td>"
                f"<td class='num big {'pos' if r.vert>0 else 'neg'}'>{r.vert:+.1f}</td></tr>")

    worst = "\n".join(rrow(r, p) for p, r in g.sort_values("d", ascending=False).head(25).iterrows())
    best = "\n".join(rrow(r, p) for p, r in g.sort_values("d").head(15).iterrows())
    ap_worst = "\n".join(arow(i, r) for i, r in ga.sort_values("d", ascending=False).head(15).iterrows())
    ap_best = "\n".join(arow(i, r) for i, r in ga.sort_values("d").head(10).iterrows())
    by_co2 = "\n".join(rrow(r, p) for p, r in g.sort_values("co2_t", ascending=False).head(15).iterrows())
    bandrows = "\n".join(
        f"<tr><td>{esc(i)} km</td><td class=num>{int(r.n):,}</td>"
        f"<td class=num>{pct0(r.med)}</td></tr>" for i, r in band.iterrows())
    n_closed = int((g.closed != "").sum())

    # ---- i quattro risultati, scritti UNA volta e usati in DUE posti --------
    # In home va il titolo con l'attacco; il seguito, che e' dove stanno i
    # caveat, va nella metodologia sotto la propria ancora. Scriverli due volte
    # significherebbe vederli divergere al primo aggiornamento, e sarebbe il
    # caveat quello che resta indietro.
    # La conclusione f1 e' una TENDENZA, non una classifica: si calcola qui,
    # come tutto il resto della sezione. Nominare gli estremi senza la tendenza
    # darebbe a scali minuscoli un rilievo che il loro traffico non giustifica.
    _big = ga.nlargest(15, 'n')
    big_med = float(_big.d.median())
    rest_med = float(ga.drop(_big.index).d.median())
    big_top = _big.d.idxmax()
    big_top_d = float(_big.d.max())
    traf_r = float(np.corrcoef(np.log(ga.n.to_numpy()), ga.d.to_numpy())[0, 1])
    global OG_ALT
    OG_ALT = (f"co2gap — {len(df):,} flights, {co2_t/1e6:,.1f} Mt CO2 emitted in "
              f"flight, {excess_t/1e6:,.2f} Mt gap from the theoretical optimum")
    # L'escursione va DERIVATA: scritta a mano diceva 'no more than six points'
    # mentre la f3 della stessa pagina nominava Stavanger a -7,7. La cifra
    # battuta a mano e' sempre quella che mente.
    # ⚠️ 28/08 sera, secondo giro: derivarla non bastava. Era derivata dalla
    # MEDIANA degli aeroporti (-0,6) mentre la frase dice "above the norm", e la
    # norma e' lo zero del glossario. Stampava 7/7 con Stavanger a -7,63 due
    # findings piu' sotto. E per un "no more than X" il formato giusto e' il
    # CEIL, non il round: con un minimo a -7,02 il round dava 7 e la frase era
    # falsa di due centesimi, in silenzio.
    ap_span = int(np.ceil(ga.d.max()))
    ap_span_lo = int(np.ceil(-ga.d.min()))

    # In quale delle due componenti sta la deviazione di un aeroporto: e' un
    # FATTO che cambia con i dati, e fino a stasera era una frase fissa che
    # diceva "not in the route" anche per Jersey, che ha +3,0 di laterale
    # contro +2,7 di verticale. La soglia e' mezzo punto, cioe' la risoluzione
    # con cui le due cifre vengono stampate accanto.
    def _dove(l, v):
        if abs(v - l) < 0.5:
            return "is split almost evenly between the two"
        return "sits in the profile" if v > l else "sits in the route"

    # Concordanza e cautela dipendono dal conteggio: "1 ... have" era
    # sgrammaticato, e "a real concentration" poggiava su una rotta sola.
    _hanno = "has" if ap1_routes == 1 else "have"
    _quante = ("a single route" if ap1_routes == 1
               else f"{ap1_routes} routes")

    # Il formato :+.0f su un valore vicino allo zero produce "-0%", che al
    # lettore ostile dice "i voli battono l'ottimo irraggiungibile". Sotto il
    # mezzo punto il segno non e' informazione, e' un artefatto di stampa.
    _band_hi = pct0(float(band.iloc[-1].med)).lstrip("+")

    FINDINGS = [
        ("f1",
         "Busier airports deviate more than quieter ones, and by little: the "
         "relationship holds across the table, the margin does not.",
         f"""The fifteen busiest airports sit at <b>{big_med:+.1f} points</b> against
<b>{rest_med:+.1f}</b> for the other {len(ga)-15}, and the deviation rises with
traffic across the whole table (correlation {traf_r:+.2f} against the logarithm
of movements). The highest of the fifteen is {esc(aname(big_top))} at
{big_top_d:+.1f}. <b>No airport in the table sits more than {ap_span} points above the
norm, or more than {ap_span_lo} below it.</b> The airports
furthest from the norm are smaller ones: {esc(ap1_name)} at {ap1_d:+.1f} across
{ap1_n:,} movements, then {esc(aname(ap2))} at {ap2r.d:+.1f} &mdash; real
deviations, measured on traffic too thin to move the European total. The median
across all {len(ga)} airports is {ap_med:+.1f}. A point is one percentage point
of CO&#8322; relative to the ideal flight.""",
         f"""<b>These are not places in a league table.</b> At the head of the ranking ten
positions can be separated by as little as {head_span_min:.1f} points within a
single month, so an individual position there is not resolvable — the same
caution the methodology applies to the middle of the ranking applies to its top.
What is stable is the distance from the norm: across the {len(m2margin)} months
{esc(aname(ap2))} never leaves the top {ap2_worst_pctile:.0f}% of airports and
stays at least {ap2_min_margin:.1f} points above the median, even while its
nominal position moves between {ap2_best_rank} and {ap2_worst_rank}. Its
magnitude is seasonal ({ap2_hi:+.1f} in the strongest month, {ap2_lo:+.1f} in
the weakest); {ap1_name} is steadier, never falling below rank
{ap1_worst_rank}.<br>
The two do not deviate for the same reason. {ap1_name}'s gap {_dove(ap1_l, ap1_v)}
({ap1_l:+.1f} lateral, {ap1_v:+.1f} vertical); {esc(aname(ap2))}'s
{_dove(ap2r.lat, ap2r.vert)} ({ap2r.lat:+.1f} lateral, {ap2r.vert:+.1f}
vertical). A single figure per airport does not say which of the two it is.<br>
{ap1_routes} of the twenty routes furthest from the norm {_hanno} {ap1_name} at one
end, against a base rate of {ap1_share:.1f}% of all ranked routes — consistent
with a concentration, though it rests on {_quante} and a hub with many routes is
over-represented in any tail.
Where the gap sits in the profile, that shape is what dense terminal areas
produce: early descents, level segments, sequencing. ADS-B shows the profiles flown, not the noise
abatement rules, sequencing constraints or capacity limits that require them.
<b>This describes what these flights fly. It does not measure what the airports,
their airlines or their controllers could do differently.</b>"""),
        ("f2",
         "Closed airspace has a cost, and it is large where it bites.",
         f"""The clearest example is <b>{kal_a} ↔ {kal_b}</b>, flying <b>+{kal_pct:.0f}%</b>
further en route because the straight line between the two airports crosses
Kaliningrad. It runs {kal_n:,} flights over the period — below the {RANK_MIN_N}
needed to enter the rankings, and quoted here as an illustration of the
mechanism rather than as a placing.""",
         f"""The detour is geometric: it does not depend
on sample size. Baltic connections
towards Turkey route around Belarus and Ukraine for the same reason. In total
{n_closed} ranked routes have a direct path through closed airspace. None of
this is recoverable while those closures hold. And the overflight ban binds
European carriers but not third-country ones, so each figure is an average
across operators that must divert and operators that need not."""),
        ("f3",
         "The efficient end of the ranking is small and peripheral.",
         f"""{apb_name} sits at <b>{apb_d:+.1f} points</b>, followed by other Nordic and
island airports, {big_med - apb_d:.0f} points below where the fifteen busiest sit.""",
         """Light
traffic buys continuous descents and direct clearances. It is a measure of how
much congestion costs, not a target a hub could adopt."""),
        ("f4",
         "Most of this gap cannot be compressed — the part usually left out.",
         f"""Of the median flight's {vert_fleet:.1f} points of vertical gap,
<b>{vert_floor:.1f} remain for a
flight going direct through an empty night sky</b> — which we read as the baseline
staying out of reach rather than inefficiency, though nothing here separates the
two.""",
         f"""Only {vert_oper:.1f} points move with traffic,
routing and profile — and that subtraction compares two groups of different
length, since the floor is measured above 1,000 km. At equal distance the margin
is about {vert_oper_eq:.1f} points; the rest is the distance mix. The distinction
is the difference between "European aviation wastes X" and "between comparable
flights there is a spread of this size" — and only the second is something these
data support."""),
    ]
    finding_cards = "\n".join(
        f'<div class=card><h3>{i}. {t}</h3><p>{lead}</p>'
        f'<a class=more href="methodology.html#{slug}">The full reading →</a></div>'
        for i, (slug, t, lead, _) in enumerate(FINDINGS, 1))
    finding_sections = "\n".join(
        f'<h3 id={slug}>{i}. {t}</h3><p>{lead}<br>{rest}</p>'
        for i, (slug, t, lead, rest) in enumerate(FINDINGS, 1))

    html_doc = f"""<!doctype html>
<html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>co2gap — how far European flights sit from a theoretical optimum</title>
{meta("co2gap — how far European flights sit from a theoretical optimum", DESC_INDEX)}
<style>{STYLE_INDEX}</style></head><body>
{NAV}
<div class=wrap>

<div class=hero>
<p class=eyebrow>{term('ecac', 'ECAC area')} · {esc(days[0])} → {esc(days[-1])} · {len(days)} days</p>
<h1>How far European flights sit from a theoretical optimum</h1>
<p class=lede>Actual emissions and the <b>gap against an ideal flight</b>, computed
from the ADS-B trajectory of every flight, with a {term('gc', 'great-circle')} baseline
<b>corrected for wind</b> and split into a <b>lateral</b> component (the route)
and a <b>vertical</b> one (the profile).</p>

<div class=figure>
  <div class=n>{(lat_w+vert_w):.1f}%</div>
  <div class=u>more CO&#8322; than that ideal flight, across {len(df):,} flights</div>
</div>

<p class=shield>
<b>This is not fuel that could be saved.</b> The ideal great-circle flight at a
perfect profile is a theoretical limit no real flight can reach: separation
between aircraft, route structure, constrained airspace and arrival queues put
it out of reach. Published estimates of <i>recoverable</i> inefficiency are much
smaller — EUROCONTROL puts at roughly {BENCH['cco_cdo_kg']} kg per flight what continuous climb and
descent procedures would recover, against the roughly {vertical_kg_per_flight(df):.0f} kg of
vertical gap measured here. Those two figures are not rival estimates of one
quantity: theirs is measured against current practice and is recoverable by a
known procedure, ours against a theoretical optimum that no flight can fly.
<b>This site measures the distance from a theoretical optimum,
not avoidable waste.</b> That reference sits near the bottom of a ladder
EUROCONTROL is developing for the same purpose: two further rungs, one adding
weather and the next adding ATM and network constraints, before the ladder
reaches a trajectory the network could actually deliver.
The full comparison is in the methodology; how this
figure sits beside EUROCONTROL's own estimate of what is recoverable, and how
much aviation weighs in the first place, is on the
<a href="context.html">context page</a>.
</p>

<div class=stats>
  <div class=stat><div class=v>{len(df):,}</div><div class=l>flights analysed</div></div>
  <div class=stat><div class=v>{co2_t/1e6:,.1f} Mt</div><div class=l>CO&#8322; emitted in flight</div></div>
  <div class=stat><div class=v>{excess_t/1e6:,.2f} Mt</div><div class=l>gap from the theoretical optimum</div></div>
  <div class=stat><div class=v>{len(g_all):,}</div><div class=l>routes with n≥{MIN_N}</div></div>
</div>

<div class=howto>
<h3>How to read these numbers</h3>
<dl>
<dt>A {term('point', 'point')}</dt><dd>is one percentage point of the ideal flight's
CO&#8322;. An airport at <b>+10</b> emits about <b>10% more</b> than comparable flights.</dd>
<dt>Comparable</dt><dd>means <b>same length, same aircraft type</b>. That median is the
<b>{term('norm', 'norm')}</b>, and every <i>efficiency</i> ranking here measures
distance from it — never the raw gap.</dd>
<dt>{term('lateral', 'Lateral')}</dt><dd>is <b>extra kilometres flown</b>; <b>vertical</b>
is a less efficient climb, cruise and descent along the same route. The two add up to
the total.</dd>
<dt>{term('movements', 'Movements')}</dt><dd>are <b>take-offs and landings together</b>: a
flight counts once at each end.</dd>
<dt>Where and when</dt><dd>the ECAC area — Europe wider than the EU — from
{esc(days[0])} to {esc(days[-1])}, {len(days)} days. <b>Release {RELEASE}</b>,
methodology v{METHOD_VERSION}, updated twice a year over a 12-month window; next update
{NEXT_RELEASE}, covering {NEXT_WINDOW}.</dd>
</dl>
</div>
</div>

<section>
<h2>What the gap is made of</h2>
<p class=hint>For every flight we compare the CO&#8322; actually
emitted with that of an ideal flight: same aircraft type, direct great-circle
route, the most efficient altitude and speed for that distance, and <b>the same
real wind</b>. The difference splits into two additive parts: the
<b>lateral</b> one (having flown more kilometres) and the <b>vertical</b> one
(having flown the same route on a less efficient altitude and speed profile).
Over the period observed: total <b>{(lat_w+vert_w):.1f}%</b>, of which
lateral <b>{lat_w:.1f}%</b> and vertical <b>{vert_w:.1f}%</b>.</p>
<div class=card><div class=vizwrap>{viz_concept()}</div>
<p class=caveat style="margin-top:14px">Schematic, not a real flight: the shapes are
drawn to show what the two terms mean, not to depict a particular trajectory.</p></div>
<div class=card><div class=vizwrap>{viz_split(lat_w, vert_w)}</div></div>
<p class=cap>{vert_vs_lat}</p>
</section>

<section id=findings>
<h2>What the data shows</h2>
<p class=hint>Four things visible in the data, with what is known about why —
and what remains unknown. Each opens in full in the methodology.</p>
<div class=findings>{finding_cards}</div>
</section>

<section>
<h2>How much of this gap is compressible</h2>
<p class=hint>Derived from the data itself, not assumed.</p>
<div class=note>
<p>A flight that goes <b>direct</b>, departing <b>at night</b> into a nearly
empty sky, on a <b>long</b> sector where cruise dominates, is about as close to
our ideal trajectory as an airliner gets in practice. Across {n_floor:,} such
flights the vertical gap still stands at <b>{vert_floor:.1f}%</b>.</p>
<p>That is the <b>floor</b>: not inefficiency, but the baseline remaining out of
reach. It comes from choices and constraints no procedure removes: the cruise
speed chosen to meet schedules rather than to minimise fuel, the need to climb
in steps as the aircraft gets lighter, flight levels available only at discrete
intervals.</p>
<table><thead><tr><th>vertical component</th><th class=num>points</th></tr></thead>
<tbody>
<tr><td>median across all flights</td><td class=num>{vert_fleet:.1f}</td></tr>
<tr><td>— floor, not compressible</td><td class=num>{vert_floor:.1f}</td></tr>
<tr><td>— <b>operational margin</b> (traffic, routing, profile)</td>
<td class=num><b>{vert_oper:.1f}</b></td></tr>
</tbody></table>
<p><b>Most of that margin is sector length, not operations.</b> The floor is
measured above 1,000 km, where the median across all flights is
{vert_long:.1f} points rather than {vert_fleet:.1f}. At equal distance the margin
is about <b>{vert_oper_eq:.1f} points</b>, and the rest of the {vert_oper:.1f} is
the distance mix between the two groups. If anything that makes the
incompressible share larger than {vert_floor:.1f} on short sectors, not smaller.</p>
<p>How this floor compares with references built on the <i>best profile actually
observed</i> is set out in the methodology: those measure a different quantity,
and the comparison needs its caveats stated beside it.</p>
</div>
</section>


<section>
<h2>What the spread between comparable flights is worth</h2>
<p class=hint>Not against the theoretical optimum, which nobody can reach, but
against what flights of the same length <b>already achieve</b>.</p>
<div class=note>
<p>If the flights sitting <b>above</b> the median of comparable ones flew like
that median, the CO&#8322; avoided would be <b>{sc_a:.1f} Mt a year</b>
({sc_a_fuel_kt:,.0f} kt of fuel) across the traffic we observe. Bringing only
the worst quartile up to the 75th percentile — the most cautious assumption —
gives <b>{sc_b:.1f} Mt a year</b>.</p>
<p>For comparison, <b>EUROCONTROL estimates 1.1 Mt of CO&#8322; a year</b> as
recoverable in the ECAC area through continuous climb and descent procedures
alone. <b>The two figures coincide, and that is not a confirmation.</b> They
count different things: the spread between comparable flights on one side,
what two named procedures recover on the other. A coincidence between
measurements of different quantities is worth no more than a difference
between them would have been.</p>
<p><b>This is counterfactual arithmetic, not a forecast.</b> It assumes the
median level is reachable everywhere, and it is not: some routes sit above the
median because of structural constraints — closed airspace, terrain, congestion
— that no procedure removes. It measures what the <i>observed spread</i> between
comparable flights is worth, not what is achievable.</p>
</div>
</section>


<section>
<h2>Comparison with the EUROCONTROL indicator</h2>
<p class=hint>Built the same way as KEA: a ratio of sums, over the en-route
portion only, beyond 40 NM from the airports.</p>
<div class=note>
Aggregating our trajectories <b>the way EUROCONTROL does</b>, en-route extension
comes to <b>+{kea:.2f}%</b>, against the <b>~{BENCH['kea_published']:.0f}%</b>
EUROCONTROL publishes for Europe. Same order of magnitude and same construction,
but <b>not</b> the same number: they use radar data over the EUROCONTROL
reference area, we use ADS-B over a quality-filtered subset, with our own
baseline and criteria.
</div>
</section>


<section>
<h2>Why a raw ranking would be wrong</h2>
<p class=hint>The raw gap grows as distance shrinks, so a raw ranking would sort
by shortness. Every ranking below uses the deviation
from the median of <b>flights of the same length and the same aircraft
type</b>.</p>
<div class=card><div class=vizwrap>{viz_bands(band)}</div></div>
<p class=cap>The baseline does not fly every sector at airline cruise level: it
picks the altitude that minimises its own fuel for that distance. Checked against
what aircraft actually do, on the shortest sectors it asks for about
<b>{BENCH['alt_short_below_ft']:,} ft less</b> climb than the median real flight
reaches. Whatever drives the {band.iloc[0].med:.0f}% median gap there, it is not a
reference demanding the impossible.</p>
<p class=cap>The same figures as a table, with the flight counts, are on the
<a href="data.html#bands">data page</a>.</p>
</section>


<section>
<h2>Routes furthest from the norm</h2>
<p class=hint>Δ norm in percentage points against flights of the same length and
type. Rankings use only routes with at least <b>{RANK_MIN_N}</b> flights: below
that the sample is too small for an ordering to mean anything. Routes whose
traffic is majority business aviation are excluded — see
<a href="methodology.html#privacy">privacy</a>.
⚑ = the direct path crosses closed or avoided airspace ({n_closed} routes
flagged).</p>
<div class=card><div class=vizwrap>{viz_routes(g, aname)}</div></div>
<p class=cap>All {len(g):,} ranked routes, those closest to the optimum, and the
CO&#8322; totals: <a href="data.html#routes">on the data page</a>.</p>
</section>


<section>
<h2>Airports</h2>
<p class=hint>Arrivals and departures combined, at least {MIN_N_AIRPORT:,} flights.
The <b>vert.</b> column isolates the profile component, where early descents
and terminal-area holding show up.</p>

<div class=note>
<b>Read this before reading the table.</b> Each row describes <b>the flights that
touch this airport</b>. It is not a measure of the airport's own conduct. A flight
is counted at both of its ends, and its gap is measured over the <b>whole flight</b>
— so part of what appears under one airport may have been produced at the other, or
in the cruise between them. <b>How much?</b> Until the gap could be split by phase,
that question had no answer here; it does now, below.<br><br>
That is why <b>on dep.</b> and <b>on arr.</b> are shown separately: the same figure,
split by the role the airport played. What it shows is where a figure is
concentrated; whether the gap was produced at this airport or inherited from the
other end is answered further down, by the phase split. Both readings occur here.
<b>{esc(aname(sym_ap))}</b> stands at {symr.dep:+.1f} on departure and
{symr.arr:+.1f} on arrival: whatever produces that gap is not confined to one end
of its flights. That is a statement about where the deviation appears, not about
what causes it. <b>{esc(aname(asym_ap))}</b> stands at {asymr.dep:+.1f} and
{asymr.arr:+.1f}: nearly all of it appears on one side, and its combined figure of
{asymr.d:+.1f} alone would not have told you which. The median across all
{len(ga)} airports is {ap_med:+.1f}.<br><br>
{phase_note}
</div>

<div class=card><div class=vizwrap>{viz_airports(ga, aname)}</div></div>
<p class=cap>All {len(ga):,} airports, with the departure and arrival columns:
<a href="data.html#airports">on the data page</a>.</p>
</section>


<section>
<h2>What I make of this</h2>
<p class=hint>The rest of this site is measurement. This section is opinion, and it
is signed. Anyone named here has the right of reply,
<a href="replies.html">published in full</a>. What the
opinion rests on — how much aviation weighs, and what other people measure — is
set out with its sources on the <a href="context.html">context page</a>.</p>

<div class=note>
<p>Nobody commissioned this. I run an ADS-B receiver, which is how I got
interested; the flights here are not its own, but the daily dumps that thousands of
receivers feed. Those dumps are public,
the performance model is open, and I wanted to know what those trajectories would
say if you asked them something harder than <i>where is that plane</i>.</p>

<p>What surprised me is how far that got. Aggregated the way EUROCONTROL builds its
own indicator, this comes out at <b>+{kea:.2f}%</b> against the
~{BENCH['kea_published']:.0f}% they publish. Split by phase,
<b>{pa['arr_desc']:.0f}% of an airport's arrival deviation is burnt in the descent
into it</b>. EUROCONTROL's own figures point the same way: they put the fuel
recoverable by continuous descent at around ten times that recoverable by
continuous climb. I did not expect open data, an open
performance model and a laptop to land that close to institutions that do this for
a living.</p>

<p>Five things I will say as opinions rather than findings.</p>

<p><b>If there is room anywhere, it is in the last forty miles.</b> Not because
most fuel is burnt there — it is burnt climbing and cruising — but because that is
where most of the <i>gap</i> accumulates. A descent profile is not something an
airline decides on its own.</p>

<p><b>The shorter the flight, the worse the arithmetic.</b> Below 200 km a flight
burns about {band.iloc[0].med:.0f}% more than its ideal; on the longest sectors it
is {_band_hi}. Climbing to altitude costs the same whether you then
fly for twenty minutes or for four hours, so on a short sector that fixed cost is
most of the flight. This is geometry, not blame. But it is the clearest pattern in
the whole dataset.</p>

<p><b>A single number per airport can be false.</b> {esc(aname(asym_ap))} is
{asymr.d:+.1f} combined — and {asymr.dep:+.1f} on departure against
{asymr.arr:+.1f} on arrival. I published the split because the combined figure would
have been a lie of omission, and combined figures are what this field usually
publishes.</p>

<p><b>A closed sky has a fuel bill, and you can read it from the ground.</b>
{kal_a} to {kal_b} flies <b>{kal_pct:.0f}%</b> further en route than the straight
line, because the straight line crosses Kaliningrad; {n_closed} of the ranked routes
have a direct path through airspace that is closed or avoided. None of it is
anyone's inefficiency, and all of it is burnt.</p>

<p><b>Most of this table is not a ranking.</b> Half the routes sit within a few
points of the norm, and at the head of the airport table ten positions can be
separated by {head_span_min:.1f} points. Only the extremes mean anything.</p>

<p>The biggest thing these figures leave out is contrails. What I would most
like is to be told where the method is wrong.</p>

<p style="text-align:right;color:var(--mut)">— co2gap</p>
</div>

<h2 id=download>Check it yourself</h2>
<p class=hint>Every figure here can be recomputed from scratch: the data is public,
the method is documented in full and the code is open.</p>
<div class=dl>
<a href="data.html"><b>Browse the data →</b><span>Every route, airport and distance
band, searchable by name or ICAO code</span></a>
<a href="methodology.html"><b>Methodology →</b><span>What is not measured,
validations, stated limitations, independence</span></a>
<a href="https://github.com/co2gap/co2gap"><b>Source code →</b><span>The whole
pipeline, from raw ADS-B to this page</span></a>
<a href="releases.html"><b>Cite a version →</b><span>Which release produced a figure,
what changed since the last one, and how to cite it</span></a>
<a href="mailto:hello@co2gap.org"><b>Ask for your own figures →</b><span>Airports,
ANSPs and airlines: the detail behind these numbers for your own traffic</span></a>
<a href="faq.html#weak"><b>Where this method is weak →</b><span>Three known soft
spots, and an open request to people who work in this field</span></a>
</div>
</section>


<section>
<h2>Method and limitations</h2>
<div class=note>
<b>Data.</b> ADS-B trajectories from the public daily dumps of
<a href="https://adsb.lol">adsb.lol</a>, ODbL licence. Wind from ERA5
(Copernicus/ECMWF). Fuel burn modelled with <a href="https://openap.dev">OpenAP</a>
(TU Delft), anchored per aircraft type to the ICAO Carbon Emissions Calculator
methodology v13.1.<br><br>

<b>How the comparison is built.</b> The ideal flight uses the optimal altitude
for the <i>great-circle</i> distance, not for the distance actually flown:
otherwise a detour would quietly earn itself a better cruise level. The wind
along the real track is sampled along the path and weighted by distance.<br><br>

<b>Stated limitations.</b>
(1) <b>Taxi and ground movement are outside every figure here</b>, on both
sides of the comparison: the fuel model is a model of flight and an aircraft on
the ground is outside its domain, and the reference trajectory never taxis. The
CO&#8322; total is therefore CO&#8322; <i>in flight</i> and understates what the
traffic emitted. (2) We measure the gap from a <b>theoretical</b> optimum, not avoidable
inefficiency.
(3) The period is 2026 only, January to July: no year-on-year comparison.
(4) Four days are missing inside the period, absent at the source; the window
ends on 20 July because the four days after it have flight data but no wind data
yet.
(5) <b>Only the tails of the rankings are reliable</b>: half the routes sit
within a few points of the norm, inside the uncertainty of the method, and their
ordering is not meaningful.
(6) Routes flagged ⚑ <i>cannot</i> fly the direct path: the airspace is closed.
The ban applies to European carriers and not to third-country ones, so the
figure shown is an average between those who must divert and those who need not.
(7) No data about an individual flight or aircraft is published: every row
aggregates at least {MIN_N} flights.
(8) ADS-B coverage does not include oceanic sectors.<br><br>
<b><a href="methodology.html">Full methodology, validations and external
comparisons →</a></b>
</div>

<div class=note>
<b>About this project.</b> This is an independent open-data project, not the
output of an institution. That is why the limitations are stated as prominently
as the results.<br><br>
I am not an aviation professional or a climate scientist; I run an ADS-B
receiver and I care about this. The method, the modelling and the code were
built with AI assistance (Claude); the constraints are mine — what the figures cover, when they change, and what this project declines to claim. The analytical choices behind them
are written down so that people who know the field can check them.<br><br>
<b>Found a mistake, or named here and want to reply?</b>
<a href="mailto:hello@co2gap.org">hello@co2gap.org</a> — corrections and replies
are published on this site, in full and unconditionally.<br><br>
<b>Operate an airport, an ANSP or an airline?</b> The detail behind these figures
for your own traffic — by hour, by origin, by aircraft type, month by month —
exists in the pipeline and is not published here. Same address. The rules that
keep that separate from what appears on this page are written down under
<a href="methodology.html#independence">independence</a>.
</div>
</section>
<p class=foot>
{FOOTNAV}<br>
Trajectory data © <a href="https://adsb.lol">adsb.lol</a> contributors, licensed
under <a href="https://opendatacommons.org/licenses/odbl/">ODbL</a>. These pages are
a Produced Work: reuse with attribution, no share-alike. A dataset extracted from
them is a Derivative Database and stays under ODbL —
<a href="methodology.html#licence">what that means</a>.
Wind: ERA5, Copernicus Climate Change Service.
Airports: OurAirports (CC0).
Fuel references: ICAO CEC Methodology v13.1.
Performance model: OpenAP, TU Delft.
Text and charts <a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>.<br>
{len(df):,} flights · {len(days)} days · {len(months)} months.<br>
Contact <a href="mailto:hello@co2gap.org">hello@co2gap.org</a> ·
{SOCIAL}
</p>

</div></body></html>
"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html_doc)
    print(f"scritto {OUT}  ({len(html_doc)/1024:.0f} KB)")

    # ---- pagina Dati -------------------------------------------------------
    # Qui arriva chi cerca il proprio nome, e prima non poteva: le tabelle
    # complete piu' un filtro dal vivo. Le intestazioni sono sigle, quindi ogni
    # sigla porta alla sua voce di glossario — chi arriva qui puo' aver saltato
    # la home del tutto.
    RH = (f'<tr><th>Route</th><th class=num>flights</th><th class=num>km</th>'
          f'<th class=num>{term("norm", "Δ norm")}</th>'
          f'<th class=num>{term("lateral", "lat.")}</th>'
          f'<th class=num>{term("lateral", "vert.")}</th>'
          f'<th class=num>{term("mt", "t CO&#8322;")}</th></tr>')
    AH = (f'<tr><th>Airport</th><th class=num>{term("movements")}</th>'
          f'<th class=num>{term("norm", "Δ norm")}</th>'
          f'<th class=num>on dep.</th><th class=num>on arr.</th>'
          f'<th class=num>{term("lateral", "Δ lat.")}</th>'
          f'<th class=num>{term("lateral", "Δ vert.")}</th></tr>')
    data_doc = f"""<!doctype html>
<html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Data — co2gap</title>
{meta("Data — co2gap", DESC_DATA, "data.html")}
<style>{STYLE_INDEX}</style></head><body class=data>
{NAV}
<div class=wrap>
<section style="border-top:none;padding-bottom:0">
<h2>Data</h2>
<p class=hint>Everything behind the charts, for the ECAC area over
{esc(days[0])} → {esc(days[-1])}. Type to filter by airport name or ICAO code — the
tables narrow as you type. Column headings are dotted: each one is defined in the
<a href="methodology.html#glossary">glossary</a>.</p>
<input class=search id=q aria-label="Filter tables"
 placeholder="Filter by airport or ICAO code — e.g. Schiphol, EHAM, Madrid">
<p class=count id=cnt></p>
</section>

<section id=routes style="border-top:none">
<h3>Routes furthest from the norm</h3>
<p class=hint>Δ norm in points against flights of the same length and type. Rankings use
only routes with at least <b>{RANK_MIN_N}</b> flights. ⚑ = the direct path crosses closed
or avoided airspace ({n_closed} routes flagged).</p>
<div class=scroll><table><thead>{RH}</thead><tbody class=f>
{worst}
</tbody></table></div>
</section>

<section style="border-top:none">
<h3>Routes closest to the optimum</h3>
<div class=scroll><table><thead>{RH}</thead><tbody class=f>
{best}
</tbody></table></div>
</section>

<section id=airports style="border-top:none">
<h3>Airports furthest from the norm</h3>
<p class=hint>Arrivals and departures combined, at least {MIN_N_AIRPORT:,} flights.</p>
<div class=note>
<b>What this table does not say.</b> Each row measures the modelled deviation of the
flights that touch an airport. It is <b>not</b> a measure of that airport's
performance, of its responsibility, or of emissions it could avoid, and it does not
establish that the airport caused anything.<br><br>
A high value may reflect operator choices, air traffic control constraints,
congestion, the structure of the surrounding airspace, aircraft type, weather, or
other factors this analysis does not separate. The flight is counted at both of its
ends. <a href="index.html#airports">The fuller note is on the home page</a>.
</div>
<div class=scroll><table><thead>{AH}</thead><tbody class=f>
{ap_worst}
</tbody></table></div>
</section>

<section style="border-top:none">
<h3>Airports closest to the norm</h3>
<div class=scroll><table><thead>{AH}</thead><tbody class=f>
{ap_best}
</tbody></table></div>
</section>

<section id=co2 style="border-top:none">
<h3>Routes by total CO&#8322;</h3>
<p class=hint>The routes that weigh most in absolute terms, regardless of efficiency.</p>
<div class=scroll><table><thead>{RH}</thead><tbody class=f>
{by_co2}
</tbody></table></div>
</section>

<section id=bands style="border-top:none">
<h3>European norm by distance band</h3>
<div class=scroll><table><thead><tr><th>Band</th><th class=num>flights</th>
<th class=num>median gap</th></tr></thead><tbody>
{bandrows}
</tbody></table></div>
</section>
</div>

<p class=foot><span class=wrap style="display:block">
{FOOTNAV}<br>
Trajectory data © <a href="https://adsb.lol">adsb.lol</a> contributors, licensed
under <a href="https://opendatacommons.org/licenses/odbl/">ODbL</a>. These pages are
a Produced Work: reuse with attribution, no share-alike. A dataset extracted from
them is a Derivative Database and stays under ODbL —
<a href="methodology.html#licence">what that means</a>.
Wind: ERA5, Copernicus. Airports: OurAirports (CC0). Fuel references: ICAO CEC
v13.1. Performance model: OpenAP, TU Delft.<br>
{len(df):,} flights · {len(days)} days · generated {esc(gen)}.<br>
Contact <a href="mailto:hello@co2gap.org">hello@co2gap.org</a> ·
{SOCIAL}
</span></p>
<script>
var q=document.getElementById('q'),cnt=document.getElementById('cnt'),
    bodies=[].slice.call(document.querySelectorAll('tbody.f')),
    rows=[].slice.call(document.querySelectorAll('tbody.f tr'));
function run(){{
  var s=q.value.trim().toLowerCase(),k=0;
  rows.forEach(function(r){{
    var hit=!s||r.textContent.toLowerCase().indexOf(s)>-1;
    r.style.display=hit?'':'none'; if(hit)k++;
  }});
  bodies.forEach(function(b){{
    var any=[].slice.call(b.rows).some(function(r){{return r.style.display!=='none';}});
    b.closest('section').style.display=any?'':'none';
  }});
  cnt.textContent=s?k+' of '+rows.length+' rows match \\u201c'+q.value+'\\u201d':'';
}}
q.addEventListener('input',run);
</script>
</body></html>
"""
    OUT_DATA = OUT.parent / "data.html"
    OUT_DATA.write_text(data_doc)
    print(f"scritto {OUT_DATA}  ({len(data_doc)/1024:.0f} KB)")


    # ---- FAQ ---------------------------------------------------------------
    # Le domande che questo sito si tira addosso da solo, con la risposta
    # scritta prima che arrivino. Ogni cifra e' interpolata, mai battuta a mano:
    # una FAQ che diverge dalla pagina che spiega e' peggio di nessuna FAQ.
    # L'ordine non e' per importanza ma per sospetto: si apre con l'accusa piu'
    # probabile, non con la presentazione.
    pa_arr_own = f"{pa['arr_own']:.0f}%" if pa else "40 NM of it"
    QA = [
        ("How much does aviation actually matter?",
         """<b>About 2.5% of the world's CO₂ — and that figure is used to end the
argument as often as to start it.</b> It is small beside electricity, industry or
heating, and it has grown roughly eightfold as a share since 1940 while almost
every other sector shrank in relative terms. Counting contrails and nitrogen
oxides, which tonnes of CO₂ do not capture, aviation accounts for about 3.5% of
human-caused radiative forcing. The two percentages are of different quantities
and must not be added or swapped. The <a href="context.html">context page</a>
sets out the series behind all of this, with the sources.""",),
        ("Are you saying airlines and airports are wasting fuel?",
         f"""<b>No, and the distinction is the whole point of this site.</b> We
measure the distance between what a flight actually burnt and what the same
aircraft would have burnt flying the direct route at the most efficient profile,
in the same real wind. That ideal is a <i>theoretical limit</i>: separation
between aircraft, route structure, closed airspace and arrival queues put it out
of reach of every real flight. Published estimates of what is genuinely
<i>recoverable</i> are far smaller — EUROCONTROL puts about
{BENCH['cco_cdo_kg']} kg of fuel per flight on continuous climb and descent
procedures. Of the {vert_fleet:.1f} points of vertical gap the <i>median flight</i>
carries — the headline {vert_w:.1f} is the fleet aggregate, which weighs long flights
more heavily —
<b>{vert_floor:.1f} remain even for a flight going direct through an empty night
sky</b>. We read that as the baseline being unreachable rather than anybody's
inefficiency — a reading, not a second measurement.</p>
<p>That is not the only figure on this site. Alongside the distance from the
theoretical optimum, the front page reports what closing the spread between
comparable flights would be worth: <b>{sc_a:.1f} Mt</b> of CO&#8322; a year if the
flights above the median flew like that median, and <b>{sc_b:.1f} Mt</b> on the
most cautious assumption. EUROCONTROL's own estimate for continuous climb and
descent procedures is <b>1.1 Mt</b>. <b>Those are the figures to set beside
published estimates of avoidable emissions; the {(lat_w+vert_w):.1f}% is not one
of them.</b>"""),
        ("What does an airport's number actually mean?",
         f"""It describes <b>the flights that touch that airport</b>, not the
conduct of the airport. Each flight is counted at both ends and its gap is
measured over the whole flight, so the columns <b>on dep.</b> and <b>on arr.</b>
split the same figure by the role the airport played. The phase split goes
further: for arrivals, a median of {pa_arr_own if pa else '—'} of an airport's
deviation was produced within {term('nm', '40 NM')} of the airport itself.
<b>That is a location, not a cause.</b> It says where the fuel was burnt; it does
not say whether the profile was chosen by the operator or imposed by the
traffic, and nothing here distinguishes the two."""),
        # La domanda era DIGITATA con le cifre di prima della correzione del
        # rullaggio (18 e 22) mentre la risposta sotto era gia' derivata e
        # diceva 11 e 12,1. Correggere una frase non corregge le sue sorelle, e
        # il titolo di una domanda e' una sorella: ora viene dagli stessi
        # numeri della risposta.
        (f"Your figures divide to {excess_t/co2_t*100:.0f}%, not "
         f"{(lat_w+vert_w):.0f}%. Which is right?",
         f"""<b>Both, and the difference is the denominator.</b> Dividing
{excess_t/1e6:,.2f} Mt of gap by the {co2_t/1e6:,.1f} Mt actually emitted gives
{excess_t/co2_t*100:.0f}% — the gap as a share of what was <i>burnt</i>. The
headline {(lat_w+vert_w):.1f}% is the gap as a share of what the <i>ideal
flight</i> would have burnt, which is the smaller number, so the percentage is
larger. Both are ratios of sums, not averages of per-flight percentages; the
remaining fraction of a point between them is the type calibration, which applies
to the tonnages and cancels inside the percentages.
Neither figure is wrong; they answer different questions, and this site
uses the second because every comparison here — route against route, airport
against airport — is made against the ideal, not against the actual.
Per flight that gap is about {total_kg_per_flight(df):.0f} kg of fuel, of which
{vertical_kg_per_flight(df):.0f} kg is the vertical component: the part
continuous climb and descent procedures address."""),
        ("Can I trust the ranking order?",
         f"""<b>Only its tails.</b> About half the routes sit within a few points
of the norm, inside the uncertainty of the method, and their ordering carries no
information. At the head of the airport table ten positions can be separated by
as little as {head_span_min:.1f} points within a single month. What is stable is
the <i>distance from the norm</i>, not the position: read "well above comparable
flights", never "third worst in Europe"."""),
        ("Is this peer reviewed?",
         """<b>No.</b> It is an independent open-data project, not an institutional
or academic publication. What it offers instead is verifiability: the source data
is public, the method is documented in full, the code is open, and every figure
can be recomputed from scratch. The organisations the findings single out were
given advance notice before publication, and any reply from anyone named here is
published in full and unconditionally."""),
        ("Who pays for this?",
         """<b>Nobody.</b> There is no funder, client, sponsor or advertising, and
no organisation has had sight of the figures before publication beyond material
provided on request, on terms open to anyone named here. The rules that keep it that way — including
what happens if that ever changes — are written down under
<a href="methodology.html#independence">independence</a>."""),
        ("What does it leave out?",
         """<b>CO&#8322; is not the whole climate effect of flying.</b> Contrails
and nitrogen oxides contribute a large share of aviation's total warming effect —
by published assessments, the majority of it — and these figures contain none of
them. It also excludes ground operations, and ADS-B coverage does not include
oceanic sectors. Read the figures here as what they are: fuel burnt in the air
over Europe, turned into CO&#8322;."""),
        ("Why only 2026, and why no comparison with last year?",
         f"""Because {len(days)} days of 2026 is what has been processed so far,
and a year-on-year comparison built on a single period would be an invitation to
read weather as a trend. Releases come twice a year from now on, each covering
twelve months, so the first honest comparison becomes possible once two of those
windows exist."""),
        ("Does this track individual flights, aircraft or people?",
         f"""<b>No.</b> Nothing is published below an aggregate of at least
{MIN_N} flights, and rankings need at least {RANK_MIN_N}. The pipeline keeps no
registration and no callsign — only the aircraft type and the airports — and no
figure on this site describes an identifiable flight, operator crew or
passenger."""),
        ("Is this an emissions inventory?",
         f"""<b>No, and the difference is the point.</b> An inventory answers
<i>how much, and where</i>. The most complete one built from the same raw material
as this site — <a href="https://acp.copernicus.org/articles/24/725/2024/">GAIA</a>,
published in <i>Atmospheric Chemistry and Physics</i> in {BENCH['gaia_year']} —
reconstructs {BENCH['gaia_flights_m']} million ADS-B trajectories worldwide and
gives CO&#8322;, nitrogen oxides and particulate on a grid, for atmospheric
research. It is the reference for what aviation emits.<br><br>
This site answers a narrower question: <b>how far from a reference</b>. It does
not try to count Europe's aviation emissions — it compares each flight with an
ideal version of itself, and reports the difference by route and by airport. An
inventory tells you what was emitted; this tells you how much comparable flights
differ from one another. The two are complementary, and where they overlap the
inventory is the better source."""),
        ("How accurate is the fuel model?",
         """Fuel burn comes from <a href="https://openap.dev">OpenAP</a>, an open
performance model from TU Delft, anchored per aircraft type to the ICAO Carbon
Emissions Calculator methodology. The check that matters is on the types the
model was <i>not</i> corrected for: the most common airliners in the sample land
within about 5% of a reference the model never saw. Types that needed correction,
and why, are listed in the <a href="methodology.html">methodology</a> — the
correction compensates a documented limitation, not an unexplained
discrepancy."""),
        ("A route here cannot fly its direct path. Is that counted as inefficiency?",
         f"""It is measured, and it is flagged. {n_closed} ranked routes have a
great circle crossing closed or systematically avoided airspace, and they carry a
⚑ wherever they appear. That detour is not recoverable while the closures hold.
Note also that an overflight ban binds European carriers and not third-country
ones, so a figure for such a route averages operators that must divert with
operators that need not."""),
        ("Can I reuse these figures?",
         f"""Yes, and the terms differ by what you reuse. The <b>text and the charts</b> are
additionally offered under CC BY 4.0 — this project's own expression. The
<b>figures as data</b> derive from
<a href="https://adsb.lol">adsb.lol</a> trajectories under
{term('odbl', 'ODbL')}, so a dataset built from them is a derivative database and carries
the same share-alike obligation. In both cases attribution is required. The
detail is under <a href="methodology.html#licence">licence and reuse</a>."""),
        ("I am named here and I disagree with the figure. What happens?",
         """Write to <a href="mailto:hello@co2gap.org">hello@co2gap.org</a>. A
reply is published on this site <b>in full, unconditionally and next to the
figure it concerns</b> — not summarised, not answered selectively. If the
disagreement is about the method rather than the reading, the pipeline is open
and a reproducible counter-example is the fastest way to change what is
published."""),
    ]
    qa_html = "\n".join(
        f"<section><h2>{q}</h2><p>{a}</p></section>" for q, a in QA)
    FOOT_FAQ = f"""<p class=foot><span class=wrap style="display:block">
{FOOTNAV}<br>
Trajectory data © <a href="https://adsb.lol">adsb.lol</a> contributors, licensed
under <a href="https://opendatacommons.org/licenses/odbl/">ODbL</a>. These pages are
a Produced Work: reuse with attribution, no share-alike. A dataset extracted from
them is a Derivative Database and stays under ODbL —
<a href="methodology.html#licence">what that means</a>.
Wind: ERA5, Copernicus. Airports: OurAirports (CC0). Fuel references: ICAO CEC
v13.1. Performance model: OpenAP, TU Delft.<br>
{len(df):,} flights · {len(days)} days · release {RELEASE} · generated {esc(gen)}.<br>
Contact <a href="mailto:hello@co2gap.org">hello@co2gap.org</a> ·
{SOCIAL}
</span></p>"""
    faq_doc = f"""<!doctype html>
<html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Questions and answers — co2gap</title>
{meta("Questions and answers — co2gap", DESC_FAQ, "faq.html")}
<style>{STYLE_INDEX}</style></head><body>
{NAV}
<div class=wrap>
<div class=hero style="padding-bottom:0">
<p class=eyebrow>Questions and answers</p>
<h1>What this site does and does not say</h1>
<p class=lede>The questions a project like this invites, answered before they are
asked. If yours is not here, <a href="mailto:hello@co2gap.org">write</a> and it will
be — the list is meant to grow.</p>
</div>
{qa_html}
<section id=weak>
<h2>Where this method is weak</h2>
<p class=hint>Not a disclaimer. These are the places where I already know the
method is soft, written down so that someone who works in this field can tell me how
wrong I am. <b>Criticism of the method is what is being asked for — not endorsement.</b>
Whatever comes back is published here, including the parts that do not suit the
conclusions.</p>

<div class=card>
<h3>1. The cruise baseline is not as optimal as it claims</h3>
<p>Measured over the <b>cruise alone</b>, our gap comes out slightly <i>negative</i>:
the real aircraft burns marginally less than the profile we call optimal. That is not
a result about aviation, it is a defect in our reference: the optimal cruise altitude
we compute is not the fuel-optimal one. Published work on cruise efficiency finds a
clear positive gap on the same perimeter, so the disagreement is ours to explain.</p>
<p>We now know where it comes from. Compared with the altitude aircraft actually
reach, our baseline cruises about <b>{BENCH['alt_long_below_ft']:,} ft below</b>
them on the longest sectors — and lower, up there, is not better. The reference is
burning more than it should exactly where cruise dominates, which is enough to
swallow the gap and turn it negative.</p>
<p class=caveat>It affects the <b>level</b> of the vertical term, not the attribution:
cruise contributes very little to what separates one airport from another. Correcting
it would most likely make the headline figure <i>larger</i>, since a genuinely optimal
reference burns less.</p>
</div>

<div class=card>
<h3>2. CO&#8322; is not the whole climate effect</h3>
<p>Contrails and nitrogen oxides account for a large share of aviation's warming
effect &mdash; by published assessments the majority of it &mdash; and these figures contain
neither. A route flown at a level that avoids contrail formation could be worse by this
site's figures and better for the climate.</p>
<p>Both have now been measured, for European traffic across 201 days of data, to 24 July 2026 — a slightly wider window than the CO&#8322; figures above, whose 197 days end on 20 July because that is where the wind data ends. The method,
the figures and what they cannot support will be published with the January 2027 release,
when the window covers a full calendar year: contrail forcing per flight differs by a
factor of about four and a half between January and July, so seven months is not a year,
and publishing a figure now would mean publishing one that then moves. The January release
will also be explicit about which parts of it can be broken down and which cannot &mdash;
some of what a per-airport contrail ranking appears to show follows from modelling
choices rather than from the air above the airport.</p>
<p class=caveat>Anyone who works on non-CO&#8322; effects and can say how badly that
changes the reading of these rankings would be doing this project a service.</p>
</div>

<div class=card>
<h3>3. Fuel modelling for the types the model does not cover</h3>
<p>The performance model carries calibrated fuel curves for a limited set of aircraft
types; the rest fall back on a generic model rescaled from a static take-off figure.
Our per-type correction compensates that, and the check is that the types needing no
correction land within about 5% of an independent reference. But a correction is
still a correction.</p>
<p class=caveat>The diagnosis, including which types are affected and why, is written
up in the <a href="methodology.html">methodology</a> and has been put to the model's
authors publicly.</p>
</div>

<div class=card>
<h3>4. The uncertainty of the figures is not quantified</h3>
<p>Aircraft mass is estimated, not known, and it is the largest physical
uncertainty in the model. We say so, but we do not say <b>how much</b> it moves
the result — there is no ± on any number here.</p>
<p class=caveat>And the honest version of that is harder than it sounds. This
metric is a <b>difference between two model runs</b>, the real flight and the
ideal one. An error that is systematic cancels out in the subtraction; one that
varies with altitude, weight or phase of flight does not, and lands squarely on
the gap. Showing that the model reproduces published fuel burn to within a few
per cent says nothing about either case. Until that sensitivity is measured, only
the extremes of these rankings should be read as meaning anything, which is why
that caution appears wherever a ranking does.</p>
</div>

<div class=card>
<h3>And one we found ourselves</h3>
<p>The quality gate is <b>geometric</b>: it checks that a flight covered the distance
it should have, and never looks at the fuel. A handful of flights in the published
period therefore carry burn figures that are physically impossible, the residue of
degenerate trajectories. They are far too few to move any published statistic
(correcting them shifts the headline by two ten-thousandths of a point), but they are
there, and a gate on fuel plausibility is due in the next release.</p>
</div>
</section>

<section>
<h2>How to send something useful</h2>
<p class=hint>Corrections are welcome and are published. These arrive in a form that
can actually be acted on:</p>
<div class=card>
<p><b>For a figure you think is wrong:</b> the airport or route, the period, which
number you are disputing, and what you believe it should be. If you hold traffic or
fuel data of your own, saying <i>how far</i> ours is from yours is more useful than
saying that it is wrong.</p>
<p><b>For the method:</b> the step you disagree with, and — if you can — the case that
breaks it. The pipeline is open, so a reproducible counter-example changes what is
published faster than any argument.</p>
<p><b>If you are named here and want to reply:</b> say so, and the reply is published
in full, next to the figure it concerns, without editing.</p>
</div>
</section>

<section>
<h2>Still unanswered?</h2>
<p class=hint><a href="mailto:hello@co2gap.org">hello@co2gap.org</a>. Corrections and
replies are published on this site, in full and unconditionally.</p>
</section>
</div>
{FOOT_FAQ}
</body></html>
"""
    OUT_FAQ = OUT.parent / "faq.html"
    OUT_FAQ.write_text(faq_doc)
    print(f"scritto {OUT_FAQ}  ({len(faq_doc)/1024:.0f} KB)")

    rel_doc = build_releases(days, len(df))
    (OUT.parent / "releases.html").write_text(rel_doc)
    rep_doc = build_replies()
    (OUT.parent / "replies.html").write_text(rep_doc)
    service_files(OUT.parent, gen[:10], days)
    print(f"scritto {OUT.parent/'releases.html'} · {OUT.parent/'replies.html'} · "
          f"sitemap.xml · robots.txt · feed.xml")

    import context_page
    ctx_doc = context_page.build(
        meta=meta, nav=NAV, footnav=FOOTNAV, style=STYLE_INDEX, term=term, release=RELEASE,
        method_version=METHOD_VERSION, n_flights=len(df), days=len(days),
        lat_w=lat_w, vert_w=vert_w)
    OUT_CTX = OUT.parent / "context.html"
    OUT_CTX.write_text(ctx_doc)
    # Le cifre esterne viaggiano col sito: un lettore che vuole verificarle deve
    # poter leggere fonte e data di verifica senza aprire il repository.
    shutil.copyfile(context_page.EXTERNAL, OUT.parent / "context-sources.json")
    print(f"scritto {OUT_CTX}  ({len(ctx_doc)/1024:.0f} KB)")

    meth = build_methodology(df, days, months, lat_w, vert_w, kea,
                             co2_t, excess_t, len(g_all), len(g), len(ga), gen,
                             sc_a, sc_b, sc_a_fuel_kt,
                             vert_floor, vert_fleet, vert_oper, n_floor,
                             vert_long, vert_oper_eq,
                             conv_fleet, conv_ap1,
                             finding_sections, n_biz_routes, non_airliner_pct)
    OUT_METH.write_text(add_toc(meth))
    print(f"scritto {OUT_METH}  ({len(meth)/1024:.0f} KB)")
    print(f"  voli {len(df):,} · giorni {len(days)} · rotte n>={MIN_N} {len(g_all):,} "
          f"· in classifica n>={RANK_MIN_N} {len(g):,} · aeroporti {len(ga):,}")
    print(f"  CO2 {co2_t/1e6:.2f} Mt · excess {excess_t/1e6:.2f} Mt "
          f"· lat {lat_w:.2f}% · vert {vert_w:.2f}% · KEA +{kea:.2f}%")
    print(f"  rotte con corridoio chiuso segnalate: {n_closed}")



# ===========================================================================
#  Pagine di servizio: la storia delle versioni e il contraddittorio
# ===========================================================================
def simple_page(title, desc, page, body):
    """Guscio delle pagine minori: stessa testata, stesso stile, stesso piede."""
    return f"""<!doctype html>
<html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>{esc(title)} — co2gap</title>
{meta(f"{title} — co2gap", desc, page)}
<style>{STYLE_INDEX}</style></head><body>
{NAV}
<div class=wrap>
{body}
</div>
<p class=foot><span class=wrap style="display:block">
{FOOTNAV}<br>
Trajectory data © <a href="https://adsb.lol">adsb.lol</a> contributors, licensed
under <a href="https://opendatacommons.org/licenses/odbl/">ODbL</a> — these pages
are a Produced Work: attribution, no share-alike.
Wind: ERA5, Copernicus. Airports: OurAirports (CC0). Performance model: OpenAP,
TU Delft.<br>
<b>Release {RELEASE}</b> · methodology v{METHOD_VERSION} ·
<a href="releases.html">release history</a> ·
<a href="feed.xml">updates feed</a><br>
Contact <a href="mailto:hello@co2gap.org">hello@co2gap.org</a>
</span></p>
</body></html>"""


def build_releases(days, n_flights) -> str:
    rows = []
    for r in RELEASES:
        doi = (f'<a href="https://doi.org/{r["doi"]}">{r["doi"]}</a>' if r["doi"]
               else '<span class=mut>archived at publication</span>')
        rows.append(f"""<tr><td class=r style="white-space:nowrap"><b>{r['date']}</b></td>
<td class=r style="min-width:15em">{esc(days[0])} → {esc(days[-1])}, {len(days)} days<br>
<span class=mut>{n_flights:,} flights · methodology v{r['version']}</span></td>
<td class=r>{r['what']}</td><td class=r>{doi}</td></tr>""")
    body = f"""<div class=hero style="padding-bottom:0">
<p class=eyebrow>Release history</p>
<h1>Which version produced a figure</h1>
<p class=lede>Figures on this site move between releases, and not always because
European flying changed. This page says what changed and why, so that a difference
can be read for what it is.</p>
</div>

<section>
<h2>Published</h2>
<div class=scroll><table>
<thead><tr><th>release</th><th>covers</th><th>what changed</th><th>archive</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
</section>

<section>
<h2>Planned — {PLANNED['date']}</h2>
<div class="note warn">
<p><b>Covering {PLANNED['window']}.</b></p>
<p>{PLANNED['what'].format(days=len(days))}</p>
<p>It is written here before the fact rather than explained after it, because a
change announced in advance can be checked and one explained afterwards can only
be believed.</p>
</div>
<p class=hint>From the January 2027 release onwards, releases are twice a year, at
the end of January and the end of July, and each one carries twelve months rather
than the calendar half it follows: a
rolling window contains every season, and this site's own figures are seasonal.
The cost, stated plainly: two consecutive releases share six months of data, so
movements between them are damped.</p>
</section>

<section>
<h2>Citing a version</h2>
{citation(days)}
</section>"""
    return simple_page(
        "Release history",
        "Which version of co2gap produced a figure, what changed between releases, "
        "and how to cite a specific one.",
        "releases.html", body)


def build_replies() -> str:
    """La pagina del diritto di replica.

    Vuota e' informativa: dice che la porta e' aperta e che nessuno l'ha ancora
    usata. Senza questa pagina la promessa fatta due volte altrove sul sito —
    «right of reply, published in full» — non ha un indirizzo, e una promessa
    senza indirizzo non e' una promessa.
    """
    body = f"""<div class=hero style="padding-bottom:0">
<p class=eyebrow>Right of reply</p>
<h1>Replies and corrections</h1>
<p class=lede>Anyone named on this site — an airport, an airline, an air navigation
service provider, anyone at all — can reply, and the reply is published here in
full and unedited. Corrections to the figures are published here too, whoever
finds them.</p>
</div>

<section>
<h2>Published so far</h2>
<div class=note>
<p><b>No reply to a published figure yet:</b> these figures become public with
this release. The advance notice has already produced one exchange — the
organisations notified on 29 July 2026 all received the same terms and the same
lead time, and one of them asked for the methodology and the underlying figures
before commenting. Those were sent on 16 August, on terms open to any
organisation named here that asks for them. No comment on the figures themselves
has been received.</p>
<p>This section lists replies and corrections as they arrive, oldest first, with
the date and the figure each one concerns.</p>
<p class=hint>The invitation is the same for everyone named on this site, and it
does not expire.</p>
</div>
</section>

<section>
<h2>How this works</h2>
<ul>
<li><b>A reply is published in full.</b> Not summarised, not excerpted, not
answered inline before you have read it. If it is long, it gets its own page.</li>
<li><b>A correction changes the figure, not the wording around it.</b> If a number
here is wrong, it is recomputed and the change is recorded on the
<a href="releases.html">release history</a>. What was previously published stays
readable: a figure that quietly changes is worse than one that was wrong.</li>
<li><b>Disagreement about method is not a correction</b>, and is published as a
reply rather than folded into the method. Where a criticism has changed the
method, the <a href="methodology.html#weak">stated limitations</a> say so.</li>
<li><b>No approval is asked before publishing a figure</b>, and none is given
after. The right of reply is not a right of veto — it is the reason this work is
public rather than private.</li>
</ul>
<p>Write to <a href="mailto:hello@co2gap.org">hello@co2gap.org</a>. If you
represent an organisation named here, say so and the reply is published under that
name; if you would rather not be named, say that too and the substance is published
without the attribution.</p>
</section>"""
    return simple_page(
        "Replies and corrections",
        "The right of reply on co2gap: how replies and corrections are published, "
        "and what has been received so far.",
        "replies.html", body)


def service_files(out_dir: Path, gen_iso: str, days) -> None:
    """sitemap.xml, robots.txt e il feed Atom.

    Il feed ha una voce per release, non per modifica: chi lo segue vuole sapere
    quando escono cifre nuove, non quando si corregge una virgola. Trenta righe e
    nessun servizio esterno, che e' il minimo onesto per due uscite l'anno.
    """
    pages = ["index.html", "context.html", "data.html", "methodology.html",
             "faq.html", "releases.html", "replies.html"]
    urls = "".join(
        f"<url><loc>{SITE_URL}/{p}</loc><lastmod>{gen_iso[:10]}</lastmod></url>\n"
        for p in pages)
    (out_dir / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}</urlset>\n", encoding="utf-8")
    (out_dir / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}/sitemap.xml\n", encoding="utf-8")

    entries = ""
    for r in sorted(RELEASES, key=lambda x: x["date"], reverse=True):
        doi = f" DOI: {r['doi']}." if r["doi"] else ""
        entries += f"""<entry>
<title>Release {r['date']}</title>
<link href="{SITE_URL}/releases.html"/>
<id>tag:co2gap.org,{r['date']}:release</id>
<updated>{r['date']}T00:00:00Z</updated>
<summary>{esc(r['what'])} Covers {esc(days[0])} to {esc(days[-1])}, methodology
v{r['version']}.{doi}</summary>
</entry>
"""
    (out_dir / "feed.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom">\n'
        "<title>co2gap — releases</title>\n"
        f'<link href="{SITE_URL}/"/>\n'
        f'<link rel="self" href="{SITE_URL}/feed.xml"/>\n'
        f"<id>{SITE_URL}/</id>\n"
        f"<updated>{RELEASES[0]['date']}T00:00:00Z</updated>\n"
        "<author><name>co2gap</name></author>\n"
        "<subtitle>Two releases a year: what changed, and which version produced a "
        "figure.</subtitle>\n"
        f"{entries}</feed>\n", encoding="utf-8")

if __name__ == "__main__":
    main()
