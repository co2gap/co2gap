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
OUT_METH = OUT.parent / "metodologia.html"

# Results produced by other steps of the pipeline and quoted on the methodology
# page. They are constants here because they come from runs this script does not
# perform; each is reproducible with the command named beside it.
GATE = {"gennaio": (8.6, 5.1, 1798), "febbraio": (9.9, 5.2, 1781),
        "luglio": (8.5, 4.6, 2458)}          # lab/gate.py
STAB = {"pairs": 21, "median": 0.867, "worst": 0.789,
        "worst_pair": "feb→lug", "consec": 0.924}   # lab/stability.py
# Verified against primary sources on 2026-07-27, see reports/.
BENCH = {"cco_cdo_kg": 39, "cco_cdo_pct": 1.1,
         "pasutto_pct": 4.6, "pasutto_kg": 60, "pasutto_avg_pct": 7.5,
         "pasutto_avg_kg": 85, "kea_published": 3.0}

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
MIN_N_AIRPORT = 200

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
    # co2_kg_v0 is UNCALIBRATED. Percentages are calibration-invariant (the
    # factor multiplies real and ideal alike and cancels), tonnages are not.
    df["co2_real_kg"] = df.co2_kg_v0.to_numpy() * k
    df["co2_ideal_kg"] = df.ideal_gc_co2_kg.to_numpy() * k
    df["co2_hybrid_kg"] = df.hybrid_co2_kg.to_numpy() * k
    df["excess_kg"] = df.co2_real_kg - df.co2_ideal_kg
    df["bin"] = pd.cut(df.gc_km, BINS).astype(str)
    for src, dst in (("excess_total_pct", "d_tot"),
                     ("excess_lateral_pct", "d_lat"),
                     ("excess_vertical_pct", "d_vert")):
        med = df.groupby("bin")[src].median()
        df[dst] = df[src].to_numpy() - df["bin"].map(med).to_numpy()
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


STYLE = """
:root{--bg:#0e1216;--card:#161d23;--fg:#e8eef3;--mut:#8ea3b2;--line:#243039;
--pos:#ff8a6b;--neg:#5fd0a8;--hi:#5ac8fa;--warn:#f0b429}
@media(prefers-color-scheme:light){:root{--bg:#fbfcfd;--card:#fff;--fg:#16212b;
--mut:#5b6b78;--line:#e2e8ee;--pos:#c2410c;--neg:#0f766e}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:36px 20px 90px}
h1{font-size:1.7rem;margin:0 0 8px;letter-spacing:-.02em}
h2{font-size:1.12rem;margin:40px 0 8px;letter-spacing:-.01em}
h3{font-size:.98rem;margin:24px 0 6px;color:var(--fg)}
.sub{color:var(--mut);margin:0 0 22px}
p{margin:12px 0}
ul{margin:12px 0;padding-left:22px}
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


def build_methodology(df, days, months, lat_w, vert_w, kea, co2_t, excess_t,
                      n_routes_all, n_routes_rank, n_airports, gen,
                      sc_a, sc_b, sc_a_fuel_kt,
                      vert_floor, vert_fleet, vert_oper, n_floor) -> str:
    """The page that has to be right even when nobody reads it.

    Written comparative-first: the defensible product of this work is that one
    route deviates more than comparable ones, not that European aviation wastes
    N megatonnes. The absolute figure is context and is labelled as such.
    """
    gate_rows = "\n".join(
        f"<tr><td>{m}</td><td class=num>{r:,}</td><td class=num>{wf:.1f}</td>"
        f"<td class=num><b>{wa:.1f}</b></td></tr>"
        for m, (wf, wa, r) in GATE.items())
    # Calibrated, like every absolute mass on this site and in the phase-2b
    # report: percentages are calibration-invariant, kilograms are not, and the
    # two artefacts must not quote different figures for the same quantity.
    per_flight_vert = (df.co2_real_kg.sum() - df.co2_hybrid_kg.sum()) / 3.16 / len(df)
    return f"""<!doctype html>
<html lang=it><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Metodologia — co2gap</title>
<style>{STYLE}</style></head><body><div class=wrap>

<p><a href="index.html">← torna ai dati</a></p>
<h1>Metodologia</h1>
<p class=sub>Come sono calcolati i numeri di questo sito, che cosa significano
e — soprattutto — che cosa <b>non</b> significano.
Aggiornato al {esc(gen)}.</p>

<h2>1. La domanda a cui rispondiamo</h2>
<p>Per ogni volo confrontiamo il CO₂ realmente emesso con quello di un volo
<b>ideale</b>: stesso tipo di aeromobile, percorso diretto great-circle, quota e
velocità più efficienti per quella distanza, e <b>lo stesso vento reale</b>.</p>
<p>La differenza è divisa in due parti che sommate danno il totale:</p>
<ul>
<li><b>laterale</b> — il costo di aver volato più chilometri del necessario;</li>
<li><b>verticale</b> — il costo di aver volato lo <i>stesso</i> percorso con un
profilo di quota e velocità meno efficiente.</li>
</ul>
<p>La separazione si ottiene con una baseline intermedia: traccia al suolo
<i>reale</i>, ma profilo di quota e velocità <i>ottimo</i>. Le due componenti
sono additive per costruzione, perché condividono il denominatore.</p>

