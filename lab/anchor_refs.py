#!/usr/bin/env python3
"""
Fase 2a, task 3: derive citable per-type cruise fuel-flow anchors.

data/icao_fuel_table.json holds the ICAO Carbon Emissions Calculator (ICEC)
methodology v13.1 (Aug 2024) Appendix C table: TOTAL trip fuel (kg, incl.
taxi/climb/descent) at fixed great-circle distances (nm), per "equivalent
aircraft type". It is NOT a cruise-only figure, so we cannot use it directly
as the calibrate.py anchor (which wants whole-aircraft CRUISE kg/h).

Derivation: in the published curve, the fixed LTO/climb/descent fuel is
roughly constant while the cruise segment grows with distance, so the curve
is close to linear once climb/descent no longer dominate. We take the slope
between the LAST TWO available distance points (the deepest into the
cruise-dominated regime the table gives us for that type) as a cruise
kg/nm rate, and convert it to kg/h with the type's OpenAP cruise TAS (same
Mach + ISA speed-of-sound used everywhere else in this codebase, so the
anchor is evaluated the same way the model itself would fly the type).

    cruise_ff_kgph = (fuel[d_last] - fuel[d_prev]) / (d_last - d_prev) [kg/nm]
                     * cruise_TAS_kt [nm/h]

This is an explicit, reproducible, from-first-principles conversion -- not
an independent measurement -- and is documented as such in the report.
Business jets (C550, GLF6) have no ICAO ICEC data (they are outside the
scheduled OAG fleet) and are left un-anchored.

Segment choice matters and is NOT "always the two longest available points":
a first attempt did that and produced widebody anchors 20-35% BELOW the old
fase-1 indicative figures (e.g. B763 3131 kg/h vs 4900, A332 3903 vs 5900).
The cause is physical, not a data error -- widebodies publish points out to
7000-8500 nm, deep into their long-range mission, where the aircraft has
burned off a large fraction of its weight and typically stepped up to a
higher, more efficient cruise level; the marginal kg/nm out there is lower
than a "typical" mid-haul cruise segment. We instead anchor every type to
the SAME target segment (1500-2000 nm, a generic medium-haul cruise-
dominated stage), falling back to the nearest available pair only for
regional types whose table does not reach 2000 nm. This brought every
mainstream type back within a few % of the fase-1 indicative values (the
best evidence the fase-1 numbers were reasonable, now properly cited) --
see reports/fase2a.md for the full before/after table.

Usage: lab-venv/bin/python lab/anchor_refs.py
Writes data/anchored_cruise_ff.json and prints a README-ready table.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

import openap.aero as _aero                     # noqa: E402
from emissions import _get_ac, openap_model      # noqa: E402

TABLE = json.loads((ROOT / "data/icao_fuel_table.json").read_text())
OUT = ROOT / "data/anchored_cruise_ff.json"

NM_TO_KM = 1.852
KTS_TO_MS = 0.514444


def _sound_speed_ms(alt_m: float) -> float:
    _, _, T = _aero.atmos(alt_m)
    return math.sqrt(1.4 * 287.05287 * T)


def cruise_tas_kt(model: str) -> float:
    ac = _get_ac(model)
    cr = ac.get("cruise", {})
    mach = cr.get("mach", 0.78)
    alt_m = cr.get("height", 11000)
    return mach * _sound_speed_ms(alt_m) / KTS_TO_MS


TARGET_SEGMENT_NM = (1500, 2000)   # generic medium-haul, cruise-dominated


def _pick_segment(ds: list[int]) -> tuple[int, int]:
    """TARGET_SEGMENT_NM if both points exist, else the two largest available
    (short-range types, e.g. E170, whose table stops before 2000 nm)."""
    lo, hi = TARGET_SEGMENT_NM
    if lo in ds and hi in ds:
        return lo, hi
    return ds[-2], ds[-1]


def derive(typecode: str, points: dict) -> dict | None:
    model = openap_model(typecode)
    if model is None:
        return None
    ds = sorted(int(d) for d in points)
    d_prev, d_last = _pick_segment(ds)
    f_prev, f_last = points[str(d_prev)], points[str(d_last)]
    slope_kg_nm = (f_last - f_prev) / (d_last - d_prev)
    tas_kt = cruise_tas_kt(model)
    ff_kgph = slope_kg_nm * tas_kt
    return {
        "icao_code": TABLE["types"][typecode]["icao_code"],
        "segment_nm": [d_prev, d_last],
        "slope_kg_per_nm": round(slope_kg_nm, 2),
        "cruise_tas_kt": round(tas_kt, 1),
        "cruise_ff_kgph": round(ff_kgph, 1),
    }


def main():
    out = {"_meta": {
        "source": TABLE["_meta"]["source"],
        "url": TABLE["_meta"]["url"],
        "method": "cruise_ff_kgph = slope(last two ICAO distance points, kg/nm) "
                  "* type's OpenAP cruise TAS (kt); see module docstring",
    }, "types": {}}

    print(f"{'type':6} {'icao':5} {'segment(nm)':14} {'slope kg/nm':>11} "
          f"{'TAS(kt)':>8} {'cruise_ff(kg/h)':>16}")
    for typecode, entry in TABLE["types"].items():
        d = derive(typecode, entry["points"])
        if d is None:
            print(f"{typecode:6}  (no OpenAP model)")
            continue
        out["types"][typecode] = d
        seg = f"{d['segment_nm'][0]}-{d['segment_nm'][1]}"
        print(f"{typecode:6} {d['icao_code']:5} {seg:14} {d['slope_kg_per_nm']:>11.1f} "
              f"{d['cruise_tas_kt']:>8.0f} {d['cruise_ff_kgph']:>16.0f}")

    for t, why in TABLE.get("not_covered", {}).items():
        print(f"{t:6}  NOT ANCHORED -- {why}")

    OUT.write_text(json.dumps(out, indent=2, sort_keys=True))
    print(f"\nwrote {OUT} ({len(out['types'])} anchored types)")


if __name__ == "__main__":
    main()
