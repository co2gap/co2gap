#!/usr/bin/env python3
"""
Phase-0 orchestrator: one adsb.lol day -> Italy/Alps box -> complete flights
-> OpenAP fuel/CO2 -> excess vs great-circle. Writes a JSON of per-flight
results and prints run stats (wall time, peak RAM, data scanned).

Run inside the project venv:
    nice -n15 ionice -c3 venv/bin/python pipeline/run_fase0.py
"""

from __future__ import annotations

import json
import os
import resource
import sys
import time
import warnings
from pathlib import Path

# OpenAP's smooth-limit uses exp() that overflows on extreme (ground) inputs;
# the resulting NaN is guarded in estimate_fuel. Silence the noise.
warnings.filterwarnings("ignore", category=RuntimeWarning)

ROOT = Path("/mnt/wd_elements/adsb-co2")
sys.path.insert(0, str(ROOT / "ingest"))
sys.path.insert(0, str(ROOT / "pipeline"))

from source import AdsbLolDaySource, BBox          # noqa: E402
from trajectories import flights_from_trace, haversine_km  # noqa: E402
from emissions import openap_model                 # noqa: E402
from excess import excess_for_flight               # noqa: E402

DAY = "v2026.07.23-planes-readsb-prod-0"
PARTS = [ROOT / "data/raw" / f"{DAY}.tar.{s}" for s in ("aa", "ab", "ac")]
BOX = BBox(lat_min=36.0, lat_max=48.0, lon_min=6.0, lon_max=19.0)
OUT = ROOT / "data/interim"
LOAD_FACTOR = 0.82
RESERVE_KG = 2000.0
MAX_FLIGHTS = int(os.environ["MAX_FLIGHTS"]) if os.environ.get("MAX_FLIGHTS") else None

# Major EU-South airports for labelling only (aggregate reporting, not tracking)
AIRPORTS = {
    "LIML": (45.445, 9.278, "Milano Linate"),
    "LIMC": (45.630, 8.728, "Milano Malpensa"),
    "LIME": (45.669, 9.704, "Bergamo Orio"),
    "LIRF": (41.800, 12.239, "Roma Fiumicino"),
    "LIRA": (41.799, 12.595, "Roma Ciampino"),
    "LIPZ": (45.505, 12.352, "Venezia"),
    "LIPE": (44.535, 11.289, "Bologna"),
    "LIRN": (40.886, 14.291, "Napoli"),
    "LICC": (37.467, 15.066, "Catania"),
    "LICJ": (38.176, 13.091, "Palermo"),
    "LIEE": (39.251, 9.054, "Cagliari"),
    "LIMF": (45.201, 7.649, "Torino"),
    "LIPX": (45.396, 10.888, "Verona"),
    "LIRP": (43.684, 10.393, "Pisa"),
    "LIBD": (41.139, 16.760, "Bari"),
    "LICA": (38.912, 16.242, "Lamezia"),
    "LIEO": (40.899, 9.518, "Olbia"),
    "LFMN": (43.665, 7.215, "Nice"),
    "LFML": (43.436, 5.215, "Marseille"),
    "LSZH": (47.458, 8.555, "Zurich"),
    "LSGG": (46.238, 6.109, "Geneva"),
    "LOWW": (48.110, 16.570, "Vienna"),
    "LDZA": (45.743, 16.069, "Zagreb"),
    "LGAV": (37.936, 23.945, "Athens"),  # out of box lon, kept for ref
    "LMML": (35.858, 14.478, "Malta"),
}


def nearest_airport(lat, lon, max_km=12.0):
    best, bestd = None, 1e9
    for icao, (alat, alon, name) in AIRPORTS.items():
        d = haversine_km(lat, lon, alat, alon)
        if d < bestd:
            best, bestd = (icao, name, d), d
    if best and best[2] <= max_km:
        return f"{best[1]} ({best[0]})"
    return f"~{lat:.2f},{lon:.2f}"


def peak_rss_mb():
    kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return kb / 1024.0  # Linux ru_maxrss is KB


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    src = AdsbLolDaySource(PARTS, bbox=BOX)

    t0 = time.time()
    n_traces = n_inbox = n_complete = n_supported = 0
    rows = []

    for trace in src.iter_traces():
        n_traces += 1
        n_inbox += 1  # source already box-filtered
        for fl in flights_from_trace(trace, BOX):
            n_complete += 1
            if openap_model(fl.typecode) is None:
                continue
            n_supported += 1
            res = excess_for_flight(fl, LOAD_FACTOR, RESERVE_KG)
            real = res["real"]
            if not real.ok:
                continue
            p0, p1 = fl.points[0], fl.points[-1]
            rows.append({
                "icao": fl.icao,
                "reg": fl.reg,
                "type": fl.typecode,
                "origin": nearest_airport(p0.lat, p0.lon),
                "dest": nearest_airport(p1.lat, p1.lon),
                "dep_utc": time.strftime("%H:%M", time.gmtime(fl.t_start)),
                "duration_min": round(fl.duration_s / 60.0, 1),
                "gc_km": round(res["gc_km"], 1),
                "dist_flown_km": round(real.dist_flown_km, 1),
                "detour_pct": round(res["detour_pct"], 1) if res["detour_pct"] is not None else None,
                "max_alt_ft": round(fl.max_alt),
                "init_mass_kg": round(real.init_mass_kg),
                "fuel_kg": round(real.fuel_kg),
                "co2_kg": round(real.co2_kg),
                "cruise_ff_kgph": round(real.cruise_ff_kgph),
                "ideal_co2_kg": round(res["ideal"].co2_kg) if res["ideal"] and res["ideal"].ok else None,
                "excess_pct": round(res["excess_pct"], 1) if res["excess_pct"] is not None else None,
                "phase_fuel": {k: round(v) for k, v in (real.phase_fuel or {}).items()},
                "tas_mode": real.tas_mode,
            })
        if n_traces % 20000 == 0:
            print(f"  ... {n_traces} in-box traces, {n_supported} modelled flights, "
                  f"{time.time()-t0:.0f}s, peakRSS {peak_rss_mb():.0f} MB", flush=True)
        if MAX_FLIGHTS and len(rows) >= MAX_FLIGHTS:
            break

    elapsed = time.time() - t0
    summary = {
        "day": DAY,
        "box": [BOX.lat_min, BOX.lat_max, BOX.lon_min, BOX.lon_max],
        "load_factor": LOAD_FACTOR,
        "reserve_kg": RESERVE_KG,
        "n_inbox_traces": n_inbox,
        "n_complete_flights": n_complete,
        "n_supported_modelled": n_supported,
        "n_rows": len(rows),
        "elapsed_s": round(elapsed, 1),
        "peak_rss_mb": round(peak_rss_mb(), 1),
    }
    (OUT / "fase0_results.json").write_text(
        json.dumps({"summary": summary, "flights": rows}, indent=2))
    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))
    print(f"wrote {OUT/'fase0_results.json'}")


if __name__ == "__main__":
    main()
