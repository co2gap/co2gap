#!/usr/bin/env python3
"""
Validation + aggregation over fase0_results.json.

1. Compare each flight's modelled *cruise fuel flow* against published typical
   cruise fuel-flow ranges for its type; flag deviations > 20%.
2. Cross-check trip fuel per km.
3. Aggregate excess-CO2 by unordered route (both directions) to damp the wind
   confound and show the route-level number that the product would publish.
"""
import json
import statistics as st
from collections import defaultdict
from pathlib import Path

RES = Path("/mnt/wd_elements/adsb-co2/data/interim/fase0_results.json")

# Published typical CRUISE fuel flow (kg/h), whole-aircraft, rough industry
# figures for sanity-checking (not a certification source).
REF_CRUISE_FF = {
    "A319": (2000, 2500), "A320": (2350, 2700), "A20N": (1850, 2300),
    "A321": (2800, 3300), "A21N": (2200, 2700),
    "B738": (2350, 2650), "B38M": (2050, 2450), "B737": (2300, 2650),
    "B739": (2450, 2800), "B39M": (2200, 2550),
    "E170": (1250, 1650), "E75L": (1300, 1750), "E190": (1450, 1850),
    "E195": (1500, 1950), "E290": (1400, 1800), "E295": (1500, 1950),
    "B752": (3500, 4200), "B763": (4500, 5300),
    "B788": (4900, 5700), "B789": (5300, 6100), "B77W": (7300, 8200),
    "B772": (6200, 7000), "A332": (5500, 6300), "A333": (5700, 6500),
    "A359": (5600, 6400), "CRJ9": (1300, 1750),
}


def route_key(o, d):
    a, b = sorted([o, d])
    return f"{a}  <->  {b}"


def main():
    data = json.loads(RES.read_text())
    flights = data["flights"]
    summ = data["summary"]

    print("### RUN SUMMARY ###")
    for k, v in summ.items():
        print(f"  {k}: {v}")

    print(f"\n### CRUISE FUEL-FLOW VALIDATION (n={len(flights)}) ###")
    print(f"{'type':5} {'reg':8} {'cruiseFF':>9} {'ref range':>13} {'dev%':>7}  flag")
    devs = []
    n_ok = n_flag = n_noref = 0
    by_type = defaultdict(list)
    for r in sorted(flights, key=lambda x: x["type"]):
        t = r["type"]
        cff = r["cruise_ff_kgph"]
        by_type[t].append(r)
        ref = REF_CRUISE_FF.get(t)
        if not ref or not cff:
            n_noref += 1
            print(f"{t:5} {str(r['reg'])[:8]:8} {cff:9} {'(no ref)':>13} {'-':>7}")
            continue
        lo, hi = ref
        mid = (lo + hi) / 2
        dev = (cff - mid) / mid * 100
        devs.append(dev)
        flag = "" if lo * 0.8 <= cff <= hi * 1.2 else "  <-- >20% OFF"
        if flag:
            n_flag += 1
        else:
            n_ok += 1
        print(f"{t:5} {str(r['reg'])[:8]:8} {cff:9} {str(lo)+'-'+str(hi):>13} {dev:+7.0f}{flag}")

    print(f"\n  in-band (±20% of published): {n_ok}/{n_ok+n_flag}   flagged: {n_flag}   no-ref: {n_noref}")
    if devs:
        print(f"  mean dev {st.mean(devs):+.1f}%  median {st.median(devs):+.1f}%  "
              f"stdev {st.pstdev(devs):.1f}%")

    print("\n### FUEL PER KM (trip fuel / flown distance) ###")
    for t in sorted(by_type):
        vals = [r["fuel_kg"] / r["dist_flown_km"] for r in by_type[t] if r["dist_flown_km"]]
        if vals:
            print(f"  {t:5} n={len(vals):2}  {st.mean(vals):.2f} kg/km "
                  f"(min {min(vals):.2f} max {max(vals):.2f})")

    print("\n### EXCESS CO2 AGGREGATED BY ROUTE (both directions) ###")
    routes = defaultdict(list)
    for r in flights:
        if r["excess_pct"] is None:
            continue
        routes[route_key(r["origin"], r["dest"])].append(r)
    print(f"{'route':44} {'n':>2} {'mean_excess%':>12} {'dir_spread%':>11}")
    agg = []
    for rt, rs in sorted(routes.items(), key=lambda kv: -len(kv[1])):
        ex = [x["excess_pct"] for x in rs]
        spread = (max(ex) - min(ex)) if len(ex) > 1 else 0.0
        agg.append(st.mean(ex))
        if len(rs) >= 2:
            print(f"{rt[:44]:44} {len(rs):>2} {st.mean(ex):>12.1f} {spread:>11.1f}")
    allex = [r["excess_pct"] for r in flights if r["excess_pct"] is not None]
    if allex:
        print(f"\n  per-flight excess: mean {st.mean(allex):.1f}%  median {st.median(allex):.1f}%  "
              f"min {min(allex):.1f}%  max {max(allex):.1f}%  (n={len(allex)})")


if __name__ == "__main__":
    main()
