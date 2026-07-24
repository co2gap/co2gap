#!/usr/bin/env python3
"""
Build the first static, publishable report (phase-1 objective 4).

ONLY aggregates with n>=10 flights are shown — never per-flight or per-aircraft
rows (GDPR).

Ranking method — why we do NOT rank on raw excess%:
    excess% is strongly distance-dependent (corr ~ -0.55): a short sector is
    dominated by climb/descent and terminal vectoring, so ranking routes by raw
    excess would mostly rank them by shortness. We therefore report the
    DISTANCE-ADJUSTED excess: each flight's excess minus the median excess of
    all European flights in its distance band. Positive = worse than the
    European norm for a trip of that length. Raw excess is shown alongside, so
    nothing is hidden.

Writes site/index.html (self-contained). Does NOT deploy.

Usage: lab-venv/bin/python lab/report.py [days...]
"""

from __future__ import annotations

import csv
import html
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lab"))
from analysis import (load_flights, build_windfield, quality_gate, enrich,  # noqa: E402
                      load_calibration)

MIN_N = 10
OUT = ROOT / "site" / "index.html"
BANDS = [150, 300, 500, 800, 1200, 2000, 20000]
BAND_LABELS = ["150–300 km", "300–500 km", "500–800 km",
               "800–1200 km", "1200–2000 km", ">2000 km"]

NAMES = {}
with open(ROOT / "data/airports.csv", newline="") as f:
    for r in csv.DictReader(f):
        NAMES[r["icao"]] = r["name"]


def aname(icao):
    n = NAMES.get(icao, icao)
    for junk in (" International Airport", " Airport", " International"):
        n = n.replace(junk, "")
    return n


def esc(s):
    return html.escape(str(s))


def add_band_adjustment(q):
    q = q.copy()
    q["band"] = pd.cut(q.gc_km, BANDS, labels=BAND_LABELS)
    band_med = q.groupby("band", observed=True).excess_wind.median()
    q["excess_adj"] = q.excess_wind - q.band.map(band_med).astype(float)
    return q, band_med


def agg_routes(q):
    routes = defaultdict(list)
    for r in q.itertuples(index=False):
        rk = tuple(sorted([r.origin_icao, r.dest_icao]))
        routes[rk].append((r.excess_wind, r.excess_adj, r.co2_cal_kg, r.gc_km))
    rows = []
    for rk, v in routes.items():
        if len(v) < MIN_N:
            continue
        rows.append({
            "pair": rk, "n": len(v),
            "ex": float(np.median([x[0] for x in v])),
            "adj": float(np.median([x[1] for x in v])),
            "co2_t": sum(x[2] for x in v) / 1000.0,
            "gc": float(np.median([x[3] for x in v])),
        })
    return rows


def agg_airports(q):
    dep = defaultdict(list)
    for r in q.itertuples(index=False):
        dep[r.origin_icao].append((r.excess_wind, r.excess_adj, r.co2_cal_kg))
    rows = []
    for icao, v in dep.items():
        if len(v) < MIN_N:
            continue
        rows.append({
            "icao": icao, "n": len(v),
            "ex": float(np.median([x[0] for x in v])),
            "adj": float(np.median([x[1] for x in v])),
            "co2_t": sum(x[2] for x in v) / 1000.0,
        })
    return rows


