#!/usr/bin/env python3
"""
Build the first static, publishable report (phase-1 objective 4).

ONLY aggregates with n>=10 flights are shown — never per-flight or per-aircraft
rows (GDPR). Routes are unordered airport pairs; airports are ranked by mean
departure excess. Excess is wind-aware; CO2 tonnage uses per-type calibration.

Writes site/index.html (self-contained). Does NOT deploy — publication is a
separate, explicit decision.

Usage: lab-venv/bin/python lab/report.py [days...]
"""

from __future__ import annotations

import html
import sys
import statistics as st
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lab"))
from analysis import (load_flights, build_windfield, quality_gate, enrich,  # noqa: E402
                      load_calibration)

MIN_N = 10
OUT = ROOT / "site" / "index.html"

# airport display names (best-effort, from data/airports.csv)
import csv
NAMES = {}
with open(ROOT / "data/airports.csv", newline="") as f:
    for r in csv.DictReader(f):
        NAMES[r["icao"]] = r["name"]


def aname(icao):
    return NAMES.get(icao, icao)


def agg_routes(df):
    routes = defaultdict(list)
    for r in df.itertuples(index=False):
        if r.excess_wind is None or not np.isfinite(r.excess_wind):
            continue
        rk = tuple(sorted([r.origin_icao, r.dest_icao]))
        routes[rk].append((r.excess_wind, r.co2_cal_kg))
    rows = []
    for rk, vals in routes.items():
        if len(vals) < MIN_N:
            continue
        ex = [v[0] for v in vals]
        co2 = sum(v[1] for v in vals)
        rows.append((rk, len(vals), st.mean(ex), st.median(ex), co2))
    return sorted(rows, key=lambda x: -x[2])


def agg_airports(df):
    dep = defaultdict(list)
    for r in df.itertuples(index=False):
        if r.excess_wind is None or not np.isfinite(r.excess_wind):
            continue
        dep[r.origin_icao].append((r.excess_wind, r.co2_cal_kg))
    rows = []
    for icao, vals in dep.items():
        if len(vals) < MIN_N:
            continue
        ex = [v[0] for v in vals]
        rows.append((icao, len(vals), st.mean(ex), sum(v[1] for v in vals)))
    return sorted(rows, key=lambda x: -x[2])


def esc(s):
    return html.escape(str(s))


