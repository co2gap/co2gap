#!/usr/bin/env python3
"""La pagina di contesto — le uniche cifre del sito che non escono dalla pipeline.

Vive in un modulo suo apposta. Tutto il resto di site_build.py stampa numeri
che la macchina ha appena calcolato e che chiunque puo' ricalcolare dai parquet
pubblicati; questa pagina cita GCB, Lee, Klower, l'OCSE ed EUROCONTROL, cioe'
grandezze che nessuno qui puo' verificare rieseguendo qualcosa.

Da cio' discendono due regole, e sono il motivo per cui questo file esiste:

1. le cifre esterne stanno TUTTE in data/context/external.json, ognuna con la
   propria fonte e la data in cui e' stata verificata. Nessuna e' scritta qui
   dentro;
2. freeze_check NON le protegge — confronta il sito con il sito precedente, non
   con le fonti — quindi vanno riverificate a mano a ogni release. Il passo e'
   in DEPLOY.md, ed e' l'unico punto del sito dove il cancello automatico non
   arriva.

Le cifre di co2gap che la pagina cita (il 12,1%, la scomposizione, i voli) NON
stanno nel json: arrivano come argomenti da site_build.py, calcolate dai dati
come ogni altra cifra del sito.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTERNAL = ROOT / "data/context/external.json"


# --------------------------------------------------------------- numeri --
def n(v, dec=1):
    """Numero nella convenzione inglese del sito: 1,833,127 e 43.2."""
    return f"{v:,.{dec}f}"


def sc(d0, d1, r0, r1):
    return lambda v: r0 + (v - d0) / (d1 - d0) * (r1 - r0)


def poly(pts):
    return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)


# ---------------------------------------------------------------- stile --
STYLE_CONTEXT = """
body.context .wrap,body.context .top .wrap{max-width:1000px}
body.context .col{max-width:66ch}
:root{--c1:#2a78d6;--c2:#eb6834;--c3:#7a4fbf;--c4:#2e8b57}
@media(prefers-color-scheme:dark){:root{--c1:#3987e5;--c2:#d95926;--c3:#9a79de;--c4:#2fa377}}
.cfig{margin:30px 0;background:var(--card);border:1px solid var(--line);border-radius:12px}
.cfig figcaption{padding:16px 20px 0}
.cfig .eyebrow{margin-bottom:6px}
.cfig h3{margin:0;font-size:1.12rem;line-height:1.3;letter-spacing:-.01em}
.cplot{position:relative;padding:14px 10px 4px;overflow-x:auto}
.cplot svg{width:100%;height:auto;display:block;min-width:520px;overflow:visible}
.cnote{border-top:1px solid var(--line);padding:16px 20px 18px;font-size:.94rem}
.cnote p{margin:0 0 10px;max-width:74ch}
.cnote .src{font-size:.82rem;color:var(--mut);margin:0;line-height:1.5}
.clegend{display:flex;flex-wrap:wrap;gap:16px;list-style:none;margin:10px 0 0;padding:0 20px;
font-size:.85rem;color:var(--mut)}
.clegend li{display:flex;align-items:center;gap:7px}
.sw{width:11px;height:11px;border-radius:2px;display:inline-block}
.sw.c1{background:var(--c1)}.sw.c2{background:var(--c2)}
.sw.c3{background:var(--c3)}.sw.c4{background:var(--c4)}
.cgrid{stroke:var(--grid);stroke-width:1}
.caxis{stroke:var(--axis);stroke-width:1}
.ctick,.crowlab,.cval,.cendlab,.cendsub,.cpaneltitle,.cpanelsub,.cannot{
font-variant-numeric:tabular-nums}
.ctick{font-size:11.5px;fill:var(--mut)}
.caxlab{font-size:10.5px;fill:var(--mut);letter-spacing:.08em;text-transform:uppercase}
.crowlab{font-size:12.5px;fill:var(--fg)}
.cval{font-size:12.5px;fill:var(--mut);font-weight:600}
.cpaneltitle{font-size:14px;fill:var(--fg);font-weight:620}
.cpanelsub{font-size:10.5px;fill:var(--mut)}
.cendlab{font-size:12.5px;font-weight:620}
.cendsub{font-size:11px;fill:var(--mut)}
.cannot{font-size:11px;fill:var(--mut)}
.cline{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.cline.c1{stroke:var(--c1)}.cline.c2{stroke:var(--c2)}
.cline.c3{stroke:var(--c3)}.cline.c4{stroke:var(--c4)}
.carea.c2{fill:var(--c2);fill-opacity:.14;stroke:none}
.cdot.c1{fill:var(--c1)}.cdot.c2{fill:var(--c2)}
.cdot.c3{fill:var(--c3)}.cdot.c4{fill:var(--c4)}
.cendlab.c1{fill:var(--c1)}.cendlab.c2{fill:var(--c2)}
.cendlab.c3{fill:var(--c3)}.cendlab.c4{fill:var(--c4)}
.cbar{stroke:var(--card);stroke-width:2}
.cbar.c2{fill:var(--c2)}.cbar.c3{fill:var(--c3)}.cbar.c4{fill:var(--c4)}
.cbar:hover{opacity:.82}
.cval.c2{fill:var(--c2)}.cval.c4{fill:var(--c4)}
.cannotdot{fill:var(--mut)}
.cannotline{stroke:var(--axis);stroke-width:1}
.ccross{stroke:var(--mut);stroke-width:1;stroke-dasharray:3 3}
.ctip{position:absolute;pointer-events:none;z-index:5;background:var(--card);
border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:.8rem;
line-height:1.55;color:var(--fg);white-space:nowrap;box-shadow:0 8px 24px rgba(0,0,0,.18);
transform:translate(-50%,-115%);font-variant-numeric:tabular-nums}
.ctip .ty{color:var(--mut);display:block;margin-bottom:3px}
.ctip .tr{display:flex;gap:14px;justify-content:space-between}
.ctip .tn{color:var(--mut)}
.ctbl{margin:0 0 14px}
.ctbl summary{font-size:.82rem;color:var(--mut);cursor:pointer;list-style:none;
display:flex;align-items:center;gap:7px}
.ctbl summary::-webkit-details-marker{display:none}
.ctbl summary::before{content:"+";color:var(--c2);font-weight:700}
.ctbl[open] summary::before{content:"\\2212"}
.ctbl summary:hover{color:var(--fg)}
.ctblwrap{max-height:300px;overflow:auto;margin-top:10px;border:1px solid var(--line);
border-radius:8px}
.ctblwrap table{border-collapse:collapse;width:100%;font-size:.82rem;
font-variant-numeric:tabular-nums}
.ctblwrap th{position:sticky;top:0;background:var(--card);text-align:left;font-weight:600;
color:var(--mut);padding:7px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
.ctblwrap td{padding:5px 12px;border-bottom:1px solid var(--grid);color:var(--mut);
white-space:nowrap}
.ctblwrap td:first-child{color:var(--fg)}
.ctiles{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line);
border:1px solid var(--line);border-radius:12px;overflow:hidden;margin:26px 0 34px}
.ctile{background:var(--card);padding:16px 16px 18px}
.ctile b{display:block;font-size:1.9rem;line-height:1;letter-spacing:-.02em;margin-bottom:8px;
font-variant-numeric:tabular-nums}
.ctile span{font-size:.86rem;color:var(--fg);line-height:1.4;display:block}
.ctile em{font-size:.74rem;font-style:normal;color:var(--mut);display:block;margin-top:8px}
.ctile.t1 b{color:var(--c2)}.ctile.t2 b{color:var(--c3)}
.ctile.t3 b{color:var(--c1)}.ctile.t4 b{color:var(--c4)}
.cpull{font-size:1.16rem;line-height:1.5;border-left:3px solid var(--c2);padding:2px 0 2px 18px;
margin:26px 0}
@media(max-width:760px){.ctiles{grid-template-columns:repeat(2,1fr)}}
@media(max-width:430px){.ctiles{grid-template-columns:1fr}}
.csrc th,.csrc td{white-space:normal;vertical-align:top}
.csrc td:first-child{width:34%;color:var(--fg)}
.csrc td{color:var(--mut)}
@media print{.cplot{overflow:visible}.ctbl{display:none}}
"""


# ------------------------------------------------------------- costruttori --
CHARTS: dict = {}


def datatable(headers, rows, caption):
    """I numeri della figura in forma leggibile, sotto la figura stessa.

    Un SVG letto come testo — da un lettore di schermo, da un motore di ricerca
    o da un modello — e' una sequenza di tacche senza struttura. Questi sono gli
    stessi valori che disegnano il grafico, formattati dalla stessa funzione.
    """
    th = "".join(f"<th>{h}</th>" for h in headers)
    tr = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return (f'<details class=ctbl><summary>Data behind this chart — {len(rows)} rows</summary>'
            f'<div class=ctblwrap><table><thead><tr>{th}</tr></thead>'
            f'<tbody>{tr}</tbody></table></div></details>')


def figure(cid, title, kicker, svg, note, source, legend="", table=""):
    return (f'<figure class=cfig id="fig-{cid}">'
            f'<figcaption><p class=eyebrow>{kicker}</p><h3>{title}</h3></figcaption>'
            f'{legend}<div class=cplot>{svg}<div class=ctip hidden></div></div>'
            f'<div class=cnote><p>{note}</p>{table}<p class=src>{source}</p></div></figure>')


def legend(items):
    li = "".join(f'<li><span class="sw {c}"></span>{lab}</li>' for lab, c in items)
    return f'<ul class=clegend>{li}</ul>'


def line_fig(cid, title, kicker, series, years, ydom, yticks, yfmt, xticks,
             note, source, w=880, h=380, pad=(26, 140, 40, 58), ylab="", annot=()):
    t, r, b, l = pad
    x = sc(years[0], years[-1], l, w - r)
    y = sc(ydom[0], ydom[1], h - b, t)
    out = [f'<svg viewBox="0 0 {w} {h}" role=img aria-label="{title}" data-cid="{cid}">']
    for tv in yticks:
        yy = y(tv)
        out.append(f'<line class=cgrid x1="{l}" x2="{w-r}" y1="{yy:.1f}" y2="{yy:.1f}"/>')
        out.append(f'<text class=ctick x="{l-10}" y="{yy+4:.1f}" text-anchor=end>{yfmt(tv)}</text>')
    for tv in xticks:
        out.append(f'<text class=ctick x="{x(tv):.1f}" y="{h-b+22}" text-anchor=middle>{tv}</text>')
    out.append(f'<line class=caxis x1="{l}" x2="{w-r}" y1="{y(ydom[0]):.1f}" y2="{y(ydom[0]):.1f}"/>')
    if ylab:
        out.append(f'<text class=caxlab x="{l-10}" y="{t-8}" text-anchor=end>{ylab}</text>')
    js = []
    for s in series:
        pts = [(x(yr), y(v)) for yr, v in zip(s["years"], s["values"])]
        if s.get("area"):
            ar = pts + [(pts[-1][0], y(ydom[0])), (pts[0][0], y(ydom[0]))]
            out.append(f'<path class="carea {s["cls"]}" d="{poly(ar)} Z"/>')
        out.append(f'<path class="cline {s["cls"]}" d="{poly(pts)}"/>')
        lx, ly = pts[-1]
        dy = s.get("dy", 0)
        out.append(f'<circle class="cdot {s["cls"]}" cx="{lx:.1f}" cy="{ly:.1f}" r="4"/>')
        out.append(f'<text class="cendlab {s["cls"]}" x="{lx+11:.1f}" y="{ly+4+dy:.1f}">{s["label"]}</text>')
        if s.get("sub"):
            out.append(f'<text class=cendsub x="{lx+11:.1f}" y="{ly+20+dy:.1f}">{s["sub"]}</text>')
        js.append({"cls": s["cls"], "name": s["name"], "years": s["years"],
                   "xs": [round(p[0], 1) for p in pts], "ys": [round(p[1], 1) for p in pts],
                   "vals": [s["fmt"](v) for v in s["values"]]})
    for ax, av, atxt, adx, ady in annot:
        px, py = x(ax), y(av)
        out.append(f'<circle class=cannotdot cx="{px:.1f}" cy="{py:.1f}" r="3.5"/>')
        out.append(f'<line class=cannotline x1="{px:.1f}" y1="{py:.1f}" '
                   f'x2="{px+adx:.1f}" y2="{py+ady:.1f}"/>')
        out.append(f'<text class=cannot x="{px+adx-6:.1f}" y="{py+ady+4:.1f}" '
                   f'text-anchor=end>{atxt}</text>')
    out.append(f'<rect class=chit x="{l}" y="{t}" width="{w-r-l}" height="{h-b-t}" fill="transparent"/>')
    out.append(f'<line class=ccross x1=0 x2=0 y1="{t}" y2="{h-b}" style="display:none"/>')
    out.append("</svg>")
    CHARTS[cid] = {"series": js, "top": t}
    allyears = sorted({yr for s in series for yr in s["years"]})
    lk = [dict(zip(s["years"], s["values"])) for s in series]
    rows = [[str(yr)] + [(s["fmt"](d[yr]) if yr in d else "—") for s, d in zip(series, lk)]
            for yr in allyears]
    tbl = datatable(["Year"] + [s["name"] for s in series], rows, "")
    return figure(cid, title, kicker, "".join(out), note, source, table=tbl)


def bar_fig(cid, title, kicker, rows, note, source, unit, w=880, barh=34, gap=12,
            l=248, r=96, colorcls=lambda row: "c4", maxv=None, leg=""):
    h = 20 + len(rows) * (barh + gap)
    mx = maxv or max(v for _, v in rows)
    x = sc(0, mx, l, w - r)
    out = [f'<svg viewBox="0 0 {w} {h}" role=img aria-label="{title}">']
    for i, (lab, v) in enumerate(rows):
        yy = 14 + i * (barh + gap)
        out.append(f'<text class=crowlab x="{l-14}" y="{yy+barh*0.68:.1f}" text-anchor=end>{lab}</text>')
        out.append(f'<rect class="cbar {colorcls((lab, v))}" x="{l}" y="{yy}" '
                   f'width="{max(x(v)-l,2):.1f}" height="{barh}" rx=3 '
                   f'data-tip="{lab} · {n(v, 1)} {unit}"/>')
        out.append(f'<text class=cval x="{x(v)+10:.1f}" y="{yy+barh*0.68:.1f}">{n(v,1)}</text>')
    out.append("</svg>")
    tbl = datatable(["Item", unit], [[lab, n(v, 1)] for lab, v in rows], "")
    return figure(cid, title, kicker, "".join(out), note, source, leg, tbl)


def col_fig(cid, title, kicker, panels, note, source, w=880, h=300):
    """Piccoli multipli: ogni pannello ha la sua scala e la dichiara."""
    pw = w / len(panels)
    out = [f'<svg viewBox="0 0 {w} {h}" role=img aria-label="{title}">']
    for pi, p in enumerate(panels):
        ox, l, r, t, b = pi * pw, 46, 26, 52, 42
        y = sc(0, p["max"], h - b, t)
        step = (pw - l - r) / len(p["rows"])
        bw = step * 0.62
        out.append(f'<text class=cpaneltitle x="{ox+l:.1f}" y=20>{p["title"]}</text>')
        out.append(f'<text class=cpanelsub x="{ox+l:.1f}" y=38>{p["sub"]}</text>')
        for tv in p["ticks"]:
            yy = y(tv)
            out.append(f'<line class=cgrid x1="{ox+l:.1f}" x2="{ox+pw-r:.1f}" y1="{yy:.1f}" y2="{yy:.1f}"/>')
            out.append(f'<text class=ctick x="{ox+l-8:.1f}" y="{yy+4:.1f}" text-anchor=end>{n(tv,0)}</text>')
        for i, (lab, v) in enumerate(p["rows"]):
            cx, yy = ox + l + step * i + (step - bw) / 2, y(v)
            out.append(f'<rect class="cbar {p["cls"]}" x="{cx:.1f}" y="{yy:.1f}" width="{bw:.1f}" '
                       f'height="{h-b-yy:.1f}" rx=3 data-tip="{lab} · {n(v, p.get("dec",1))} {p["unit"]}"/>')
            out.append(f'<text class=ctick x="{cx+bw/2:.1f}" y="{h-b+20}" text-anchor=middle>{lab}</text>')
            out.append(f'<text class="cval {p["cls"]}" x="{cx+bw/2:.1f}" y="{yy-8:.1f}" '
                       f'text-anchor=middle>{n(v, p.get("dec",1))}</text>')
        out.append(f'<line class=caxis x1="{ox+l:.1f}" x2="{ox+pw-r:.1f}" y1="{h-b}" y2="{h-b}"/>')
    out.append("</svg>")
    tbl = "".join(datatable([p["title"], p["unit"]],
                            [[lab, n(v, p.get("dec", 1))] for lab, v in p["rows"]], "")
                  for p in panels)
    return figure(cid, title, kicker, "".join(out), note, source, table=tbl)


JS = """
(function(){
var CH=window.__CTXCHARTS__||{},NS="http://www.w3.org/2000/svg";
function place(tip,svg,x,y){var r=svg.getBoundingClientRect(),vb=svg.viewBox.baseVal,
k=r.width/vb.width,host=tip.parentElement.getBoundingClientRect();
tip.style.left=(r.left-host.left+x*k)+"px";tip.style.top=(r.top-host.top+y*k)+"px";}
document.querySelectorAll("svg[data-cid]").forEach(function(svg){
var cfg=CH[svg.dataset.cid];if(!cfg)return;
var tip=svg.parentElement.querySelector(".ctip"),cross=svg.querySelector(".ccross"),
dots=cfg.series.map(function(s){var c=document.createElementNS(NS,"circle");
c.setAttribute("r",4.5);c.setAttribute("class","cdot "+s.cls);
c.setAttribute("stroke","var(--card)");c.setAttribute("stroke-width","2");
c.style.display="none";svg.appendChild(c);return c;});
var zone=svg.querySelector(".chit");
zone.addEventListener("pointermove",function(ev){
var p=svg.createSVGPoint();p.x=ev.clientX;p.y=ev.clientY;
p=p.matrixTransform(svg.getScreenCTM().inverse());
var ref=cfg.series[0],i=0,best=Infinity;
ref.xs.forEach(function(x,k){var d=Math.abs(x-p.x);if(d<best){best=d;i=k;}});
var year=ref.years[i],rows="";
cross.setAttribute("x1",ref.xs[i]);cross.setAttribute("x2",ref.xs[i]);cross.style.display="";
cfg.series.forEach(function(s,si){var j=s.years.indexOf(year);
if(j<0){dots[si].style.display="none";return;}
dots[si].setAttribute("cx",s.xs[j]);dots[si].setAttribute("cy",s.ys[j]);dots[si].style.display="";
rows+='<span class=tr><span class=tn>'+s.name+'</span><b>'+s.vals[j]+'</b></span>';});
tip.innerHTML='<span class=ty>'+year+'</span>'+rows;tip.hidden=false;
place(tip,svg,ref.xs[i],cfg.top+6);});
zone.addEventListener("pointerleave",function(){tip.hidden=true;cross.style.display="none";
dots.forEach(function(d){d.style.display="none";});});});
document.querySelectorAll(".cplot").forEach(function(plot){
var tip=plot.querySelector(".ctip");
plot.querySelectorAll("[data-tip]").forEach(function(el){
el.addEventListener("pointerenter",function(){tip.innerHTML=el.dataset.tip;tip.hidden=false;
var b=el.getBBox();place(tip,el.ownerSVGElement,b.x+b.width/2,b.y);});
el.addEventListener("pointerleave",function(){tip.hidden=true;});});});
})();
"""


def build(*, meta, nav, footnav, style, term, release, method_version, n_flights,
          days, lat_w, vert_w):
    """La pagina completa. Le cifre di co2gap arrivano da fuori, mai digitate qui."""
    ext = json.loads(EXTERNAL.read_text(encoding="utf-8"))
    S, FIG = ext["series"], ext["figures"]

    def series(key, scale=1.0, since=None):
        v = {int(y): x * scale for y, x in S[key]["values"].items()}
        if since:
            v = {y: x for y, x in v.items() if y >= since}
        ys = sorted(v)
        return ys, [v[y] for y in ys]

    YG, G_TOT = series("world_co2_total_t", 1 / 1e9, since=1940)
    fossil = {int(y): x for y, x in S["world_co2_fossil_t"]["values"].items()}
    YA, A_SHARE = series("aviation_share_pct", since=1940)
    tot_by_year = dict(zip(YG, G_TOT))
    A_ABS = [sh / 100 * tot_by_year[y] for y, sh in zip(YA, A_SHARE)]
    av_by_year = dict(zip(YA, A_ABS))
    YE, PKM = series("aviation_pkm_bn")
    _, INTENS = series("aviation_intensity_g_pkm")
    _, AVCO2 = series("aviation_co2_gt")
    oecd = {int(y): x / 1e6 for y, x in S["oecd_passenger_aviation_t"]["values"].items()}
    idx = lambda v: [x / v[0] * 100 for x in v]

    erf = FIG["erf_2018_mwm2"]
    E, ENET = erf["values"], erf["net"]
    elo, ehi = erf["range_5_95"]
    flights = sorted((int(y), v) for y, v in FIG["ecac_flights_m"]["values"].items())
    ecco2 = sorted((int(y), v) for y, v in FIG["eurocontrol_gate_to_gate_mt"]["values"].items())
    vs = FIG["vs2019_pct"]["values"]
    Q = {k: v["v"] for k, v in FIG["quoted"]["values"].items()}
    # Anche il 9% e' una cifra altrui: sta nel json con la sua fonte, non fra
    # i BENCH del sito, che raccolgono i riferimenti del metodo.
    benefit_pool = Q["atm_benefit_pool_pct"]

    # --- cifre citate nel testo, tutte derivate dalle serie qui sopra --------
    tot24, av19, sh19, sh40 = G_TOT[-1], av_by_year[2019], A_SHARE[YA.index(2019)], A_SHARE[0]
    fossil_share19 = av19 * 1e9 / fossil[2019] * 100
    one_in = 100 / sh19
    share_mult = sh19 / sh40
    pkm_mult = PKM[YE.index(2019)] / PKM[0]
    int_drop = (1 - INTENS[YE.index(2019)] / INTENS[0]) * 100
    co2_mult = AVCO2[YE.index(2019)] / AVCO2[0]
    total_gap = lat_w + vert_w
    erf_co2_share = E["co2"] / ENET * 100
    erf_contrail_share = E["contrails"] / ENET * 100
    ec_growth = (ecco2[-1][1] / ecco2[0][1] - 1) * 100
    fl_growth = (flights[-1][1] / dict(flights)[2022] - 1) * 100

    F1 = line_fig(
        "totale", "Global CO₂ emissions, and the part that flies",
        f"{YG[0]} – {YG[-1]} · billion tonnes",
        [dict(cls="c1", name="Total CO₂", label="Total CO₂", sub=f"{n(tot24)} Gt in {YG[-1]}",
              years=YG, values=G_TOT, fmt=lambda v: f"{n(v)} Gt"),
         dict(cls="c2", name="Aviation", label="Aviation", dy=-18,
              sub=f"{n(A_ABS[-1],2)} Gt in {YA[-1]}",
              years=YA, values=A_ABS, fmt=lambda v: f"{n(v,2)} Gt")],
        YG, (0, 45), [0, 10, 20, 30, 40], lambda v: n(v, 0),
        [1940, 1960, 1980, 2000, 2020],
        f"On the same scale aviation is a line almost resting on the axis: in 2019, its highest "
        f"year, one part in {int(round(one_in))} of the total. That is what makes the figure "
        f"&laquo;2.5%&raquo; sound like the end of the argument. The total covers fossil fuels, "
        f"cement and land use; the aviation series is reconstructed by multiplying that total by "
        f"the published share, and agrees within rounding with the absolute series of Bergero et "
        f"al. ({n(AVCO2[YE.index(2019)],2)} Gt against {n(av19,2)} in 2019).",
        f"Source: {S['world_co2_total_t']['source']}, and {S['aviation_share_pct']['source']}. "
        f"Downloaded {S['world_co2_total_t']['downloaded']}.",
        ylab="Gt CO₂")

    F2 = line_fig(
        "quota", "Aviation's share of world CO₂", f"{YA[0]} – {YA[-1]} · per cent of the total",
        [dict(cls="c2", name="Aviation share", label=f"{n(A_SHARE[-1],2)}%",
              sub=f"in {YA[-1]}", area=True,
              years=YA, values=A_SHARE, fmt=lambda v: f"{n(v,2)}%")],
        YA, (0, 3), [0, 1, 2, 3], lambda v: f"{n(v,0)}%",
        [1940, 1960, 1980, 2000, 2020],
        f"The same line as before, divided by the total instead of set beside it: from "
        f"{n(sh40,2)}% in {YA[0]} to {n(sh19,2)}% in 2019, almost {share_mult:.0f} times as much. "
        f"The step in 1970 and the plateau that runs to 2013 are not aviation standing still — "
        f"they are a denominator growing faster (Asian coal); the climb from 2013 and the collapse "
        f"of 2020 are aviation. Watch the denominator: against fossil fuels alone, the same 2019 "
        f"share reads {n(fossil_share19,1)}%.",
        f"Source: {S['aviation_share_pct']['source']}. {S['aviation_share_pct']['note']}",
        annot=[(2019, sh19, f"{n(sh19,2)}% in 2019", -52, -38)])

    F3 = line_fig(
        "efficienza", "Demand, efficiency and emissions since 1990",
        f"{YE[0]} = 100 · world aviation",
        [dict(cls="c1", name="Passenger-km", label="Passenger-km", sub=f"×{pkm_mult:.1f} since 1990",
              years=YE, values=idx(PKM), fmt=lambda v: f"{n(v,0)} (1990=100)"),
         dict(cls="c2", name="Aviation CO₂", label="CO₂", sub=f"×{co2_mult:.1f} since 1990",
              years=YE, values=idx(AVCO2), fmt=lambda v: f"{n(v,0)} (1990=100)"),
         dict(cls="c4", name="CO₂ per passenger-km", label="CO₂ per pass-km",
              sub=f"−{int_drop:.0f}% since 1990",
              years=YE, values=idx(INTENS), fmt=lambda v: f"{n(v,0)} (1990=100)")],
        YE, (0, 460), [0, 100, 200, 300, 400], lambda v: n(v, 0),
        [1990, 1995, 2000, 2005, 2010, 2015, 2020],
        f"The engine behind the previous chart. Carrying one passenger one kilometre costs less "
        f"than half the CO₂ it cost in 1990 ({n(INTENS[0],0)} grammes down to "
        f"{n(INTENS[YE.index(2019)],0)}): the technology did what was asked of it. But the "
        f"kilometres flown quadrupled, and the product of the two nearly doubles. It is the only "
        f"reading that explains how a share can rise while every individual aircraft improves.",
        f"Source: {S['aviation_pkm_bn']['source']}. 2020-21 is the pandemic, not a trend.",
        pad=(26, 156, 40, 58))

    F4 = bar_fig(
        "erf", "What aviation's warming is made of",
        "2018 · effective radiative forcing, mW/m²",
        [("Contrail cirrus", E["contrails"]), ("CO₂", E["co2"]),
         ("NOˣ (net)", E["nox"]), ("Water vapour", E["h2o"])],
        f"Tonnes of CO₂ are one thing; the warming that follows is another. In the 2018 balance "
        f"CO₂ accounts for {n(E['co2'],1)} of the {n(ENET,0)} mW/m² net: about a third. The rest "
        f"comes from effects that last hours or decades rather than centuries — contrail cirrus "
        f"above all, at {erf_contrail_share:.0f}% of the net. The four items above sum to more "
        f"than the net figure because aerosols (sulphates) carry the opposite sign and are not "
        f"shown. They are also the most uncertain part of the whole balance: the published "
        f"interval for the total runs from {elo} to {ehi} mW/m² (5-95%).",
        f"Source: {erf['source']}.",
        unit="mW/m²", maxv=62,
        colorcls=lambda row: "c2" if row[0] == "CO₂" else "c3",
        leg=legend([("CO₂", "c2"), ("Non-CO₂ effects", "c3")]))

    F5 = col_fig(
        "europa", "Europe since 2019: the flights come back, the emissions do not",
        "ECAC / EUROCONTROL area",
        [dict(title="IFR flights per year", sub="millions · ECAC area (2025: NM area, not identical)",
              unit="million flights", cls="c4", dec=1, max=12, ticks=[0, 4, 8, 12],
              rows=[(str(y), v) for y, v in flights]),
         dict(title="Gate-to-gate CO₂ of those flights", sub="million tonnes · EUROCONTROL area",
              unit="Mt CO₂", cls="c2", dec=0, max=240, ticks=[0, 80, 160, 240],
              rows=[(str(y), v) for y, v in ecco2])],
        f"In 2025 European skies saw the same number of flights as in 2019. The emissions did not "
        f"follow: EUROCONTROL's gate-to-gate series grows by {ec_growth:.0f}% between "
        f"{ecco2[0][0]} and {ecco2[-1][0]} while flights grow by {fl_growth:.0f}%. Dividing one "
        f"panel by the other would give a wrong CO₂ per flight: the perimeters do not match — the "
        f"CO₂ is what is emitted <b>inside</b> the EUROCONTROL area, including by flights merely "
        f"crossing it — and the emissions series starts in {ecco2[0][0]}. Two measures side by "
        f"side, not an index.",
        f"Source: {FIG['ecac_flights_m']['source']}; gate-to-gate CO₂ from "
        f"{FIG['eurocontrol_gate_to_gate_mt']['source']}.")

    F6 = bar_fig(
        "vs2019", "The same sky, six years on", "2025 against 2019 · per cent change",
        [("Distance flown", vs["distance"]), ("Passengers", vs["pax"]),
         ("Flight hours", vs["hours"]), ("CO₂ (full trajectory)", vs["co2"]),
         ("Average take-off weight", vs["mtow"]), ("Number of flights", vs["flights"])],
        f"The last row is the one that makes all the others surprising: the flights are the same "
        f"as in 2019 (+{n(vs['flights'],1)}%), but they fly {n(vs['distance'],1)}% more "
        f"kilometres, in aircraft {n(vs['mtow'],1)}% heavier, and emit {n(vs['co2'],1)}% more "
        f"CO₂. Traffic is no longer the variable that explains European emissions: sector length "
        f"and aircraft type are. It is also why counting flights no longer says how things are "
        f"going.",
        f"Source: {FIG['vs2019_pct']['source']}. {FIG['vs2019_pct']['note']}",
        unit="%", maxv=12)
    tiles = f"""<div class=ctiles>
<div class="ctile t1"><b>{n(sh19,2)}%</b><span>of world CO₂ was emitted by aviation in 2019</span>
<em>Lee et al. · Our World in Data</em></div>
<div class="ctile t2"><b>{n(Q['erf_share_anthropogenic_pct'],1)}%</b><span>of human-caused radiative
forcing, once contrails are counted</span><em>Lee et al. 2021</em></div>
<div class="ctile t3"><b>≈{n(Q['warming_share_pct'],0)}%</b><span>of the warming observed so far is
attributed to flying</span><em>Klöwer et al. 2021</em></div>
<div class="ctile t4"><b>{n(flights[-1][1],1)} m</b><span>flights in Europe in {flights[-1][0]}:
as many as in 2019</span><em>EUROCONTROL</em></div></div>"""

    desc = ("How much of the world's CO2 comes from flying, how that share has moved since 1940, "
            "and what changes once contrails and NOx are counted. The context around the co2gap "
            "figures, from published sources.")

    return f"""<!doctype html>
<html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Aviation's 2.5% — co2gap</title>
{meta("Aviation's 2.5% — co2gap", desc, "context.html")}
<style>{style}</style>
<style>{STYLE_CONTEXT}</style></head><body class=context>
{nav}
<div class=wrap>
<div class=hero style="padding-bottom:0">
<p class=eyebrow>Context</p>
<h1>Aviation's 2.5%</h1>
<p class=lede>Flying accounts for about 2.5% of the world's CO₂. It is a small figure, and it gets
used for two opposite purposes: to close the argument and to open it. This page puts it back inside
the series it comes from, and shows what changes when you change the denominator, the perimeter or
the metric.</p>
</div>

{tiles}

<section>
<h2>A thin line on a total that will not stop rising</h2>
<div class=col>
<p>The four figures above describe the same activity and cannot be compared with one another: the
first counts tonnes, the second watts per square metre, the third degrees, the fourth take-offs.
Almost every discussion about aviation and the climate runs aground exactly there, in the move from
one to the next. The simplest one comes first.</p>
<p>In {YG[-1]} the world emitted about <b>{n(tot24)} billion tonnes</b> of CO₂ counting fossil
fuels, cement and land use — four times the 1940 figure, and a record. Aviation, on the same scale,
is the line that looks as if it were resting on the axis.</p>
</div>
{F1}
<div class=col>
<p>So far, the reading that closes the argument. But the same line, divided by the total instead of
set beside it, tells the other half: aviation's share went from <b>{n(sh40,2)}% in {YA[0]} to
{n(sh19,2)}% in 2019</b>, while almost every other sector lost relative weight over the same
stretch.</p>
</div>
{F2}
<div class=col>
<p class=cpull>A small share does not mean a sector standing still. It means a small sector inside
an enormous total — one that grows faster than the total does.</p>
<p>The denominator is worth looking at closely, because it is the first source of confusion: here
the total includes land use. Against fossil fuels alone, the same 2019 share becomes
<b>{n(fossil_share19,1)}%</b>. And if you look at the perimeter of the numerator, the same question
has at least three answers: commercial passenger flights alone, as counted by the OECD, come to
<b>{n(oecd[2019],0)} Mt</b> in 2019, while the Lee and Bergero series, which also covers freight,
charter and general aviation, counts <b>{n(av19*1000,0)} Mt</b>. Neither is wrong: they count
different things. Those same three answers turn up, with the same air of contradiction, in any
public argument on the subject.</p>
</div>
</section>

<section>
<h2>The efficiency is real. Growth eats it</h2>
<div class=col>
<p>The obvious question, in front of the previous chart, is why the share rises precisely while
aircraft get more efficient. The answer is a product of two factors moving in opposite directions,
and one of them moves harder.</p>
</div>
{F3}
<div class=col>
<p>Between 1990 and 2019 the CO₂ per passenger-kilometre fell from {n(INTENS[0],0)} to
{n(INTENS[YE.index(2019)],0)} grammes: −{int_drop:.0f}%, a real technical achievement, delivered by
engines, aerodynamics and above all fuller aircraft. Over the same period passenger-kilometres went
from {n(PKM[0],0)} to {n(PKM[YE.index(2019)],0)} billion: ×{pkm_mult:.1f}. The product, which is
the emissions, nearly doubles: ×{co2_mult:.1f}.</p>
<p>This is why the two usual camps can both cite true figures and reach opposite conclusions.
&laquo;Every flight pollutes half as much as thirty years ago&raquo; and &laquo;flying pollutes
twice as much as thirty years ago&raquo; are both correct statements about two different
quantities. The second is the one the atmosphere sees.</p>
</div>
</section>

<section>
<h2>Warming is not measured in tonnes</h2>
<div class=col>
<p>Here the accounting changes in kind, and this is where aviation parts company with other
sectors. An aircraft does not emit carbon dioxide alone: it emits nitrogen oxides at altitude,
water vapour, and particles around which, in cold saturated air, trails form that can turn into
cirrus and last for hours. The standard way of adding up effects that different is effective
radiative forcing, in milliwatts per square metre.</p>
</div>
{F4}
<div class=col>
<p>In the 2018 balance CO₂ is worth {erf_co2_share:.0f}% of the total: <b>{n(E['co2'],1)} of the
{n(ENET,0)} mW/m²</b> net. Contrail cirrus alone is worth more than half. Aviation's total is about
<b>{n(Q['erf_share_anthropogenic_pct'],1)}% of anthropogenic forcing</b> — against the
{n(sh19,1)}% of the tonnes alone — and Klöwer and colleagues, applying the same accounting to
temperature, attribute to flying about <b>{n(Q['warming_share_pct'],0)}% of the observed
human-caused warming</b>, on the order of {n(Q['warming_c'],2)} °C. Aviation's cumulative emissions
since 1940 are {n(Q['cumulative_aviation_gt'],0)} Gt, roughly {n(Q['cumulative_share_pct'],0)}% of
the world's cumulative total.</p>
<p>Two warnings, which matter more than the figures. First: <b>{n(sh19,1)}% and
{n(Q['erf_share_anthropogenic_pct'],1)}% neither add up nor replace one another</b> — they are
percentages of two different quantities, and swapping them produces numbers that sound stronger and
mean nothing. Second: the non-CO₂ effects carry far wider uncertainty than CO₂ — the published
interval for the total runs from {elo} to {ehi} mW/m², at 5-95% — and they do not persist in the
same way. A tonne of CO₂ acts for centuries; a contrail is gone by morning. Comparing them requires
choosing a time horizon, and the choice changes the answer: that is why the European rules on
monitoring non-CO₂ effects prescribe publishing several horizons — 20, 50 and 100 years — rather
than a single number. At 20 years contrails dominate the comparison; at 100 CO₂ takes it back.
Neither horizon is &laquo;the right one&raquo;: it is a choice, and it has to be stated.</p>
</div>
</section>

<section>
<h2>The same flights as 2019, more emissions</h2>
<div class=col>
<p>The {term("ecac", "ECAC area")} — 44 states, the Europe of air traffic control rather than the Europe of the Union —
is the perimeter where the question can be asked flight by flight instead of through aggregate
estimates. In 2025 it passed its 2019 levels for the first time.</p>
</div>
{F5}
<div class=col>
<p>The return to pre-pandemic levels happened in the number of flights, not in the emissions.
Comparing 2025 with 2019, EUROCONTROL measures the same traffic and more CO₂: sectors have grown
longer, turboprops have been replaced by regional jets, long-haul has come back.</p>
</div>
{F6}
<div class=col>
<p>In Europe the same activity weighs more — about <b>{n(Q['eu_aviation_share_pct'],1)}% of total
emissions</b> — and not because Europe flies worse: it is the rest of the European economy that has
decarbonised while flying has not, so the same slice takes up more room in a smaller pie. The share
is set to keep rising for the same reason: the sector has fewer levers than the others, and its
main ones — sustainable fuels, fleet renewal — are slow. In EUROCONTROL's plan for 2050,
operational measures — routes, profiles, traffic management — account for
{n(Q['operational_share_2050_pct'],0)}% of the effort. Ten per cent of an enormous effort is still
a great deal.</p>
</div>
</section>

<section>
<h2>Measuring the distance from an ideal flight</h2>
<div class=col>
<p>If the technological levers are slow and demand keeps growing, what is left to look at in the
short run is how far each flight sits from a reference built on the same journey. On this, in
Europe, there are two estimates: they use different references and measure different things, and
the difference between them is not a hidden margin.</p>
<p>The first is EUROCONTROL's. In the Performance Review Report 2025 the Performance Review
Commission estimates the air-traffic-management &laquo;benefit pool&raquo; empirically at about
<b>{n(benefit_pool,0)}%</b>, comparing each flight with the tenth percentile of comparable flights
— that is, with what somebody else, on that same sector and in that same aircraft type, actually
managed to do. It is a measure of plausibly recoverable margin, because its reference is a flight
that existed.</p>
<p>The second is this site's, and it means something else: it compares each flight with an
<i>ideal flight</i> — same aircraft type, great-circle route, optimal altitude and speed profile,
the same real wind — and finds, across {n_flights:,} ECAC flights over {days} days,
<b>{total_gap:.1f}% more CO₂</b>, of which {lat_w:.1f} points of route and {vert_w:.1f} of profile.
The two figures are not in contradiction: the first measures what could be recovered, the second
how far a theoretical limit sits from every real flight, because a real flight has to respect
separation, route structure, closed airspace and arrival queues, among many other operating
conditions. That list explains why the limit is out of reach; it is not a breakdown of the
{total_gap:.1f}%, which the model does not attribute to any of those causes in particular.</p>
<p>In one line: <b>the {n(benefit_pool,0)}% measures what could realistically be recovered by
comparing real flights with one another; the {total_gap:.1f}% measures the distance from a physical
limit no flight can reach.</b> It follows that the difference between them is <i>not</i>
{total_gap - benefit_pool:.0f} points of inefficiency the institutions are ignoring: it is the part
of the gap that exists because the sky is full of other aircraft. Subtracting one figure from the
other produces no quantity that means anything.</p>
<p class=cpull>The value of the gap is not saying how much is wasted. It is saying where it
accumulates: in which phase of the flight, on which routes, with what regularity.</p>
<p>&laquo;Where&raquo; should be taken literally. A high figure on the flights that touch an
airport describes those flights — the shape of the airspace, the procedures, the queues waiting for
them — and not the conduct of the airport, which decides almost none of those queues. The
<a href="index.html#findings">findings</a> read that way throughout, and the
<a href="methodology.html">method</a> says where it is soft.</p>
</div>
</section>

<section>
<h2>What this page does not say</h2>
<div class=col>
<ul>
<li><b>It does not say flying is the main problem.</b> {n(sh19,1)}% is still {n(sh19,1)}%:
electricity, industry and heating remain orders of magnitude larger.</li>
<li><b>It does not say {n(Q['erf_share_anthropogenic_pct'],1)}% replaces {n(sh19,1)}%.</b> They are
percentages of different quantities, with different uncertainties, and each should be quoted with
its metric attached.</li>
<li><b>It does not estimate how much CO₂ could be avoided.</b> The gap this site measures is the
distance from a <i>theoretical physical limit</i>, not an operational recovery margin. For that,
the published reference is EUROCONTROL's {n(benefit_pool,0)}%.</li>
<li><b>It does not cover air freight separately</b>, nor compare flying with other modes for the
same journey: both are legitimate questions that need different data from these.</li>
<li><b>It is not a primary source.</b> Every figure here except this site's own comes from the
publications listed below; where sources diverge, the divergence is flagged rather than averaged
away. The full list, with the date each was verified, is published as
<a href="context-sources.json">context-sources.json</a>.</li>
</ul>
</div>
</section>

<section>
<h2>Sources</h2>
<div class=col>
<table class=csrc>
<thead><tr><th>Source</th><th>What it provides · perimeter</th></tr></thead>
<tbody>
<tr><td>{S['world_co2_total_t']['source']}</td><td>{S['world_co2_total_t']['what']}.
{S['world_co2_total_t']['note']}</td></tr>
<tr><td>{erf['source']}</td><td>{erf['what']}; the emissions series back to 1940. All aviation, not
scheduled flights alone.</td></tr>
<tr><td>{S['aviation_pkm_bn']['source']}</td><td>Passenger-km, carbon intensity and emissions
1990-2021.</td></tr>
<tr><td>Klöwer et al. 2021, Environmental Research Letters 16:104027</td><td>Share of observed
warming attributed to aviation; cumulative emissions.</td></tr>
<tr><td>{S['oecd_passenger_aviation_t']['source']}</td><td>{S['oecd_passenger_aviation_t']['what']}.
{S['oecd_passenger_aviation_t']['note']}</td></tr>
<tr><td>EUROCONTROL, Performance Review Report 2025 (March 2026)</td><td>Flights, distances, hours,
gate-to-gate CO₂ and emissions by flight phase in the EUROCONTROL area; the European
{n(Q['eu_aviation_share_pct'],1)}% share; the {n(benefit_pool,0)}% ATM benefit pool.</td></tr>
<tr><td>EUROCONTROL STATFOR — Standard Inputs, Data Snapshot #57, 7-Year Forecast</td><td>Annual
IFR flight counts. The ECAC area (44 states) and the Network Manager area are not the same, and the
publications alternate between them.</td></tr>
<tr><td>co2gap</td><td>Gap from the theoretical optimum, {n_flights:,} ECAC flights over {days}
days. Release {release}, methodology v{method_version}.</td></tr>
</tbody></table>
<p class=hint>Every figure on this page that is not this site's own was verified against its source
on {ext['verified']}, and is re-checked at each release. Unlike everything else here, these numbers
cannot be recomputed from the published data: they come from other people's publications, and the
machine cannot tell when they go out of date.</p>
</div>
</section>
</div>
<p class=foot><span class=wrap style="display:block">
{footnav}<br>
Trajectory data © <a href="https://adsb.lol">adsb.lol</a> contributors, licensed
under <a href="https://opendatacommons.org/licenses/odbl/">ODbL</a> — these pages
are a Produced Work: attribution, no share-alike.
Wind: ERA5, Copernicus. Airports: OurAirports (CC0). Performance model: OpenAP,
TU Delft.<br>
<b>Release {release}</b> · methodology v{method_version} · the external figures on
this page were verified on {ext['verified']} and are re-checked at each release.<br>
Contact <a href="mailto:hello@co2gap.org">hello@co2gap.org</a>
</span></p>
<script>window.__CTXCHARTS__={json.dumps(CHARTS)};</script>
<script>{JS}</script>
</body></html>"""