<h2>2. Che cosa NON stiamo misurando</h2>
<div class="note warn">
<p><b>Non stiamo misurando carburante sprecato e recuperabile.</b> Il volo ideale
è un limite teorico che nessun volo reale può raggiungere: separazione fra
aeromobili, struttura delle rotte, spazi aerei vincolati, sequenziamento in
avvicinamento e meteo lo rendono irraggiungibile per ragioni che non sono
inefficienza.</p>
<p>Le stime di inefficienza <i>evitabile</i> pubblicate dagli enti del settore
sono molto più piccole delle nostre, ed è corretto che lo siano:</p>
<table><thead><tr><th>misura</th><th class=num>per volo</th></tr></thead><tbody>
<tr><td>EUROCONTROL — livellamenti in salita e discesa, recuperabili con
procedure CCO/CDO</td><td class=num>~{BENCH['cco_cdo_kg']} kg</td></tr>
<tr><td>Pasutto et al. (EUROCONTROL, 2021) — crociera, rispetto al miglior
profilo realmente volato</td><td class=num>{BENCH['pasutto_kg']}–{BENCH['pasutto_avg_kg']} kg</td></tr>
<tr><td><b>questo sito</b> — scarto dall'ottimo teorico, intero profilo</td>
<td class=num><b>~{per_flight_vert:.0f} kg</b></td></tr>
</tbody></table>
<p>Sullo stesso perimetro di distanze usato da Pasutto (200–1500 NM) il loro
{BENCH['pasutto_pct']}% mediano di sola crociera si confronta con il nostro 13,1%
di profilo intero: un fattore <b>2,8</b>, spiegato da tre differenze dichiarate —
il loro riferimento è il <i>miglior profilo osservato</i>, il nostro un ottimo
fisico; loro coprono la sola crociera, noi anche salita, discesa e velocità;
loro assumono massa nominale e nessun vento, noi massa stimata e vento reale.</p>
<p><b>Conseguenza pratica:</b> moltiplicare il nostro totale per un prezzo del
carbonio e parlare di «spreco» sarebbe sbagliato. Non lo facciamo, e chiediamo
di non farlo.</p>
</div>

<h3>Quanta parte è comprimibile — misurato, non assunto</h3>
<p>Un volo diretto, in partenza di notte a cielo vuoto, su tratta lunga, è
quanto di più vicino alla traiettoria ideale un aereo di linea arrivi davvero.
Su {n_floor:,} voli così lo scarto verticale resta <b>{vert_floor:.1f}%</b>
contro una mediana di flotta del <b>{vert_fleet:.1f}%</b>.</p>
<p>Ne segue un'attribuzione ricavata dai dati: <b>{vert_floor:.1f} punti sono
pavimento</b> — la baseline che resta irraggiungibile per cost index, step climb
imposti dal peso e livelli di volo discreti — e <b>{vert_oper:.1f} punti sono
margine operativo</b>, legato a traffico, routing e profilo.</p>
<p>Il pavimento coincide quasi con il valore che uno studio EUROCONTROL ricava
per la crociera confrontando ogni volo con il <i>miglior profilo osservato</i>,
un riferimento che quei vincoli li contiene già. Due strade indipendenti, stesso
punto d'arrivo: è la ragione per cui il divario apparente con i riferimenti
esterni non indica un errore del modello, ma la differenza fra una mediana di
flotta e un riferimento best-in-class.</p>

<h3>Un numero che invece si può dare</h3>
<p>C'è un modo di quantificare il margine senza appoggiarsi a un ottimo
irraggiungibile: confrontare ogni volo non con la perfezione, ma con <b>quello
che voli di pari lunghezza già ottengono</b>. Quel livello è raggiungibile per
definizione, perché metà dei voli comparabili lo raggiunge.</p>
<ul>
<li>Se i voli sopra la mediana dei comparabili volassero come quella mediana:
<b>{sc_a:.1f} Mt di CO₂ all'anno</b> ({sc_a_fuel_kt:,.0f} kt di carburante).</li>
<li>Portando al 75° percentile <b>solo il quartile peggiore</b>, ipotesi molto
più prudente: <b>{sc_b:.1f} Mt all'anno</b>.</li>
</ul>
<p>EUROCONTROL stima indipendentemente in <b>1,1 Mt di CO₂ all'anno</b> il
recuperabile in area ECAC con le sole procedure di salita e discesa continue: lo
scenario prudente qui sopra vi si avvicina molto, pur essendo costruito con un
metodo del tutto diverso.</p>
<p><b>Resta aritmetica controfattuale.</b> Assume che il livello mediano sia
raggiungibile ovunque, e non lo è: parte della dispersione è dovuta a vincoli
strutturali — spazi aerei chiusi, orografia, congestione — che nessuna procedura
elimina. Il numero misura <i>quanto vale la dispersione osservata fra voli
comparabili</i>, non quanto sia realizzabile. È il limite superiore di un
margine, non un obiettivo.</p>

