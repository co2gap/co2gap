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
    return headline(by_airport(add_mean_norm(m, BINS, MIN_N_CELL),
                               MIN_N_AIRPORT))


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
    '<a href="https://github.com/Pengo-fmm/co2gap">source</a>'
)


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
<meta property=og:image:alt content="co2gap — 1,833,127 flights, 25.4 Mt CO2 emitted, \
4.57 Mt gap from the theoretical optimum">
<meta name=twitter:card content=summary_large_image>
<meta name=twitter:title content="{esc(title)}">
<meta name=twitter:description content="{esc(desc)}">
<meta name=twitter:image content="{SITE_URL}/og.png">
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
:root{--bg:#0e1216;--card:#161d23;--fg:#e8eef3;--mut:#8ea3b2;--line:#243039;
--pos:#ff8a6b;--neg:#5fd0a8;--hi:#5ac8fa;--warn:#f0b429}
@media(prefers-color-scheme:light){:root{--bg:#fbfcfd;--card:#fff;--fg:#16212b;
--mut:#5b6b78;--line:#e2e8ee;--pos:#c2410c;--neg:#0f766e}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:960px;margin:0 auto;padding:36px 20px 90px}
h1{font-size:1.8rem;margin:0 0 8px;letter-spacing:-.02em}
h2{font-size:1.12rem;margin:44px 0 6px;letter-spacing:-.01em}
h2+p.hint{margin:0 0 14px;color:var(--mut);font-size:.88rem}
.sub{color:var(--mut);margin:0 0 22px}
.stats{display:flex;flex-wrap:wrap;gap:12px;margin:22px 0}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:13px 16px;flex:1;min-width:150px}
.stat .v{font-size:1.45rem;font-weight:650;letter-spacing:-.02em}
.stat .l{color:var(--mut);font-size:.8rem;margin-top:2px}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;margin:4px 0;font-size:.9rem}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--mut);font-weight:500;font-size:.75rem;text-transform:uppercase;letter-spacing:.05em}
td.num{text-align:right;font-variant-numeric:tabular-nums}
td.big{font-weight:650}
td.pos{color:var(--pos)} td.neg{color:var(--neg)}
td.r{font-weight:500;white-space:normal}
.code{color:var(--mut);font-size:.75rem;margin-left:7px;font-family:ui-monospace,monospace}
.flag{color:var(--warn);margin-left:6px;cursor:help}
.note{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--hi);
border-radius:8px;padding:14px 18px;color:var(--mut);font-size:.89rem;margin:16px 0}
.note.warn{border-left-color:var(--warn)}
.note b{color:var(--fg)}
.note p{margin:10px 0}
.foot{color:var(--mut);font-size:.8rem;margin-top:44px;border-top:1px solid var(--line);padding-top:16px}
a{color:var(--hi)}
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
<html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Methodology — co2gap</title>
{meta("Methodology — co2gap", DESC_METHOD, "methodology.html")}
<style>{STYLE}</style></head><body><div class=wrap>

<p><a href="index.html">← back to the data</a></p>
<h1>Methodology</h1>
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
are much smaller than ours, and rightly so:</p>
<table><thead><tr><th>measure</th><th class=num>per flight</th></tr></thead><tbody>
<tr><td>EUROCONTROL — level-offs in climb and descent, recoverable through
CCO/CDO procedures</td><td class=num>~{BENCH['cco_cdo_kg']} kg</td></tr>
<tr><td>Pasutto et al. (EUROCONTROL, 2021) — cruise, against the best profile
actually flown</td><td class=num>{BENCH['pasutto_kg']}–{BENCH['pasutto_avg_kg']} kg</td></tr>
<tr><td><b>this site</b> — gap from the theoretical optimum, whole profile</td>
<td class=num><b>~{per_flight_vert:.0f} kg</b></td></tr>
</tbody></table>
<p>Over the same distance range Pasutto uses (200–1500 NM), their
{BENCH['pasutto_pct']}% median for cruise alone compares with our 13.1% for the
whole profile: a factor of <b>2.8</b>, explained by three stated differences —
their reference is the <i>best observed profile</i>, ours a physical optimum;
they cover cruise only, we also cover climb, descent and speed; they assume
nominal mass and no wind, we use estimated mass and real wind.</p>
<p><b>Practical consequence:</b> multiplying our total by a carbon price and
calling it "waste" would be wrong. We do not do it, and we ask that it not be
done.</p>
</div>

