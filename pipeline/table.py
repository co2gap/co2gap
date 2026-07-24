#!/usr/bin/env python3
import json
from pathlib import Path
d = json.loads(Path("/mnt/wd_elements/adsb-co2/data/interim/fase0_results.json").read_text())
fl = d["flights"]

def named(r):
    return "(" not in r["origin"][:1] and "~" not in r["origin"] and "~" not in r["dest"]

# curated: pick flights with both airports named, across types, incl. biased ones
want = ["A320","B738","A319","B38M","A321","B789","B752","A20N","A21N","E190","E195","GLF6"]
picked = []
seen = set()
for t in want:
    cands = [r for r in fl if r["type"]==t and named(r) and r["dep_utc"]]
    cands.sort(key=lambda r: -r["gc_km"])
    for r in cands:
        key=(t, r["origin"], r["dest"])
        if key in seen: continue
        seen.add(key); picked.append(r); break

hdr = f"| {'type':4} | {'route':34} | {'dep':5} | {'min':4} | {'GC km':6} | {'flown':6} | {'det%':4} | {'m0 t':4} | {'fuel kg':7} | {'CO2 kg':7} | {'crFF':5} | {'excess%':7} |"
print(hdr)
print("|"+"-"*6+"|"+"-"*36+"|"+"-"*7+"|"+"-"*6+"|"+"-"*8+"|"+"-"*8+"|"+"-"*6+"|"+"-"*6+"|"+"-"*9+"|"+"-"*9+"|"+"-"*7+"|"+"-"*9+"|")
for r in picked:
    route=f"{r['origin'].split(' (')[0]}->{r['dest'].split(' (')[0]}"
    print(f"| {r['type']:4} | {route[:34]:34} | {r['dep_utc']:5} | {r['duration_min']:4.0f} | {r['gc_km']:6.0f} | {r['dist_flown_km']:6.0f} | {r['detour_pct']:4.0f} | {r['init_mass_kg']/1000:4.1f} | {r['fuel_kg']:7.0f} | {r['co2_kg']:7.0f} | {r['cruise_ff_kgph']:5.0f} | {str(r['excess_pct']):>7} |")