<h2>3. Perché allora il confronto fra rotte resta valido</h2>
<p>Perché <b>un riferimento irraggiungibile si cancella nel confronto</b>. Due
aeroporti misurati rispetto allo <i>stesso</i> ottimo impossibile restano
confrontabili fra loro: la distanza che li separa non dipende
dall'irraggiungibilità, che è comune a entrambi.</p>
<p>Per questo tutte le classifiche del sito non usano il valore assoluto ma il
<b>Δ norma</b>: lo scarto dalla mediana europea dei voli di <i>pari lunghezza</i>.
L'ottimo teorico serve solo come unità di misura condivisa.</p>
<p>Questa correzione è necessaria, non cosmetica: lo scarto grezzo correla circa
<b>−0,74</b> con la lunghezza della tratta, quindi una classifica grezza
ordinerebbe le rotte per brevità e non per inefficienza. Dopo la
normalizzazione la correlazione residua con la distanza è circa <b>+0,08</b>.</p>

<h2>4. Dati e strumenti</h2>
<ul>
<li><b>Traiettorie</b>: dump giornalieri pubblici di
<a href="https://adsb.lol">adsb.lol</a>, licenza ODbL. Ogni volo è ricostruito
dai punti ADS-B trasmessi dagli aeromobili stessi.</li>
<li><b>Vento</b>: rianalisi <b>ERA5</b> (Copernicus/ECMWF), 11 livelli di
pressione, risoluzione oraria.</li>
<li><b>Consumi</b>: <a href="https://openap.dev">OpenAP</a> (TU Delft), modello
aperto di prestazioni aeronautiche.</li>
<li><b>Ancoraggio</b>: i consumi di crociera per tipo sono ancorati alla
<b>ICAO Carbon Emissions Calculator Methodology v13.1</b>, Appendice C.</li>
</ul>

<h3>Il vento è il punto che rende confrontabili le due direzioni</h3>
<p>Il consumo reale è già corretto per il vento, perché deriva dalla velocità
all'aria misurata. Il volo ideale no: se lo si cronometra senza vento, la stessa
rotta risulta artificialmente efficiente in un verso e inefficiente nell'altro.
Il volo ideale viene quindi cronometrato alla velocità al suolo corretta con il
vento ERA5 lungo il percorso, e l'asimmetria si annulla fra i due versi.</p>

<h3>Due scelte di baseline che cambiano il risultato</h3>
<ul>
<li>La quota di crociera ottima è quella della distanza <b>great-circle</b>, non
di quella realmente volata: altrimenti una deviazione si guadagnerebbe di
nascosto un livello di crociera migliore, e la componente laterale si
sgonfierebbe.</li>
<li>Il vento lungo la traccia reale è pesato sulla <b>distanza</b>, non sul
tempo: la traccia è campionata nel tempo, quindi è fitta dove l'aereo è lento, e
una media temporale sovrappeserebbe le aree terminali.</li>
</ul>

<h2>5. Calibrazione</h2>
<p>I consumi di OpenAP sono confrontati per tipo con i valori derivati da ICAO, e
corretti con un fattore per i tipi che deviano oltre il 10% con almeno 100 voli
osservati. Il fattore moltiplica sia il volo reale sia il suo ideale, quindi
<b>si semplifica nelle percentuali</b>: incide sulle tonnellate, non sugli scarti
percentuali.</p>
<p>La verifica che conta non è sui tipi calibrati — per quelli il controllo è
tautologico — ma su quelli <b>non</b> calibrati: A320, A321, B738 e A319, che da
soli sono la maggioranza dei voli, cadono entro il 5% del riferimento ICAO senza
alcuna correzione.</p>

<h2>6. Quali voli entrano</h2>
<p>Un volo entra nell'analisi solo se la sua traccia è sufficientemente
completa: copertura adeguata e nessun buco temporale ampio.</p>
<p>C'è poi un criterio che viene spesso frainteso, quindi vale la pena essere
espliciti. Scartiamo i voli la cui distanza volata risulta <b>minore</b> del 90%
della great-circle. Volare meno della rotta diretta è geometricamente
impossibile: quando succede è perché la traccia è <b>troncata</b> da un buco di
ricezione, e quel volo sembrerebbe più efficiente del possibile.
<b>Non scartiamo affatto i voli molto deviati</b> — quelli hanno distanza volata
<i>maggiore</i> della great-circle e restano tutti nel campione, comprese le
rotte di testa delle classifiche.</p>
<p>Nel periodo pubblicato: <b>{len(df):,} voli</b> su {len(days)} giorni,
{len(months)} mesi, area ECAC.</p>

<h2>7. Validazioni</h2>

<h3>Il vento è modellato correttamente?</h3>
<p>Se non lo fosse, la stessa rotta risulterebbe diversa nei due versi. Misuriamo
quindi lo scarto fra andata e ritorno su ogni rotta con almeno 10 voli per
direzione. Con il vento modellato la mediana di quello scarto crolla, e resta
<b>stabile in stagioni diverse</b> — che è il test vero, perché d'inverno le
correnti a getto sono molto più forti.</p>
<div class=scroll><table><thead><tr><th>mese</th><th class=num>rotte</th>
<th class=num>senza vento</th><th class=num>con vento</th></tr></thead><tbody>
{gate_rows}
</tbody></table></div>

