#!/usr/bin/env python3
"""
Per-type calibration (phase-1 objective 3).

The fase-0 validation found systematic per-type biases in the OpenAP fuel model:
the A320neo/A321neo run low (the a21n has no dedicated drag polar and borrows
the smaller a20n's) and the Embraer E-Jets run high. The bias is essentially
multiplicative on cruise fuel flow, so we correct it with a transparent scalar
factor per type, anchored to published typical cruise fuel flow.

We keep the stored co2_kg_v0 UNcalibrated (model-version independent, durable)
and apply the factor downstream. This script derives the factors from the
accumulated flights and writes data/calibration.json:

    factor[type] = published_cruise_ff_mid / observed_median_cruise_ff

A factor is emitted only for types whose observed median deviates > TOL from the
published mid (others stay 1.0). Every factor and its anchor is printed for the
README. After applying, all mainstream types should sit within +-15%.

Flights with poor coverage are excluded from the fit (flown>=0.9*GC, coverage).
"""

from __future__ import annotations

import json
import statistics as st
from collections import defaultdict
from pathlib import Path

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
FLIGHTS_DIR = ROOT / "data/flights"
OUT = ROOT / "data/calibration.json"
TOL = 0.10          # only correct types off by more than 10%
# Emit a factor only from a well-sampled median. With n in the teens the median
# is noisy and we would be fitting sampling noise into a "correction" — visible
# in the widebodies, where small-n types disagree in sign among near-identical
# aircraft. Under-sampled types stay uncalibrated and are flagged provisional.
MIN_N = 100
MIN_N_SHOW = 8      # still report the observed median down to this n

# Published typical CRUISE fuel flow (kg/h), whole-aircraft — industry figures
# used as calibration anchors. Documented in the README; a firmer source
# (EEA/EMEP, ICAO fuel tables) is a follow-up.
PUB_CRUISE_FF = {
    "A319": 2250, "A320": 2525, "A20N": 2075, "A321": 3050, "A21N": 2450,
    "B738": 2500, "B38M": 2250, "B737": 2475, "B739": 2625, "B39M": 2375,
    "E170": 1450, "E75L": 1525, "E190": 1650, "E195": 1725, "E290": 1600,
    "E295": 1725, "B752": 3850, "B763": 4900, "B788": 5300, "B789": 5700,
    "B77W": 7750, "B772": 6600, "A332": 5900, "A333": 6100, "A359": 6000,
    "CRJ9": 1525,
}


def load_clean():
    rows = defaultdict(list)   # type -> [cruise_ff]
    for d in sorted(FLIGHTS_DIR.glob("*")):
        f = d / "flights.parquet"
        if not f.exists():
            continue
        df = pq.read_table(f, columns=[
            "typecode", "cruise_ff_kgph_v0", "flown_ge_09gc",
            "coverage_frac", "gc_km"]).to_pandas()
        df = df[(df.flown_ge_09gc) & (df.coverage_frac >= 0.9)
                & (df.gc_km >= 150) & (df.cruise_ff_kgph_v0 > 0)]
        for t, ff in zip(df.typecode, df.cruise_ff_kgph_v0):
            rows[t].append(float(ff))
    return rows


def main():
    rows = load_clean()
    factors = {}
    print(f"{'type':5} {'n':>4} {'obs_median':>10} {'published':>9} {'dev%':>6} {'factor':>7}")
    provisional = []
    for t in sorted(rows, key=lambda x: -len(rows[x])):
        ffs = rows[t]
        if len(ffs) < MIN_N_SHOW:
            continue
        obs = st.median(ffs)
        pub = PUB_CRUISE_FF.get(t)
        if not pub:
            print(f"{t:5} {len(ffs):>4} {obs:>10.0f} {'(no ref)':>9}")
            continue
        dev = (obs - pub) / pub * 100
        factor = pub / obs
        mark = ""
        if abs(factor - 1.0) > TOL:
            if len(ffs) >= MIN_N:
                factors[t] = round(factor, 4)
                mark = "  <-- calibrated"
            else:
                provisional.append((t, len(ffs), dev))
                mark = f"  <-- provisional (n<{MIN_N}, NOT calibrated)"
        print(f"{t:5} {len(ffs):>4} {obs:>10.0f} {pub:>9} {dev:>+6.0f} {factor:>7.3f}{mark}")

    OUT.write_text(json.dumps(factors, indent=2, sort_keys=True))
    print(f"\nwrote {OUT} with {len(factors)} correction factor(s):")
    print(json.dumps(factors, indent=2, sort_keys=True))

    if provisional:
        print("\nPROVISIONAL (bias seen but sample too small to correct): "
              + ", ".join(f"{t} n={n} ({d:+.0f}%)" for t, n, d in provisional))

    # Post-calibration check. NOTE: for a CALIBRATED type this is tautological
    # (the factor is defined to hit the published value) — it is a consistency
    # check, not independent validation. The meaningful evidence is the set of
    # UNcalibrated types: those were modelled with no anchoring and still land
    # near the published figures, which is what validates the underlying model.
    print("\n### deviation vs published, after calibration ###")
    worst_uncal = 0.0
    print(f"  {'type':5} {'n':>5} {'dev%':>6}   status")
    for t in sorted(rows):
        ffs = rows[t]
        pub = PUB_CRUISE_FF.get(t)
        if len(ffs) < MIN_N_SHOW or not pub:
            continue
        k = factors.get(t, 1.0)
        dev = (st.median(ffs) * k - pub) / pub * 100
        if t in factors:
            status = "calibrated (anchored: dev=0 by construction)"
        else:
            status = "UNcalibrated -> independent check"
            worst_uncal = max(worst_uncal, abs(dev))
        flag = "  <-- >15%" if abs(dev) > 15 else ""
        print(f"  {t:5} {len(ffs):>5} {dev:>+6.0f}   {status}{flag}")
    print(f"\nworst |dev| among UNCALIBRATED types: {worst_uncal:.0f}%  (target <=15%)")


if __name__ == "__main__":
    main()