<h3>How much is compressible — measured, not assumed</h3>
<p>A flight going direct, departing at night into an empty sky, on a long
sector, is about as close to the ideal trajectory as an airliner actually gets.
Across {n_floor:,} such flights the vertical gap still stands at
<b>{vert_floor:.1f}%</b>, against a fleet median of <b>{vert_fleet:.1f}%</b>.</p>
<p>That yields an attribution derived from the data: <b>{vert_floor:.1f} points
are floor</b> — the baseline remaining out of reach because of cost index, step
climbs imposed by weight and discrete flight levels — and
<b>{vert_oper:.1f} points are operational margin</b>, related to traffic,
routing and profile.</p>
<p>The floor nearly coincides with the value a EUROCONTROL study obtains for
cruise by comparing each flight with the <i>best observed profile</i>, a
reference that already contains those constraints. Two independent routes, the
same destination: which is why the apparent gap against external references does
not indicate a model error, but the difference between a fleet median and a
best-in-class reference.</p>

<h3>A number that can be given</h3>
<p>There is a way to quantify the margin without leaning on an unreachable
optimum: compare each flight not with perfection but with <b>what flights of the
same length already achieve</b>. That level is reachable by definition, because
half of comparable flights reach it.</p>
<ul>
<li>If flights above the median of comparable ones flew like that median:
<b>{sc_a:.1f} Mt of CO&#8322; a year</b> ({sc_a_fuel_kt:,.0f} kt of fuel).</li>
<li>Bringing only the worst quartile to the 75th percentile, a far more cautious
assumption: <b>{sc_b:.1f} Mt a year</b>.</li>
</ul>
<p>EUROCONTROL independently estimates <b>1.1 Mt of CO&#8322; a year</b> as
recoverable in the ECAC area through continuous climb and descent procedures
alone: the cautious scenario above comes close to it, while being built by an
entirely different method.</p>
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
<p>This correction is necessary, not cosmetic: the raw gap correlates about
<b>−0.74</b> with sector length, so a raw ranking would order routes by
shortness rather than by inefficiency. After normalisation the residual
correlation with distance is about <b>+0.08</b>.</p>
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
that spread collapses, and it stays <b>stable across seasons</b> — which is the
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
<p>KEA is not merely a published statistic: it is the <b>only environmental
indicator on which the Single European Sky performance scheme sets binding
targets</b> for Member States. For the current reference period, RP4, the
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

<h2>8. Stated limitations</h2>
<ul>
<li>We measure the gap from a <b>theoretical</b> optimum, not avoidable
inefficiency (§2).</li>
<li><b>Only the tails of the rankings are reliable.</b> Half the routes sit
within a few points of the norm, inside the uncertainty of the method: between
900th and 1000th place the ordering means nothing. Rankings show only routes
with at least {RANK_MIN_N} flights.</li>
<li>The period is <b>2026 only, January to July</b>: no year-on-year comparison,
and December is not covered.</li>
<li><b>Eight days are missing</b>: four absent at the source, four because of
weather data latency.</li>
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
<li><b>The split between lateral and vertical is a convention, but the result
does not depend on it.</b> We correct the route first and the profile second,
charging the extra kilometres at the <i>optimal</i> profile. The opposite
convention charges them at the flight's <i>actual</i> efficiency, which moves
weight towards the lateral component: across the fleet the split goes from
7.2 / 14.0 to 9.4 / 11.6 points. <b>The vertical component still dominates</b>,
including for the airport named in the findings (7.9 / 33.0 becomes
12.2 / 28.3 on raw medians). The total is identical under both by construction.
Anything resting on the <i>size</i> of the split should be read with this
sensitivity in mind; the ordering does not change.</li>
<li>The ideal trajectory's flight time uses the <b>harmonic mean of ground
speed</b> along the path, which is the quantity that reproduces the correct
flight time when wind varies. An earlier version used the arithmetic mean of
wind, which understated the baseline's fuel and inflated the published gap by
about 0.35%; the figures on this site are computed after that correction.</li>
</ul>

<h2>9. Privacy</h2>
<p>Every published row aggregates <b>at least {MIN_N} flights</b>. We do not
publish, and will not publish, data about an individual flight, aircraft or
operator. The rankings concern routes and airports, never people or
identifiable aircraft.</p>

