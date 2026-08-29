#!/usr/bin/env python3
"""
Everything an airport needs to check its own published figure.

Built for Aena, who asked for the methodology, the detailed datasets and the
calculations behind Madrid-Barajas before commenting. It is written for any
ICAO code, because the same request from any other named organisation has to be
answerable on the same terms.

The point that decides what goes in the bundle: an extract of one airport's
flights CANNOT reproduce the published figure on its own. The figure is not the
airport's own excess, it is the deviation from a EUROPEAN norm computed over
every flight of comparable length and aircraft type. Ship the flights without
the norm and the recipient can only take the headline on trust — which is the
opposite of the point. So the norm table travels with the flights, and the
per-flight rows carry the reference that was applied to them and where it came
from.

    ADSB_DECOMP_DIR=... ADSB_CALIB=... lab-venv/bin/python lab/export_airport.py LEMD

No aircraft identity is present, and none can be: the decomposition holds
aircraft TYPE, times, airports and distances, never a registration, a hex code
or a callsign. The extract is anonymous by construction, not by redaction.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lab"))

from site_build import (load, MIN_N_CELL, MIN_N_AIRPORT,   # noqa: E402
                        airport_names)

OUT_ROOT = Path(os.environ.get("ADSB_EXPORT_DIR") or (ROOT / "exports"))


def _european_median(df) -> float:
    """Median deviation across airports, pooled over departures and arrivals."""
    both = pd.concat([df.assign(ap=df.origin_icao), df.assign(ap=df.dest_icao)])
    g = both.groupby("ap").agg(n=("d_tot", "size"), d=("d_tot", "median"))
    return float(g[g.n >= MIN_N_AIRPORT].d.median())


def _readme(icao, name, s, n) -> str:
    """Written here rather than by hand so it cannot drift from the data."""
    return f"""{icao} — {name}
Data behind the published figure. Generated {dt.datetime.now(dt.timezone.utc):%Y-%m-%d}.
Period {s['period']}. {n:,} movements, departures and arrivals combined.

WHAT THE PUBLISHED FIGURE IS
  {icao} is reported at {s['deviation_from_norm_points']:+.1f} percentage points.
  That is NOT this airport's own excess. It is the deviation from a European
  norm: for each flight, the median excess of ALL European flights in the same
  distance band and of the same aircraft type. The type is normalised out
  because the question is routing and profile efficiency, not fleet choice.
  Cells with fewer than {MIN_N_CELL} flights fall back to the distance band alone.

  This is why the norm tables are included. An extract of one airport cannot
  reproduce the figure on its own, and taking it on trust is the opposite of
  the intention.

  To reproduce: d_tot = excess_total_pct - norm_total_pct, then take the median
  of d_tot over all rows. It should give {s['deviation_from_norm_points']:+.2f}.
  European median across airports with at least {MIN_N_AIRPORT} movements:
  {s['european_median_all_airports_points']:+.2f}.

FILES
  {icao}_flights.parquet / .csv.gz   one row per movement
  norm_by_band_and_type.csv          the norm actually applied, where the cell was large enough
  norm_by_band.csv                   the fallback norm, by distance band alone
  {icao}_summary.csv                 the figures above, machine readable

COLUMNS
  day, dep_utc            date and departure time, UTC
  flight_id               row identifier within its day, for cross-reference only
  typecode                ICAO aircraft type
  direction               departure from, or arrival at, this airport
  other_airport           the airport at the other end
  bin                     distance band used for the norm, km
  gc_km                   great-circle distance between the two airports
  flown_km                length of the reconstructed ground track
  dist_ratio              flown / great circle, whole flight
  dist_ratio_enroute      the same excluding 40 NM around each airport, as EUROCONTROL's KEA does
  cruise_alt_ft           fuel-optimal cruise level used by the reference flight
  mean_wpar_*_ms          mean along-track wind, ERA5, for the reference and along the real track
  co2_real_kg             CO2 of the flight as reconstructed
  co2_ideal_kg            CO2 of the reference: same type, direct route, optimal profile, same real wind
  co2_hybrid_kg           intermediate: real ground track, optimal profile. Separates route from profile
  excess_total_pct        (real - ideal) / ideal x 100
  excess_lateral_pct      the part attributable to flying further
  excess_vertical_pct     the part attributable to altitude and speed profile
  norm_source             whether the norm came from band x type, or band alone
  norm_total_pct          the norm applied to this flight
  d_tot, d_lat, d_vert    deviation from the norm: the quantities that are ranked