<h3>Il segnale è strutturale o è meteo?</h3>
<p>Se le classifiche fossero rumore, si rimescolerebbero ogni mese. Confrontando
la classifica delle rotte fra tutte le <b>{STAB['pairs']} coppie di mesi</b>
disponibili, la correlazione di rango resta alta ovunque: mediana
<b>{STAB['median']:.3f}</b>, peggiore <b>{STAB['worst']:.3f}</b>
({STAB['worst_pair']}), fra mesi consecutivi {STAB['consec']:.3f}.</p>
<p>Il dettaglio più informativo è che la correlazione <b>decade in ordine</b> con
la distanza temporale fra i mesi. È la firma di un segnale strutturale con una
deriva stagionale modesta: il rumore darebbe correlazioni basse ovunque, un
artefatto le darebbe uniformemente alte.</p>

<h3>I numeri reggono un confronto esterno?</h3>
<p>Aggregando le nostre traiettorie <b>come EUROCONTROL aggrega il proprio
indicatore KEA</b> — rapporto fra somme, sola porzione en-route oltre 40 NM dagli
aeroporti — otteniamo <b>+{kea:.2f}%</b> contro il <b>~{BENCH['kea_published']:.0f}%</b>
pubblicato. Stesso ordine di grandezza e stessa costruzione.</p>
<p>Restano differenze che non possiamo eliminare: loro usano dati radar
sull'area di riferimento EUROCONTROL, noi ADS-B su un sottoinsieme filtrato per
qualità, con baseline e criteri nostri. Il confronto dice «coerente», non
«identico».</p>

<h2>8. Limiti dichiarati</h2>
<ul>
<li>Misuriamo lo scarto dall'ottimo <b>teorico</b>, non l'inefficienza evitabile
(§2).</li>
<li><b>Solo le code delle classifiche sono affidabili.</b> Metà delle rotte sta
entro pochi punti dalla norma, cioè dentro l'incertezza del metodo: fra la 900ª e
la 1000ª posizione l'ordine non significa nulla. Le classifiche mostrano solo
rotte con almeno {RANK_MIN_N} voli.</li>
<li>Il periodo è il <b>solo 2026, da gennaio a luglio</b>: nessun confronto anno
su anno, e dicembre non è coperto.</li>
<li><b>Otto giorni mancano</b>: quattro perché assenti alla fonte, quattro per la
latenza dei dati meteo.</li>
<li>Le rotte contrassegnate ⚑ <b>non possono</b> volare il percorso diretto
perché lo spazio aereo è chiuso. Il divieto riguarda però i vettori europei e non
quelli di paesi terzi, quindi il valore mostrato è una media fra chi deve
aggirare e chi no.</li>
<li>La copertura ADS-B <b>non include le tratte oceaniche</b>.</li>
<li>La massa dell'aeromobile è stimata, non nota: è la principale incertezza
fisica del modello. La baseline inoltre non impone il <b>vincolo di quota
raggiungibile a pieno carico</b>: un aereo pesante deve salire per gradini,
mentre la traiettoria ideale vola l'intera crociera a quota unica. Quanto questo
pesi è misurato dal pavimento del §2-bis.</li>
<li>La traiettoria ideale vola alla <b>velocità di minimo consumo</b>. Le
compagnie volano più veloci di proposito, per rispettare gli orari: è una scelta
economica, non un'inefficienza, e finisce comunque conteggiata nella componente
verticale. È una delle voci del pavimento incomprimibile.</li>
<li><b>La ripartizione fra laterale e verticale non è univoca.</b> Correggiamo
prima il percorso e poi il profilo; l'ordine inverso attribuirebbe pesi diversi,
perché le due cose interagiscono — cambiare rotta cambia i venti incontrati e la
quota conveniente. Il totale è robusto, la sua divisione in due è una
convenzione dichiarata.</li>
<li>Il tempo di volo della traiettoria ideale è calcolato con la media
aritmetica del vento lungo il percorso, dove la grandezza esatta sarebbe la media
armonica della velocità al suolo. L'approssimazione <b>sottostima leggermente il
consumo della baseline</b>, quindi gonfia di circa mezzo punto percentuale lo
scarto che pubblichiamo: l'errore è nella direzione che ci sfavorisce
correggere.</li>
<li>Il residuo di asimmetria fra le due direzioni di una rotta è attribuito a
vettoramento, sulla base della sua correlazione con la geometria del percorso.
È un'inferenza, non una misura diretta.</li>
</ul>

<h2>9. Privacy</h2>
<p>Ogni riga pubblicata aggrega <b>almeno {MIN_N} voli</b>. Non pubblichiamo, e
non pubblicheremo, dati riferiti a un singolo volo, aeromobile o operatore. Le
classifiche riguardano rotte e aeroporti, mai persone o velivoli identificabili.</p>