<h2>10. Who made this, and how to report an error</h2>
<div class=note>
<p><b>Everything here is reproducible.</b> The trajectory data is public, the
performance model is open source, and the code that turns one into the other is
published: any figure on this site can be recomputed and any choice made along
the way can be inspected. What you find here is a <i>tool</i> with its
limitations stated, not an authored study.</p>
<p>I am not an aviation professional or a climate scientist; I run an ADS-B
receiver and I care about this. <b>The code was developed with AI tooling</b>
(Claude, by Anthropic) — the analytical decisions it implements are documented
on this page precisely so they can be checked rather than taken on trust.</p>
<p><b>Right of reply.</b> If a figure looks wrong to you, or if you represent
an airport, an airline or an air navigation service provider named here, write
to <a href="mailto:hello@co2gap.org">hello@co2gap.org</a>. Corrections are
published on this site, and a reply you send will be published alongside the
figure it concerns. It is why this work is public rather than private.</p>
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
<li><b>No advance access, embargo or preview for anyone.</b> Organisations named
here were written to before the first publication: all of them, on the same
terms, with no ability to alter what was published. Notice is given because
being named deserves warning, never as a commercial courtesy.</li>
<li><b>Right of reply is free and unconditional</b>, published in full and on
identical terms whether or not there is any other relationship.</li>
<li><b>Any commercial relationship with a named organisation is disclosed next
to that organisation's figure</b>, for as long as the figure is published.</li>
<li><b>No grades, tiers or composite scores are sold or published.</b> What this
project produces is measured quantities with their uncertainty; a score would
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
<li><b>The text of these pages</b> is
<a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>; the
<a href="https://github.com/Pengo-fmm/co2gap">pipeline source code</a> is
Apache-2.0. The project name and domain are not covered by either.</li>
</ul>
<p>Wind data: ERA5, Copernicus Climate Change Service. Fuel references: ICAO CEC
Methodology v13.1. Performance model: OpenAP, TU Delft (LGPL-3.0). Each release
is archived with a DOI so that a figure can be cited against the version that
produced it.</p>