WHAT IS NOT HERE
  No aircraft identity, and none can be: the dataset holds aircraft TYPE, times,
  airports and distances. No registration, no ICAO 24-bit address, no callsign.
  It is anonymous by construction rather than by redaction.

WHAT THIS MEASURES, AND WHAT IT DOES NOT
  The gap from a theoretical optimum, not recoverable fuel. No real flight can
  fly the reference profile: separation, route structure, constrained airspace
  and arrival sequencing exist for good reasons. Multiplying these figures by a
  carbon price would be wrong.
  Most of the gap is the vertical component. A later analysis found that this
  part is almost entirely NOT explained by the cruise level or the cruise Mach:
  it sits in climb and descent, where the reference profile is at its crudest.
  How much of it is real inefficiency and how much is modelling is open, and is
  stated in the methodology rather than left to be discovered.

LICENCE
  Trajectories are (c) adsb.lol contributors under the Open Database Licence.
  Anything derived from them, including this extract, carries that licence: you
  may keep, analyse and redistribute it, with attribution and share-alike. No
  further condition is attached by us.
  Wind: ERA5, Copernicus Climate Change Service. Fuel references: ICAO CEC
  Methodology v13.1. Performance model: OpenAP, TU Delft.

  Errors and disagreements: hello@co2gap.org. A reply is published next to the
  figure it concerns, in full and unedited.