<h2>10. Chi ha fatto questo, e come segnalare un errore</h2>
<div class=note>
<p><b>Non sono un professionista dell'aviazione né un climatologo.</b> Gestisco un
ricevitore ADS-B e la cosa mi sta a cuore. Quello che trovate qui è uno
<i>strumento</i> con i suoi limiti dichiarati, non uno studio d'autore.</p>
<p><b>La pipeline è stata sviluppata con l'assistenza di un'AI</b> (Claude
di Anthropic). Metodo e codice sono aperti proprio perché chi conosce il campo
possa verificarli.</p>
<p>Se un numero vi sembra sbagliato, o se rappresentate un aeroporto o una
compagnia citata e volete replicare, scrivetemi: la correzione verrà pubblicata.
È il motivo per cui questo lavoro è pubblico invece che privato.</p>
</div>

<p class=foot>
Dati traiettoria © contributori <a href="https://adsb.lol">adsb.lol</a>, licenza
<a href="https://opendatacommons.org/licenses/odbl/">ODbL</a> — i dati derivati
qui pubblicati sono distribuiti alle stesse condizioni, come la licenza richiede.
Vento: ERA5, Copernicus Climate Change Service.
Riferimenti carburante: ICAO CEC Methodology v13.1.
Modello prestazioni: OpenAP, TU Delft.<br>
{len(df):,} voli · {len(days)} giorni · {n_routes_all:,} rotte pubblicabili ·
{n_routes_rank:,} in classifica · {n_airports:,} aeroporti · generato {esc(gen)}.
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
    g_all = g[g.n >= MIN_N]      # tutte le rotte pubblicabili (soglia privacy)
    g = g[g.n >= RANK_MIN_N]     # solo queste entrano nelle classifiche
    g["co2_t"] /= 1000
    g["closed"] = [", ".join(gc_crosses_closed(a, b, coords)) for a, b in g.index]

    # ---- airports -------------------------------------------------------
    both = pd.concat([df.assign(ap=df.origin_icao), df.assign(ap=df.dest_icao)])
    ga = both.groupby("ap").agg(
        n=("d_tot", "size"), d=("d_tot", "median"),
        lat=("d_lat", "median"), vert=("d_vert", "median"),
    )
    ga = ga[ga.n >= MIN_N_AIRPORT]

    band = df.groupby("bin").agg(n=("d_tot", "size"),
                                 med=("excess_total_pct", "median"),
                                 lo=("gc_km", "min")).sort_values("lo")

    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def rrow(r, pair):
        a, b = pair
        note = (f"<span class=flag title='il percorso diretto attraversa "
                f"{esc(r.closed)}'>⚑</span>" if r.closed else "")
        return (f"<tr><td class=r>{esc(aname(a))} ↔ {esc(aname(b))}{note}"
                f"<span class=code>{esc(a)}–{esc(b)}</span></td>"
                f"<td class=num>{int(r.n):,}</td><td class=num>{r.gc:,.0f}</td>"
                f"<td class='num big {'pos' if r.d>0 else 'neg'}'>{r.d:+.0f}</td>"
                f"<td class=num>{r.lat:.0f}%</td><td class=num>{r.vert:.0f}%</td>"
                f"<td class=num>{r.co2_t:,.0f}</td></tr>")

    def arow(icao, r):
        return (f"<tr><td class=r>{esc(aname(icao))}"
                f"<span class=code>{esc(icao)}</span></td>"
                f"<td class=num>{int(r.n):,}</td>"
                f"<td class='num big {'pos' if r.d>0 else 'neg'}'>{r.d:+.1f}</td>"
                f"<td class=num>{r.lat:+.1f}</td>"
                f"<td class='num big {'pos' if r.vert>0 else 'neg'}'>{r.vert:+.1f}</td></tr>")

    worst = "\n".join(rrow(r, p) for p, r in g.sort_values("d", ascending=False).head(25).iterrows())
    best = "\n".join(rrow(r, p) for p, r in g.sort_values("d").head(15).iterrows())
    ap_worst = "\n".join(arow(i, r) for i, r in ga.sort_values("d", ascending=False).head(15).iterrows())
    ap_best = "\n".join(arow(i, r) for i, r in ga.sort_values("d").head(10).iterrows())
    by_co2 = "\n".join(rrow(r, p) for p, r in g.sort_values("co2_t", ascending=False).head(15).iterrows())
    bandrows = "\n".join(
        f"<tr><td>{esc(i)} km</td><td class=num>{int(r.n):,}</td>"
        f"<td class=num>{r.med:+.0f}%</td></tr>" for i, r in band.iterrows())
    n_closed = int((g.closed != "").sum())

    html_doc = f"""<!doctype html>
<html lang=it><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>co2gap — osservatorio CO₂ e inefficienza dei voli in Europa</title>
<style>
:root{{--bg:#0e1216;--card:#161d23;--fg:#e8eef3;--mut:#8ea3b2;--line:#243039;
--pos:#ff8a6b;--neg:#5fd0a8;--hi:#5ac8fa;--warn:#f0b429}}
@media(prefers-color-scheme:light){{:root{{--bg:#fbfcfd;--card:#fff;--fg:#16212b;
--mut:#5b6b78;--line:#e2e8ee;--pos:#c2410c;--neg:#0f766e}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}}
.wrap{{max-width:960px;margin:0 auto;padding:36px 20px 90px}}
h1{{font-size:1.8rem;margin:0 0 8px;letter-spacing:-.02em}}
h2{{font-size:1.12rem;margin:44px 0 6px;letter-spacing:-.01em}}
h2+p.hint{{margin:0 0 14px;color:var(--mut);font-size:.88rem}}
.sub{{color:var(--mut);margin:0 0 22px}}
.stats{{display:flex;flex-wrap:wrap;gap:12px;margin:22px 0}}
.stat{{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:13px 16px;flex:1;min-width:150px}}
.stat .v{{font-size:1.45rem;font-weight:650;letter-spacing:-.02em}}
.stat .l{{color:var(--mut);font-size:.8rem;margin-top:2px}}
.scroll{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse;margin:4px 0;font-size:.9rem}}
th,td{{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);white-space:nowrap}}
th{{color:var(--mut);font-weight:500;font-size:.75rem;text-transform:uppercase;letter-spacing:.05em}}
td.num{{text-align:right;font-variant-numeric:tabular-nums}}
td.big{{font-weight:650}}
td.pos{{color:var(--pos)}} td.neg{{color:var(--neg)}}
td.r{{font-weight:500;white-space:normal}}
.code{{color:var(--mut);font-size:.75rem;margin-left:7px;font-family:ui-monospace,monospace}}
.flag{{color:var(--warn);margin-left:6px;cursor:help}}
.note{{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--hi);
border-radius:8px;padding:14px 18px;color:var(--mut);font-size:.89rem;margin:16px 0}}
.note.warn{{border-left-color:var(--warn)}}
.note b{{color:var(--fg)}}
.foot{{color:var(--mut);font-size:.8rem;margin-top:44px;border-top:1px solid var(--line);padding-top:16px}}
a{{color:var(--hi)}}
</style></head><body><div class=wrap>

<h1>co2gap — osservatorio CO₂ e inefficienza dei voli in Europa</h1>
<p class=sub>Emissioni reali e <b>scarto rispetto al volo ideale</b>, calcolati dalle
traiettorie ADS-B di ogni volo, con baseline great-circle <b>al netto del vento</b>
e scomposti in componente <b>laterale</b> (percorso) e <b>verticale</b> (profilo).
Area ECAC · {esc(days[0])} → {esc(days[-1])} · {len(days)} giorni · generato {esc(gen)}.</p>

<div class=stats>
  <div class=stat><div class=v>{len(df):,}</div><div class=l>voli analizzati</div></div>
  <div class=stat><div class=v>{co2_t/1e6:,.1f} Mt</div><div class=l>CO₂ emessa</div></div>
  <div class=stat><div class=v>{excess_t/1e6:,.2f} Mt</div><div class=l>scarto dall'ottimo teorico</div></div>
  <div class=stat><div class=v>{len(g_all):,}</div><div class=l>rotte con n≥{MIN_N}</div></div>
</div>

<div class=note>
<b>Che cosa misura questo sito.</b> Per ogni volo confrontiamo il CO₂ realmente
emesso con quello di un volo ideale: stesso aeromobile, rotta diretta
great-circle, quota e velocità più efficienti per quella distanza, <b>e lo stesso
vento reale</b>. La differenza è divisa in due parti additive: la <b>laterale</b>
(aver volato più chilometri) e la <b>verticale</b> (aver volato lo stesso
percorso con un profilo di quota e velocità meno efficiente).
Sul periodo osservato: totale <b>{(lat_w+vert_w):.1f}%</b>, di cui
laterale <b>{lat_w:.1f}%</b> e verticale <b>{vert_w:.1f}%</b>.
</div>

<div class="note warn">
<b>Questo non è carburante che si può risparmiare.</b> Il volo ideale
great-circle a profilo perfetto è un limite teorico che nessun volo reale può
raggiungere: separazione fra aeromobili, struttura delle rotte, spazi aerei
vincolati e code in avvicinamento lo rendono irraggiungibile. Le stime di
inefficienza <i>recuperabile</i> pubblicate dagli enti del settore sono molto più
piccole — EUROCONTROL calcola circa 39 kg di carburante per volo recuperabili
lisciando i profili di salita e discesa, contro i circa 520 kg di scarto totale
che misuriamo qui. <b>Questo sito misura la distanza dall'ottimo teorico, non
lo spreco evitabile.</b> Il confronto per esteso è nella metodologia.
</div>

<h2>Quanta parte di questo scarto è comprimibile</h2>
<p class=hint>Ricavato dai dati stessi, non da un'assunzione.</p>
<div class=note>
<p>Un volo che vola <b>diretto</b>, in partenza <b>di notte</b> a cielo quasi
vuoto, su una tratta <b>lunga</b> dove la crociera domina, è quanto di più
vicino alla nostra traiettoria ideale un aereo di linea arrivi nella realtà.
Su {n_floor:,} voli di questo tipo lo scarto verticale resta comunque
<b>{vert_floor:.1f}%</b>.</p>
<p>Quello è il <b>pavimento</b>: non è inefficienza, è la baseline che resta
irraggiungibile. Dipende da scelte e vincoli che nessuna procedura elimina —
la velocità di crociera scelta per rispettare gli orari anziché per il minimo
consumo, la necessità di salire per gradini man mano che l'aereo alleggerisce,
i livelli di volo disponibili a intervalli discreti.</p>
<table><thead><tr><th>componente verticale</th><th class=num>punti</th></tr></thead>
<tbody>
<tr><td>mediana di tutti i voli</td><td class=num>{vert_fleet:.1f}</td></tr>
<tr><td>— pavimento, incomprimibile</td><td class=num>{vert_floor:.1f}</td></tr>
<tr><td>— <b>margine operativo</b> (traffico, routing, profilo)</td>
<td class=num><b>{vert_oper:.1f}</b></td></tr>
</tbody></table>
<p>Il pavimento misurato qui è vicino al valore che uno studio di EUROCONTROL
ottiene per la crociera confrontando ogni volo con il <i>miglior profilo
realmente osservato</i> — cioè un riferimento che quei vincoli li incorpora già.
Le due strade, indipendenti, portano allo stesso punto.</p>
</div>

<h2>Quanto pesa lo scarto fra voli comparabili</h2>
<p class=hint>Non rispetto all'ottimo teorico, che nessuno può raggiungere, ma
rispetto a quello che voli di pari lunghezza <b>già ottengono</b>.</p>
<div class=note>
<p>Se i voli che stanno <b>sopra</b> la mediana dei comparabili volassero come
quella mediana, la CO₂ evitata sarebbe <b>{sc_a:.1f} Mt all'anno</b>
({sc_a_fuel_kt:,.0f} kt di carburante) sul traffico che osserviamo. Portando al
livello del 75° percentile <b>solo il quartile peggiore</b> — l'ipotesi più
prudente — si arriva a <b>{sc_b:.1f} Mt all'anno</b>.</p>
<p>Per confronto, <b>EUROCONTROL stima in 1,1 Mt di CO₂ all'anno</b> il
recuperabile in area ECAC con le sole procedure di salita e discesa continue.
Due metodi indipendenti, stesso ordine di grandezza.</p>
<p><b>È aritmetica controfattuale, non una previsione.</b> Assume che il livello
mediano sia raggiungibile ovunque, e non lo è: alcune rotte stanno sopra la
mediana per vincoli strutturali — spazi aerei chiusi, orografia, congestione —
che nessuna procedura elimina. Misura quanto vale la <i>dispersione osservata</i>
fra voli comparabili, non quanto sia realizzabile.</p>
</div>

<h2>Confronto con l'indicatore di EUROCONTROL</h2>
<p class=hint>Stessa costruzione del KEA: rapporto fra somme, sola porzione
en-route oltre 40 NM dagli aeroporti.</p>
<div class=note>
Aggregando le nostre traiettorie <b>come fa EUROCONTROL</b>, l'estensione
en-route risulta <b>+{kea:.2f}%</b>, contro il <b>~3%</b> che EUROCONTROL
pubblica per l'Europa. Stesso ordine di grandezza e stessa costruzione, ma
<b>non</b> lo stesso numero: loro usano dati radar sull'area di riferimento
EUROCONTROL, noi ADS-B su un sottoinsieme filtrato per qualità, con baseline e
criteri nostri.
</div>

<h2>Norma europea per fascia di distanza</h2>
<p class=hint>Lo scarto grezzo cresce al diminuire della distanza, quindi una
classifica grezza ordinerebbe per brevità e non per inefficienza. Tutte le
classifiche qui sotto usano lo scarto dalla <b>mediana dei voli di pari
lunghezza</b>.</p>
<div class=scroll><table><thead><tr><th>Fascia</th><th class=num>voli</th>
<th class=num>scarto mediano</th></tr></thead><tbody>
{bandrows}
</tbody></table></div>

<h2>Rotte più distanti dalla norma</h2>
<p class=hint>Δ norma in punti percentuali rispetto ai voli di pari lunghezza.
Le classifiche usano solo rotte con almeno <b>{RANK_MIN_N}</b> voli: sotto quella
soglia il campione è troppo piccolo perché un ordinamento significhi qualcosa.
⚑ = il percorso diretto attraversa spazio aereo chiuso o evitato
({n_closed} rotte segnalate).</p>
<div class=scroll><table><thead><tr><th>Rotta</th><th class=num>voli</th>
<th class=num>km</th><th class=num>Δ norma</th><th class=num>lat.</th>
<th class=num>vert.</th><th class=num>t CO₂</th></tr></thead><tbody>
{worst}
</tbody></table></div>

<h2>Rotte più vicine all'ottimo</h2>
<div class=scroll><table><thead><tr><th>Rotta</th><th class=num>voli</th>
<th class=num>km</th><th class=num>Δ norma</th><th class=num>lat.</th>
<th class=num>vert.</th><th class=num>t CO₂</th></tr></thead><tbody>
{best}
</tbody></table></div>

<h2>Aeroporti</h2>
<p class=hint>Movimenti in arrivo e partenza, almeno {MIN_N_AIRPORT} voli.
La colonna <b>vert.</b> isola la componente di profilo, quella dove pesano le
discese anticipate e le attese in area terminale.</p>
<div class=scroll><table><thead><tr><th>Aeroporto</th><th class=num>movimenti</th>
<th class=num>Δ norma</th><th class=num>Δ lat.</th><th class=num>Δ vert.</th>
</tr></thead><tbody>
{ap_worst}
</tbody></table></div>
<p class=hint style="margin-top:18px">Gli aeroporti più vicini alla norma:</p>
<div class=scroll><table><thead><tr><th>Aeroporto</th><th class=num>movimenti</th>
<th class=num>Δ norma</th><th class=num>Δ lat.</th><th class=num>Δ vert.</th>
</tr></thead><tbody>
{ap_best}
</tbody></table></div>

<h2>Rotte per CO₂ totale</h2>
<p class=hint>Le rotte che pesano di più in assoluto, indipendentemente
dall'efficienza.</p>
<div class=scroll><table><thead><tr><th>Rotta</th><th class=num>voli</th>
<th class=num>km</th><th class=num>Δ norma</th><th class=num>lat.</th>
<th class=num>vert.</th><th class=num>t CO₂</th></tr></thead><tbody>
{by_co2}
</tbody></table></div>

<h2>Metodologia e limiti</h2>
<div class=note>
<b>Dati.</b> Traiettorie ADS-B dai dump giornalieri pubblici di
<a href="https://adsb.lol">adsb.lol</a>, licenza ODbL. Vento da ERA5
(Copernicus/ECMWF). Consumi modellati con <a href="https://openap.dev">OpenAP</a>
(TU Delft), ancorati per tipo di aeromobile alla metodologia dell'ICAO Carbon
Emissions Calculator v13.1.<br><br>

<b>Come è costruito il confronto.</b> Il volo ideale usa la quota ottima per la
distanza <i>great-circle</i>, non per quella realmente volata: altrimenti una
deviazione si guadagnerebbe di nascosto un livello di crociera migliore. Il vento
del percorso reale è campionato lungo la traccia e pesato sulla distanza.<br><br>

<b>Limiti dichiarati.</b>
(1) Misuriamo lo scarto dall'ottimo <b>teorico</b>, non l'inefficienza evitabile.
(2) Il periodo è il solo 2026, da gennaio a luglio: nessun confronto anno su anno.
(3) Otto giorni mancano — quattro perché assenti alla fonte, quattro per la
latenza dei dati meteo.
(4) <b>Solo le code delle classifiche sono affidabili</b>: metà delle rotte sta
entro pochi punti dalla norma, cioè dentro l'incertezza del metodo, e per quelle
l'ordinamento non è significativo.
(5) Le rotte contrassegnate ⚑ non <i>possono</i> volare il percorso diretto: lo
spazio aereo è chiuso. Il divieto di sorvolo riguarda però i vettori europei e
non quelli di paesi terzi, quindi il valore mostrato è una media fra chi deve
aggirare e chi no.
(6) Nessun dato riferito a un singolo volo o aeromobile viene pubblicato: ogni
riga aggrega almeno {MIN_N} voli.
(7) La copertura ADS-B non include le tratte oceaniche.<br><br>
<b><a href="metodologia.html">Metodologia completa, validazioni e confronti
esterni →</a></b>
</div>

<div class=note>
<b>Chi ha fatto questo sito.</b> Non sono un professionista dell'aviazione né un
climatologo. Gestisco un ricevitore ADS-B e la cosa mi sta a cuore. La pipeline è
stata sviluppata con l'assistenza di un'AI (Claude), e metodo e codice sono
aperti proprio perché chi conosce il campo possa verificarli e segnalare errori.
Se trovate uno sbaglio, scrivetemi: è il motivo per cui questo è pubblico.
</div>

<p class=foot>
Dati traiettoria © contributori <a href="https://adsb.lol">adsb.lol</a>, licenza
<a href="https://opendatacommons.org/licenses/odbl/">ODbL</a> — i dati derivati
qui pubblicati sono distribuiti alle stesse condizioni.
Vento: ERA5, Copernicus Climate Change Service.
Riferimenti carburante: ICAO CEC Methodology v13.1.
Modello prestazioni: OpenAP, TU Delft.<br>
{len(df):,} voli · {len(days)} giorni · {len(months)} mesi · generato {esc(gen)}.
</p>

</div></body></html>
"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html_doc)
    print(f"scritto {OUT}  ({len(html_doc)/1024:.0f} KB)")

    meth = build_methodology(df, days, months, lat_w, vert_w, kea,
                             co2_t, excess_t, len(g_all), len(g), len(ga), gen,
                             sc_a, sc_b, sc_a_fuel_kt,
                             vert_floor, vert_fleet, vert_oper, n_floor)
    OUT_METH.write_text(meth)
    print(f"scritto {OUT_METH}  ({len(meth)/1024:.0f} KB)")
    print(f"  voli {len(df):,} · giorni {len(days)} · rotte n>={MIN_N} {len(g_all):,} "
          f"· in classifica n>={RANK_MIN_N} {len(g):,} · aeroporti {len(ga):,}")
    print(f"  CO2 {co2_t/1e6:.2f} Mt · excess {excess_t/1e6:.2f} Mt "
          f"· lat {lat_w:.2f}% · vert {vert_w:.2f}% · KEA +{kea:.2f}%")
    print(f"  rotte con corridoio chiuso segnalate: {n_closed}")


if __name__ == "__main__":
    main()