def build_html(df, loaded, calib):
    routes = agg_routes(df)
    airports = agg_airports(df)
    n_flights = len(df)
    total_co2_t = df.co2_cal_kg.sum() / 1000.0
    days_str = f"{min(loaded)} → {max(loaded)}" if loaded else "-"
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def route_rows():
        out = []
        for rk, n, mean_ex, med_ex, co2 in routes[:40]:
            a, b = rk
            out.append(f"<tr><td class=r>{esc(aname(a))} ↔ {esc(aname(b))}"
                       f"<span class=code>{esc(a)}–{esc(b)}</span></td>"
                       f"<td>{n}</td><td class=num>{mean_ex:+.1f}%</td>"
                       f"<td class=num>{med_ex:+.1f}%</td>"
                       f"<td class=num>{co2/1000:.0f}</td></tr>")
        return "\n".join(out)

    def airport_rows():
        out = []
        for icao, n, mean_ex, co2 in airports[:30]:
            out.append(f"<tr><td class=r>{esc(aname(icao))}"
                       f"<span class=code>{esc(icao)}</span></td>"
                       f"<td>{n}</td><td class=num>{mean_ex:+.1f}%</td>"
                       f"<td class=num>{co2/1000:.0f}</td></tr>")
        return "\n".join(out)

    calib_note = (", ".join(f"{esc(k)}×{v}" for k, v in sorted(calib.items()))
                  or "nessuno")

    return f"""<!doctype html>
<html lang=it><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Osservatorio CO₂ aviazione EU-Sud</title>
<style>
:root{{--bg:#0f1417;--card:#161c21;--fg:#e7edf1;--mut:#8fa3b0;--line:#26313a;--hi:#5ac8fa;--warn:#f0b429}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}}
.wrap{{max-width:900px;margin:0 auto;padding:32px 20px 80px}}
h1{{font-size:1.7rem;margin:0 0 6px}}
h2{{font-size:1.15rem;margin:38px 0 12px;border-bottom:1px solid var(--line);padding-bottom:6px}}
.sub{{color:var(--mut);margin:0 0 20px}}
.stats{{display:flex;flex-wrap:wrap;gap:14px;margin:20px 0}}
.stat{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;flex:1;min-width:140px}}
.stat .v{{font-size:1.5rem;font-weight:600}}
.stat .l{{color:var(--mut);font-size:.82rem}}
table{{width:100%;border-collapse:collapse;margin:6px 0 4px;font-size:.92rem}}
th,td{{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line)}}
th{{color:var(--mut);font-weight:500;font-size:.8rem;text-transform:uppercase;letter-spacing:.04em}}
td.num{{text-align:right;font-variant-numeric:tabular-nums}}
td.r{{font-weight:500}}
.code{{color:var(--mut);font-size:.78rem;margin-left:8px;font-family:ui-monospace,monospace}}
.note{{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--warn);border-radius:8px;padding:14px 18px;color:var(--mut);font-size:.9rem;margin:18px 0}}
.foot{{color:var(--mut);font-size:.82rem;margin-top:40px;border-top:1px solid var(--line);padding-top:16px}}
a{{color:var(--hi)}}
</style></head><body><div class=wrap>

<h1>Osservatorio CO₂ &amp; inefficienza aviazione — EU-Sud</h1>
<p class=sub>Emissioni reali e <b>excess CO₂</b> (oltre l'ottimo great-circle,
al netto del vento) da traiettorie ADS-B. Finestra dati: <b>{esc(days_str)}</b>
· {len(loaded)} giorni · generato {esc(gen)}.</p>

<div class=stats>
  <div class=stat><div class=v>{n_flights:,}</div><div class=l>voli modellati (dopo controlli qualità)</div></div>
  <div class=stat><div class=v>{total_co2_t:,.0f} t</div><div class=l>CO₂ totale stimata</div></div>
  <div class=stat><div class=v>{len(routes)}</div><div class=l>rotte con n≥{MIN_N}</div></div>
</div>

<div class=note>
<b>Come leggerlo.</b> L'<b>excess %</b> è l'emissione in più rispetto al volo
ideale great-circle dello stesso tipo, calcolato <b>con lo stesso vento</b>: isola
l'inefficienza da rotte allungate, quote non ottime e holding, non l'effetto del
vento. Valori positivi = più CO₂ dell'ottimo. Solo aggregati con <b>n≥{MIN_N}</b>
voli; nessun dato per singolo volo o aeromobile.
</div>

<h2>Rotte per excess % (top 40)</h2>
<table><thead><tr><th>Rotta</th><th>voli</th><th class=num>excess medio</th>
<th class=num>excess mediano</th><th class=num>CO₂ tot (t)</th></tr></thead>
<tbody>
{route_rows()}
</tbody></table>

<h2>Aeroporti per excess % medio in partenza (top 30)</h2>
<table><thead><tr><th>Aeroporto (partenze)</th><th>voli</th>
<th class=num>excess medio</th><th class=num>CO₂ tot (t)</th></tr></thead>
<tbody>
{airport_rows()}
</tbody></table>

<h2>Metodologia e limiti</h2>
<div class=note>
<b>Metodo.</b> Fuel/CO₂ via <a href="https://openap.dev">OpenAP</a> (TU Delft)
integrato sulla traiettoria (TAS da IAS). Baseline nominale great-circle alla
quota/Mach ottimi del tipo, con tempo di crociera a ground speed = TAS + vento
along-track (<b>ERA5</b>, Copernicus). CO₂ = fuel × 3,16.<br>
<b>Calibrazione per-tipo</b> (fattore su CO₂ assoluta, non sull'excess):
{esc(calib_note)}.<br>
<b>Limiti dichiarati.</b> Massa stimata da load factor 0,82 (±8–12% sul fuel
assoluto; l'excess % ne è quasi immune). Vento da reanalisi ~0,25° oraria.
Voli con copertura ADS-B scarsa scartati (flown≥0,9·GC). L'excess % è robusto,
le tonnellate assolute vanno lette come stima. Alcuni tipi restano provvisori.
</div>

<div class=foot>
Traiettorie © <a href="https://www.adsb.lol/">adsb.lol</a> contributors, licenza
<b>ODbL v1.0</b>. Aeroporti: OurAirports (CC0). Vento: ERA5 © Copernicus/ECMWF.
Modello emissioni: OpenAP. <b>Nessun dato pubblicato a livello di individuo.</b>
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
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(build_html(q, loaded, calib))
    print(f"wrote {OUT}  ({len(q)} flights, {len(loaded)} days)")
    print("NOT deployed — publication is a separate, explicit step.")


if __name__ == "__main__":
    main()