"""


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: export_airport.py <ICAO>", file=sys.stderr)
        return 1
    icao = sys.argv[1].upper()

    df = load()                       # same loader the site uses: identical numbers
    names = airport_names()
    name = names.get(icao, icao)

    # --- the norm, exactly as the site computes it -------------------------
    cell = df["bin"] + "|" + df.typecode
    counts = cell.value_counts()
    enough = cell.map(counts) >= MIN_N_CELL
    cell_norm = (df[enough].groupby([df["bin"][enough], df.typecode[enough]])
                 .agg(n=("excess_total_pct", "size"),
                      norm_total_pct=("excess_total_pct", "median"),
                      norm_lateral_pct=("excess_lateral_pct", "median"),
                      norm_vertical_pct=("excess_vertical_pct", "median"))
                 .reset_index())
    bin_norm = (df.groupby("bin")
                .agg(n=("excess_total_pct", "size"),
                     norm_total_pct=("excess_total_pct", "median"),
                     norm_lateral_pct=("excess_lateral_pct", "median"),
                     norm_vertical_pct=("excess_vertical_pct", "median"))
                .reset_index())

    # --- this airport's flights -------------------------------------------
    m = (df.origin_icao == icao) | (df.dest_icao == icao)
    a = df[m].copy()
    if a.empty:
        print(f"no flights for {icao}", file=sys.stderr)
        return 1
    a["direction"] = np.where(a.origin_icao == icao, "departure", "arrival")
    a["other_airport"] = np.where(a.origin_icao == icao, a.dest_icao, a.origin_icao)
    a["norm_source"] = np.where(enough[m], "distance_band_x_type", "distance_band")
    a["norm_total_pct"] = a.excess_total_pct - a.d_tot
    a["dep_utc"] = pd.to_datetime(a.dep_ts, unit="s", utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    cols = ["day", "dep_utc", "flight_id", "typecode", "direction",
            "origin_icao", "dest_icao", "other_airport", "bin",
            "gc_km", "flown_km", "dist_ratio", "dist_ratio_enroute",
            "cruise_alt_ft", "mean_wpar_gc_ms", "mean_wpar_track_ms",
            "co2_real_kg", "co2_ideal_kg", "co2_hybrid_kg",
            "excess_total_pct", "excess_lateral_pct", "excess_vertical_pct",
            "norm_source", "norm_total_pct", "d_tot", "d_lat", "d_vert"]
    out = a[cols].sort_values(["day", "dep_utc"]).reset_index(drop=True)

    # Rounded to the precision the quantities actually carry. The raw frame
    # writes CO2 as 29313.90234375 and an excess as 21.343610193320483, which
    # claims an accuracy the model does not have — OpenAP is good to a few
    # percent — and states it fifteen digits deep. Rounding is the honest form
    # and it also cuts the CSV by more than half, which is what makes the file
    # small enough to send as an ordinary attachment.
    for c, nd in (("gc_km", 2), ("flown_km", 2), ("dist_ratio", 5),
                  ("dist_ratio_enroute", 5), ("mean_wpar_gc_ms", 2),
                  ("mean_wpar_track_ms", 2), ("excess_total_pct", 3),
                  ("excess_lateral_pct", 3), ("excess_vertical_pct", 3),
                  ("norm_total_pct", 3), ("d_tot", 3), ("d_lat", 3), ("d_vert", 3)):
        out[c] = out[c].round(nd)
    for c in ("cruise_alt_ft", "co2_real_kg", "co2_ideal_kg", "co2_hybrid_kg"):
        out[c] = out[c].round(0).astype("int64")

    d = OUT_ROOT / icao
    d.mkdir(parents=True, exist_ok=True)
    out.to_parquet(d / f"{icao}_flights.parquet", index=False)
    out.to_csv(d / f"{icao}_flights.csv.gz", index=False, compression="gzip")
    cell_norm.to_csv(d / "norm_by_band_and_type.csv", index=False)
    bin_norm.to_csv(d / "norm_by_band.csv", index=False)

    # La metodologia viaggia col pacchetto, e va RICOPIATA ogni volta.
    # ⚠️ 2026-08-29: il bundle di Aena conteneva ancora la metodologia del
    # 16/08 mentre i dati erano ricalcolati. Dati corretti accanto al metodo
    # che li descriveva prima della correzione del rullaggio: e' la stessa
    # famiglia di difetto che ha prodotto il 242% in pagina, e sarebbe finita
    # dentro una lettera di scuse. Copiarla a mano una volta non basta: qui
    # non puo' piu' restare indietro.
    meth = Path(os.environ.get("ADSB_SITE_OUT") or (ROOT / "site/index.html")).parent / "methodology.html"
    if not meth.exists():
        raise SystemExit(
            f"metodologia non trovata in {meth}: il pacchetto non puo' partire "
            f"senza, e una copia vecchia e' peggio di nessuna copia. "
            f"Rigenera il sito, poi rilancia.")
    shutil.copyfile(meth, d / "methodology.html")

    # --- the published figure, recomputed here so it can be checked --------
    hp = float(out.d_tot.median())
    dep = out[out.direction == "departure"]
    arr = out[out.direction == "arrival"]
    summary = {
        "airport": icao, "name": name,
        "movements": len(out),
        "period": f"{df.day.min()} to {df.day.max()}",
        "deviation_from_norm_points": round(hp, 2),
        "deviation_departures": round(float(dep.d_tot.median()), 2),
        "deviation_arrivals": round(float(arr.d_tot.median()), 2),
        "lateral_component": round(float(out.d_lat.median()), 2),
        "vertical_component": round(float(out.d_vert.median()), 2),
        # Computed exactly as the site does it, or the bundle would contradict
        # the page it is meant to substantiate: departures and arrivals pooled
        # into one row per airport, airports under MIN_N_AIRPORT movements
        # dropped, then the median across airports.
        "european_median_all_airports_points": round(_european_median(df), 2),
        "co2_real_t": round(float(out.co2_real_kg.sum()) / 1000, 1),
        "co2_ideal_t": round(float(out.co2_ideal_kg.sum()) / 1000, 1),
    }
    pd.DataFrame([summary]).to_csv(d / f"{icao}_summary.csv", index=False)
    (d / "README.txt").write_text(_readme(icao, name, summary, len(out)))

    print(f"{icao} — {name}")
    for k, v in summary.items():
        print(f"  {k:42s} {v}")
    print(f"\nwritten to {d}")
    print("  " + "\n  ".join(sorted(p.name for p in d.iterdir())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