<p class=foot>
Trajectory data © <a href="https://adsb.lol">adsb.lol</a> contributors, licensed
under <a href="https://opendatacommons.org/licenses/odbl/">ODbL</a> — the derived
data published here is distributed under the same terms, as the licence requires.
Wind: ERA5, Copernicus Climate Change Service.
Fuel references: ICAO CEC Methodology v13.1.
Performance model: OpenAP, TU Delft.<br>
<b>Release {RELEASE}</b> · methodology v{METHOD_VERSION} · updated twice a year over a 12-month window ·
next update {NEXT_RELEASE}, covering {NEXT_WINDOW}.<br>
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
            f"<b>{pa['arr_desc']:.0f}%</b> in the descent. What happens at the "
            "far end of the flight, and in the cruise between the two, accounts "
            "for very little of what separates one airport from another.<br><br>"
            "<b>That is a location, not a cause.</b> It says the gap was burnt "
            "near this airport, in the climb out of it or the descent into it. "
            "It does not say whether the profile was chosen by the operator or "
            "imposed by the traffic, and nothing here distinguishes the two.")

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
                f"<td class=num>{r.lat:.0f}%</td><td class=num>{r.vert:.0f}%</td>"
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
        f"<td class=num>{r.med:+.0f}%</td></tr>" for i, r in band.iterrows())
    n_closed = int((g.closed != "").sum())

    html_doc = f"""<!doctype html>
<html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>co2gap — CO&#8322; and flight inefficiency observatory for Europe</title>
{meta("co2gap — CO2 and flight inefficiency observatory for Europe", DESC_INDEX)}
<style>{STYLE_INDEX}</style></head><body><div class=wrap>

<h1>co2gap — CO&#8322; and flight inefficiency observatory for Europe</h1>
<p class=sub>Actual emissions and the <b>gap against an ideal flight</b>, computed
from the ADS-B trajectory of every flight, with a great-circle baseline
<b>corrected for wind</b> and split into a <b>lateral</b> component (the route)
and a <b>vertical</b> one (the profile).
ECAC area · {esc(days[0])} → {esc(days[-1])} · {len(days)} days.<br>
<b>Release {RELEASE}</b> · methodology v{METHOD_VERSION} · updated twice a year over a 12-month window ·
next update {NEXT_RELEASE}, covering {NEXT_WINDOW} ·
generated {esc(gen)}.</p>

<div class=stats>
  <div class=stat><div class=v>{len(df):,}</div><div class=l>flights analysed</div></div>
  <div class=stat><div class=v>{co2_t/1e6:,.1f} Mt</div><div class=l>CO&#8322; emitted</div></div>
  <div class=stat><div class=v>{excess_t/1e6:,.2f} Mt</div><div class=l>gap from the theoretical optimum</div></div>
  <div class=stat><div class=v>{len(g_all):,}</div><div class=l>routes with n≥{MIN_N}</div></div>
</div>

<div class=note>
<b>What this site measures.</b> For every flight we compare the CO&#8322; actually
emitted with that of an ideal flight: same aircraft type, direct great-circle
route, the most efficient altitude and speed for that distance, and <b>the same
real wind</b>. The difference splits into two additive parts: the
<b>lateral</b> one (having flown more kilometres) and the <b>vertical</b> one
(having flown the same route on a less efficient altitude and speed profile).
Over the period observed: total <b>{(lat_w+vert_w):.1f}%</b>, of which
lateral <b>{lat_w:.1f}%</b> and vertical <b>{vert_w:.1f}%</b>.
</div>

<div class="note warn">
<b>This is not fuel that could be saved.</b> The ideal great-circle flight at a
perfect profile is a theoretical limit no real flight can reach: separation
between aircraft, route structure, constrained airspace and arrival queues put
it out of reach. Published estimates of <i>recoverable</i> inefficiency are much
smaller — EUROCONTROL puts at roughly 39 kg per flight what continuous climb and
descent procedures would recover, against the roughly 520 kg of total gap
measured here. <b>This site measures the distance from a theoretical optimum,
not avoidable waste.</b> The full comparison is in the methodology.
</div>

<h2>What the data shows</h2>
<p class=hint>Four things visible in the data, with what is known about why —
and what remains unknown.</p>

<div class=note>
<p><b>1. A group of congested hubs sits well above the norm, and their gap is
in the profile rather than the route.</b><br>
Flights to and from <b>{ap1_name}</b> ({ap1_d:+.1f} points across {ap1_n:,}
movements) and <b>{esc(aname(ap2))}</b> ({ap2r.d:+.1f} across {int(ap2r.n):,})
deviate from comparable flights more than those of any other airport with
substantial traffic, followed by {esc(aname(ap3))} at {ap3r.d:+.1f}, against a
median across all {len(ga)} airports of {ap_med:+.1f}. A point is one percentage
point of CO&#8322; relative to the ideal flight.<br>
<b>These are not places in a league table.</b> At the head of the ranking ten
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
In both cases the deviation is not in the route — {ap1_name} flies routes of
normal length ({ap1_l:+.1f} lateral) — but in how the flights climb, cruise and
descend ({ap1_v:+.1f} vertical). The vertical component still dominates under
the alternative decomposition convention described in the methodology, so it is
not an artefact of that choice.<br>
{ap1_routes} of the twenty routes furthest from the norm have {ap1_name} at one
end, against a base rate of {ap1_share:.0f}% of all ranked routes — a real
concentration, though a hub with many routes is over-represented in any tail.
Profiles of this shape are what dense terminal areas produce: early descents,
level segments, sequencing. ADS-B shows the profiles flown, not the noise
abatement rules, sequencing constraints or capacity limits that require them.
<b>This describes what these flights fly. It does not measure what the airports,
their airlines or their controllers could do differently.</b></p>
</div>

<div class=note>
<p><b>2. Closed airspace has a cost, and it is large where it bites.</b><br>
The clearest example is <b>{kal_a} ↔ {kal_b}</b>, flying <b>+{kal_pct:.0f}%</b>
further en route because the straight line between the two airports crosses
Kaliningrad. It runs {kal_n:,} flights over the period — below the {RANK_MIN_N}
needed to enter the rankings, and quoted here as an illustration of the
mechanism rather than as a placing. The detour is geometric: it does not depend
on sample size. Baltic connections
towards Turkey route around Belarus and Ukraine for the same reason. In total
{n_closed} ranked routes have a direct path through closed airspace. None of
this is recoverable while those closures hold — and the overflight ban binds
European carriers but not third-country ones, so each figure is an average
across operators that must divert and operators that need not.</p>
</div>

<div class=note>
<p><b>3. The efficient end of the ranking is small and peripheral.</b><br>
{apb_name} sits at <b>{apb_d:+.1f} points</b>, followed by other Nordic and
island airports, more than thirty points away from the congested hubs. Light
traffic buys continuous descents and direct clearances. It is a measure of how
much congestion costs, not a target a hub could adopt.</p>
</div>

<div class=note>
<p><b>4. Most of this gap cannot be compressed — the part usually left out.</b><br>
Of the {vert_fleet:.1f} points of vertical gap, <b>{vert_floor:.1f} remain for a
flight going direct through an empty night sky</b>: that is the baseline staying
out of reach, not inefficiency. Only {vert_oper:.1f} points move with traffic,
routing and profile. The distinction is the difference between "European
aviation wastes X" and "between comparable flights there is a spread of this
size" — and only the second is something these data support.</p>
</div>

<h2>How much of this gap is compressible</h2>
<p class=hint>Derived from the data itself, not assumed.</p>
<div class=note>
<p>A flight that goes <b>direct</b>, departing <b>at night</b> into a nearly
empty sky, on a <b>long</b> sector where cruise dominates, is about as close to
our ideal trajectory as an airliner gets in practice. Across {n_floor:,} such
flights the vertical gap still stands at <b>{vert_floor:.1f}%</b>.</p>
<p>That is the <b>floor</b>: not inefficiency, but the baseline remaining out of
reach. It comes from choices and constraints no procedure removes — the cruise
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
<p>The floor measured here is close to the value a EUROCONTROL study obtains for
cruise by comparing each flight with the <i>best profile actually observed</i> —
a reference that already embeds those constraints. Two independent routes to the
same point.</p>
</div>

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
alone. Two independent methods, the same order of magnitude.</p>
<p><b>This is counterfactual arithmetic, not a forecast.</b> It assumes the
median level is reachable everywhere, and it is not: some routes sit above the
median because of structural constraints — closed airspace, terrain, congestion
— that no procedure removes. It measures what the <i>observed spread</i> between
comparable flights is worth, not what is achievable.</p>
</div>

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

<h2>European norm by distance band</h2>
<p class=hint>The raw gap grows as distance shrinks, so a raw ranking would sort
by shortness rather than by inefficiency. Every ranking below uses the deviation
from the median of <b>flights of the same length and the same aircraft
type</b>.</p>
<div class=scroll><table><thead><tr><th>Band</th><th class=num>flights</th>
<th class=num>median gap</th></tr></thead><tbody>
{bandrows}
</tbody></table></div>

<h2>Routes furthest from the norm</h2>
<p class=hint>Δ norm in percentage points against flights of the same length and
type. Rankings use only routes with at least <b>{RANK_MIN_N}</b> flights: below
that the sample is too small for an ordering to mean anything.
⚑ = the direct path crosses closed or avoided airspace ({n_closed} routes
flagged).</p>
<div class=scroll><table><thead><tr><th>Route</th><th class=num>flights</th>
<th class=num>km</th><th class=num>Δ norm</th><th class=num>lat.</th>
<th class=num>vert.</th><th class=num>t CO&#8322;</th></tr></thead><tbody>
{worst}
</tbody></table></div>

<h2>Routes closest to the optimum</h2>
<div class=scroll><table><thead><tr><th>Route</th><th class=num>flights</th>
<th class=num>km</th><th class=num>Δ norm</th><th class=num>lat.</th>
<th class=num>vert.</th><th class=num>t CO&#8322;</th></tr></thead><tbody>
{best}
</tbody></table></div>

<h2>Airports</h2>
<p class=hint>Arrivals and departures combined, at least {MIN_N_AIRPORT} flights.
The <b>vert.</b> column isolates the profile component — where early descents
and terminal-area holding show up.</p>

<div class=note>
<b>Read this before reading the table.</b> Each row describes <b>the flights that
touch this airport</b>. It is not a measure of the airport's own conduct. A flight
is counted at both of its ends, and its gap is measured over the <b>whole flight</b>
— so a share of what appears under one airport was produced at the other, and a
share of it in cruise, far from either.<br><br>
That is why <b>on dep.</b> and <b>on arr.</b> are shown separately: the same figure,
split by the role the airport played. If it were inherited from the airports at the
far end, one of the two sides would sit near the norm. Both readings occur here.
<b>{esc(aname(sym_ap))}</b> stands at {symr.dep:+.1f} on departure and
{symr.arr:+.1f} on arrival: the deviation travels with the airport, not with its
partners. <b>{esc(aname(asym_ap))}</b> stands at {asymr.dep:+.1f} and
{asymr.arr:+.1f}: nearly all of it appears on one side, and its combined figure of
{asymr.d:+.1f} alone would not have told you which. The median across all
{len(ga)} airports is {ap_med:+.1f}.<br><br>
{phase_note}
</div>

<div class=scroll><table><thead><tr><th>Airport</th><th class=num>movements</th>
<th class=num>Δ norm</th><th class=num>on dep.</th><th class=num>on arr.</th>
<th class=num>Δ lat.</th><th class=num>Δ vert.</th>
</tr></thead><tbody>
{ap_worst}
</tbody></table></div>
<p class=hint style="margin-top:18px">Airports closest to the norm:</p>
<div class=scroll><table><thead><tr><th>Airport</th><th class=num>movements</th>
<th class=num>Δ norm</th><th class=num>on dep.</th><th class=num>on arr.</th>
<th class=num>Δ lat.</th><th class=num>Δ vert.</th>
</tr></thead><tbody>
{ap_best}
</tbody></table></div>

<h2>Routes by total CO&#8322;</h2>
<p class=hint>The routes that weigh most in absolute terms, regardless of
efficiency.</p>
<div class=scroll><table><thead><tr><th>Route</th><th class=num>flights</th>
<th class=num>km</th><th class=num>Δ norm</th><th class=num>lat.</th>
<th class=num>vert.</th><th class=num>t CO&#8322;</th></tr></thead><tbody>
{by_co2}
</tbody></table></div>

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
(1) We measure the gap from a <b>theoretical</b> optimum, not avoidable
inefficiency.
(2) The period is 2026 only, January to July: no year-on-year comparison.
(3) Eight days are missing — four absent at the source, four because of weather
data latency.
(4) <b>Only the tails of the rankings are reliable</b>: half the routes sit
within a few points of the norm, inside the uncertainty of the method, and their
ordering is not meaningful.
(5) Routes flagged ⚑ <i>cannot</i> fly the direct path: the airspace is closed.
The ban applies to European carriers and not to third-country ones, so the
figure shown is an average between those who must divert and those who need not.
(6) No data about an individual flight or aircraft is published: every row
aggregates at least {MIN_N} flights.
(7) ADS-B coverage does not include oceanic sectors.<br><br>
<b><a href="methodology.html">Full methodology, validations and external
comparisons →</a></b>
</div>

<div class=note>
<b>About this project.</b> Every figure here can be recomputed from scratch: the
data is public, the method is documented in full and the code is open. This is
an independent open-data project, not the output of an institution — which is
why the limitations are stated as prominently as the results.<br><br>
I am not an aviation professional or a climate scientist; I run an ADS-B
receiver and I care about this. The code was developed with AI tooling
(Claude), and the analytical choices behind it are written down so that people
who know the field can check them.<br><br>
<b>Found a mistake, or named here and want to reply?</b>
<a href="mailto:hello@co2gap.org">hello@co2gap.org</a> — corrections and replies
are published on this site, in full and unconditionally.<br><br>
<b>Operate an airport, an ANSP or an airline?</b> The detail behind these figures
for your own traffic — by hour, by origin, by aircraft type, month by month —
exists in the pipeline and is not published here. Same address. The rules that
keep that separate from what appears on this page are written down under
<a href="methodology.html#independence">independence</a>.
</div>

<p class=foot>
Trajectory data © <a href="https://adsb.lol">adsb.lol</a> contributors, licensed
under <a href="https://opendatacommons.org/licenses/odbl/">ODbL</a> — the derived
data published here is distributed under the same terms.
Wind: ERA5, Copernicus Climate Change Service.
Fuel references: ICAO CEC Methodology v13.1.
Performance model: OpenAP, TU Delft.
Text <a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>; reusing
these figures as a <i>dataset</i> triggers ODbL share-alike —
<a href="methodology.html#licence">what that means</a>.<br>
{len(df):,} flights · {len(days)} days · {len(months)} months.<br>
Contact <a href="mailto:hello@co2gap.org">hello@co2gap.org</a> ·
{SOCIAL}
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