def build_html(q, loaded, calib, band_med):
    routes = agg_routes(q)
    airports = agg_airports(q)
    by_adj = sorted(routes, key=lambda r: -r["adj"])
    by_co2 = sorted(routes, key=lambda r: -r["co2_t"])
    ap_by_adj = sorted(airports, key=lambda r: -r["adj"])
    total_co2_t = q.co2_cal_kg.sum() / 1000.0
    days_str = f"{min(loaded)} → {max(loaded)}"
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def rrow(r, adj=True):
        a, b = r["pair"]
        val = (f"<td class='num big {'pos' if r['adj']>0 else 'neg'}'>{r['adj']:+.0f}</td>"
               if adj else "")
        return (f"<tr><td class=r>{esc(aname(a))} ↔ {esc(aname(b))}"
                f"<span class=code>{esc(a)}–{esc(b)}</span></td>"
                f"<td class=num>{r['n']}</td><td class=num>{r['gc']:.0f}</td>"
                f"{val}<td class=num>{r['ex']:+.0f}%</td>"
                f"<td class=num>{r['co2_t']:,.0f}</td></tr>")

    def arow(r):
        return (f"<tr><td class=r>{esc(aname(r['icao']))}"
                f"<span class=code>{esc(r['icao'])}</span></td>"
                f"<td class=num>{r['n']}</td>"
                f"<td class='num big {'pos' if r['adj']>0 else 'neg'}'>{r['adj']:+.0f}</td>"
                f"<td class=num>{r['ex']:+.0f}%</td>"
                f"<td class=num>{r['co2_t']:,.0f}</td></tr>")

    band_rows = "\n".join(
        f"<tr><td>{esc(lbl)}</td><td class=num>{int((q.band==lbl).sum()):,}</td>"
        f"<td class=num>{band_med[lbl]:+.0f}%</td></tr>"
        for lbl in BAND_LABELS if lbl in band_med.index)

    calib_note = ", ".join(f"{esc(k)} ×{v}" for k, v in sorted(calib.items())) or "nessuno"

    return f"""<!doctype html>
<html lang=it><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Osservatorio CO₂ e inefficienza — aviazione EU-Sud</title>
<style>
:root{{--bg:#0e1216;--card:#161d23;--fg:#e8eef3;--mut:#8ea3b2;--line:#243039;
--pos:#ff8a6b;--neg:#5fd0a8;--hi:#5ac8fa;--warn:#f0b429}}
@media(prefers-color-scheme:light){{:root{{--bg:#fbfcfd;--card:#fff;--fg:#16212b;
--mut:#5b6b78;--line:#e2e8ee;--pos:#c2410c;--neg:#0f766e}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}}
.wrap{{max-width:920px;margin:0 auto;padding:36px 20px 90px}}
h1{{font-size:1.75rem;margin:0 0 8px;letter-spacing:-.02em}}
h2{{font-size:1.1rem;margin:42px 0 6px;letter-spacing:-.01em}}
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
.note{{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--hi);
border-radius:8px;padding:14px 18px;color:var(--mut);font-size:.89rem;margin:16px 0}}
.note.warn{{border-left-color:var(--warn)}}
.note b{{color:var(--fg)}}
.foot{{color:var(--mut);font-size:.8rem;margin-top:44px;border-top:1px solid var(--line);padding-top:16px}}
a{{color:var(--hi)}}
</style></head><body><div class=wrap>

<h1>Osservatorio CO₂ e inefficienza — aviazione EU-Sud</h1>
<p class=sub>Emissioni reali e <b>excess CO₂</b> calcolati dalle traiettorie ADS-B
di ogni volo, con baseline great-circle <b>al netto del vento</b>.
Finestra: <b>{esc(days_str)}</b> · {len(loaded)} giorni · generato {esc(gen)}.</p>

<div class=stats>
  <div class=stat><div class=v>{len(q):,}</div><div class=l>voli modellati</div></div>
  <div class=stat><div class=v>{total_co2_t:,.0f} t</div><div class=l>CO₂ totale stimata</div></div>
  <div class=stat><div class=v>{len(routes)}</div><div class=l>rotte con n≥{MIN_N}</div></div>
  <div class=stat><div class=v>{len(airports)}</div><div class=l>aeroporti con n≥{MIN_N}</div></div>
</div>

<div class=note>
<b>Che cosa è l'“excess CO₂”.</b> È l'emissione in più rispetto al volo ideale
sulla rotta diretta (great-circle), dello stesso tipo di aeromobile, alla quota
di crociera più efficiente per quella distanza e <b>con lo stesso vento</b>.
Isola quindi l'inefficienza da rotte allungate, livelli non ottimali e attese —
non l'effetto del vento, che è già scontato da entrambi i lati del confronto.
</div>

<div class=note warn>
<b>Perché classifichiamo sull'excess “normalizzato”.</b> L'excess grezzo dipende
molto dalla distanza: un volo corto è dominato da salita, discesa e manovre di
avvicinamento, quindi una classifica sull'excess grezzo ordinerebbe le rotte
soprattutto per <i>brevità</i>. La colonna <b>Δ norma</b> confronta ogni volo con
la <b>mediana europea dei voli di pari lunghezza</b>: <b>+10</b> significa dieci
punti percentuali peggio di un volo comparabile. L'excess grezzo resta in tabella.
</div>

<h2>Norma europea per fascia di distanza</h2>
<p class=hint>La linea di riferimento usata per la normalizzazione. È il metro, non un giudizio.</p>
<div class=scroll><table><thead><tr><th>Fascia</th><th class=num>voli</th>
<th class=num>excess mediano</th></tr></thead><tbody>
{band_rows}
</tbody></table></div>

<h2>Rotte meno efficienti rispetto alla norma</h2>
<p class=hint>Top 30 per Δ norma. Solo rotte con almeno {MIN_N} voli nella finestra.</p>
<div class=scroll><table><thead><tr><th>Rotta</th><th class=num>voli</th>
<th class=num>km</th><th class=num>Δ norma</th><th class=num>excess</th>
<th class=num>CO₂ (t)</th></tr></thead><tbody>
{chr(10).join(rrow(r) for r in by_adj[:30])}
</tbody></table></div>

<h2>Rotte più efficienti rispetto alla norma</h2>
<p class=hint>Le dieci migliori: utile come controprova che la metrica separa i due estremi.</p>
<div class=scroll><table><thead><tr><th>Rotta</th><th class=num>voli</th>
<th class=num>km</th><th class=num>Δ norma</th><th class=num>excess</th>
<th class=num>CO₂ (t)</th></tr></thead><tbody>
{chr(10).join(rrow(r) for r in by_adj[-10:][::-1])}
</tbody></table></div>

<h2>Aeroporti per inefficienza in partenza</h2>
<p class=hint>Top 25 per Δ norma delle partenze — indica congestione e vettoramenti in uscita.</p>
<div class=scroll><table><thead><tr><th>Aeroporto</th><th class=num>partenze</th>
<th class=num>Δ norma</th><th class=num>excess</th><th class=num>CO₂ (t)</th></tr></thead><tbody>
{chr(10).join(arow(r) for r in ap_by_adj[:25])}
</tbody></table></div>

<h2>Rotte per CO₂ totale</h2>
<p class=hint>Volume assoluto nella finestra: qui pesano traffico e distanza, non l'efficienza.</p>
<div class=scroll><table><thead><tr><th>Rotta</th><th class=num>voli</th>
<th class=num>km</th><th class=num>excess</th><th class=num>CO₂ (t)</th></tr></thead><tbody>
{chr(10).join(rrow(r, adj=False) for r in by_co2[:20])}
</tbody></table></div>

<h2>Metodologia</h2>
<div class=note>
<b>Dati.</b> Traiettorie ADS-B dei dump storici giornalieri adsb.lol. Un volo entra
nell'analisi solo se: origine e destinazione riconosciute (aeroporto entro 8 km),
distanza volata ≥ 0,9 × great-circle, copertura ≥ 85% del tempo di volo,
great-circle ≥ 150 km.<br><br>
<b>Modello.</b> Consumo istantaneo con <a href="https://openap.dev">OpenAP</a>
(TU Delft) integrato sulla traiettoria reale; velocità vera ricavata dalla
velocità indicata (quindi non contaminata dal vento). Massa iniziale stimata
iterativamente da peso operativo, carico pagante a load factor 0,82 e riserve.
CO₂ = carburante × 3,16.<br><br>
<b>Baseline.</b> Volo nominale sul great-circle, stesso tipo, alla quota che
minimizza il consumo per quella distanza, con tempo di crociera calcolato alla
velocità al suolo (vento <b>ERA5</b> Copernicus lungo la rotta, all'ora del volo).<br><br>
<b>Calibrazione per tipo</b> (agisce sulle tonnellate, non sull'excess, perché si
semplifica fra volo reale e baseline): {esc(calib_note)}.
</div>

<div class=note warn>
<b>Limiti dichiarati.</b>
La massa è stimata, non nota: incide ±8–12% sulle tonnellate assolute, molto meno
sull'excess percentuale.
I fattori di calibrazione sono <i>ancorati</i> a valori di consumo pubblicati, non
validati in modo indipendente; la prova a favore del modello è che i tipi
<i>non</i> calibrati (A320, B738, A319, A321 — la maggioranza dei voli) cadono da
soli entro il 9% dei valori pubblicati. I widebody con pochi voli nella finestra
restano provvisori.
Il vento è una rianalisi a ~0,25°, non il vento puntuale incontrato.
L'excess include manovre di avvicinamento e attese: su tratte brevi pesa molto,
ed è la ragione della normalizzazione per distanza.
Una finestra di {len(loaded)} giorni cattura una situazione meteo e di traffico
specifica: le classifiche vanno lette come fotografia del periodo, non come
giudizio strutturale.
</div>

<div class=foot>
Traiettorie © <a href="https://www.adsb.lol/">adsb.lol</a> contributors, licenza
<b>ODbL v1.0</b> — questo lavoro è un <i>Produced Work</i> ai sensi della licenza.
Aeroporti: OurAirports (CC0). Vento: ERA5 © Copernicus/ECMWF. Modello: OpenAP.<br>
<b>Tutti i dati sono aggregati per rotta o aeroporto, con almeno {MIN_N} voli.
Nessuna informazione su singoli voli, aeromobili o persone.</b>
</div>

</div></body></html>"""


def main():
    days = sys.argv[1:]
    df, loaded = load_flights(days)
    q = quality_gate(df)
    wf = build_windfield(loaded)
    calib = load_calibration()
    q = enrich(q, wf, calib)
    q = q[np.isfinite(q.excess_wind)]
    q, band_med = add_band_adjustment(q)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(build_html(q, loaded, calib, band_med))
    print(f"wrote {OUT}  ({len(q):,} flights, {len(loaded)} days, "
          f"{OUT.stat().st_size/1024:.0f} KB)")
    print("NOT deployed — publication is a separate, explicit decision.")


if __name__ == "__main__":
    main()
