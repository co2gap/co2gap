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
(7) La copertura ADS-B non include le tratte oceaniche.
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
    print(f"  voli {len(df):,} · giorni {len(days)} · rotte n>={MIN_N} {len(g_all):,} "
          f"· in classifica n>={RANK_MIN_N} {len(g):,} · aeroporti {len(ga):,}")
    print(f"  CO2 {co2_t/1e6:.2f} Mt · excess {excess_t/1e6:.2f} Mt "
          f"· lat {lat_w:.2f}% · vert {vert_w:.2f}% · KEA +{kea:.2f}%")
    print(f"  rotte con corridoio chiuso segnalate: {n_closed}")


if __name__ == "__main__":
    main()
