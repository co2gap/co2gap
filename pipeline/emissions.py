"""
Fuel burn and CO2 for a reconstructed trajectory, using OpenAP (TU Delft).

Method (transparent on purpose for a phase-0):
  * Airspeed for the fuel model is TAS. ADS-B gives ground speed (wind-tainted)
    and often indicated airspeed (IAS, trace idx 12). We prefer IAS -> TAS
    (ISA, compressibility-correct via openap.aero when available) and fall back
    to GS only when IAS is missing.
  * Mass matters a lot for fuel flow, and we don't know it. We estimate the
    take-off mass from a load assumption and then close the loop:
        m0 = OEW + payload + reserve + trip_fuel_guess
    integrate the burn, set trip_fuel_guess = burned, repeat a few times.
    Mass decreases along the flight as fuel is burned; the aircraft still
    carries `reserve` at landing (realistic, and it slightly raises cruise ff).
  * Fuel flow per step from FuelFlow.enroute(mass, tas, alt, vs); integrate
    piecewise over the real time deltas. CO2 = fuel * 3.16.

Load assumption and reserve are explicit parameters so the phase-0 report can
show the sensitivity instead of hiding it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from openap import FuelFlow, prop
import openap.aero as _aero

CO2_PER_KG_FUEL = 3.16
PAX_WEIGHT_KG = 100.0        # passenger + baggage, standard planning figure
DEFAULT_LOAD_FACTOR = 0.82   # ~ EU average seat load factor
DEFAULT_RESERVE_KG = 2000.0  # final reserve + alternate, carried not burned
KTS_TO_MS = 0.514444


def _ias_to_tas_ms(ias_kt: float, alt_ft: float) -> float:
    """IAS(kt)->TAS(m/s). Use openap.aero compressible relation if present."""
    cas_ms = ias_kt * KTS_TO_MS
    h_m = alt_ft * 0.3048
    for name in ("vcas2vtas", "cas2tas"):
        fn = getattr(_aero, name, None)
        if fn is not None:
            try:
                return float(fn(cas_ms, h_m))
            except Exception:
                pass
    # fallback: incompressible density-ratio (ISA)
    _, rho, _ = _aero.vatmos(h_m) if hasattr(_aero, "vatmos") else (None, None, None)
    if rho:
        return cas_ms * math.sqrt(_aero.rho0 / rho)
    return cas_ms


def _tas_kt(p, mode: str) -> Optional[float]:
    """Best-available TAS in knots for a point."""
    alt = p.alt if p.alt is not None else 0.0
    if mode != "gs" and p.ias is not None and p.ias > 40 and alt is not None:
        return _ias_to_tas_ms(p.ias, alt) / KTS_TO_MS
    if p.gs is not None:
        return p.gs
    return None


def phase_of(alt_ft: float, vs_fpm: float) -> str:
    if alt_ft is None:
        return "unknown"
    if alt_ft < 1000:
        return "ground"
    if vs_fpm > 350:
        return "climb"
    if vs_fpm < -350:
        return "descent"
    return "cruise"


@dataclass
class FuelResult:
    typecode: str
    ok: bool
    reason: str = ""
    fuel_kg: float = 0.0
    co2_kg: float = 0.0
    duration_s: float = 0.0
    dist_flown_km: float = 0.0
    init_mass_kg: float = 0.0
    cruise_ff_kgph: float = 0.0     # mean fuel flow during cruise
    tas_mode: str = ""
    phase_fuel: dict = None


# OpenAP type-code aliases -> supported model
_ALIAS = {
    "A19N": "a19n", "A20N": "a20n", "A21N": "a21n",
    "A318": "a318", "A319": "a319", "A320": "a320", "A321": "a321",
    "B737": "b737", "B738": "b738", "B739": "b739",
    "B37M": "b37m", "B38M": "b38m", "B39M": "b39m",
    "B752": "b752", "B763": "b763",
    "E170": "e170", "E75L": "e75l", "E75S": "e75l", "E190": "e190",
    "E195": "e195", "E290": "e190", "E295": "e195",
    "A332": "a332", "A333": "a333", "A359": "a359",
    "B77W": "b77w", "B772": "b772", "B788": "b788", "B789": "b789",
    "CRJ9": "crj9", "C550": "c550", "GLF6": "glf6",
}


def openap_model(typecode: str) -> Optional[str]:
    return _ALIAS.get((typecode or "").upper())


_FF_CACHE: dict = {}
_AC_CACHE: dict = {}


def _get_ff(model: str) -> FuelFlow:
    ff = _FF_CACHE.get(model)
    if ff is None:
        ff = _FF_CACHE[model] = FuelFlow(ac=model)
    return ff


def _get_ac(model: str) -> dict:
    ac = _AC_CACHE.get(model)
    if ac is None:
        ac = _AC_CACHE[model] = prop.aircraft(model)
    return ac


def estimate_fuel(flight, load_factor=DEFAULT_LOAD_FACTOR,
                  reserve_kg=DEFAULT_RESERVE_KG, tas_mode="ias",
                  iters=3) -> FuelResult:
    from trajectories import haversine_km

    model = openap_model(flight.typecode)
    if model is None:
        return FuelResult(flight.typecode, False, "type not in OpenAP")

    ac = _get_ac(model)
    oew = ac["oew"]
    mtow = ac["mtow"]
    pax_max = ac.get("pax", {}).get("max", 150)
    payload = load_factor * pax_max * PAX_WEIGHT_KG
    ff = _get_ff(model)

    pts = flight.points
    # precompute per-step tas / vs / dt / dist
    steps = []
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        dt = b.t - a.t
        if dt <= 0 or dt > 600:
            continue
        alt = a.alt if a.alt is not None else 0.0
        alt_b = b.alt if b.alt is not None else alt
        vs = (alt_b - alt) / dt * 60.0     # fpm, derived
        tas = _tas_kt(a, tas_mode)
        if tas is None:
            continue
        d_km = haversine_km(a.lat, a.lon, b.lat, b.lon)
        steps.append((dt, alt, vs, tas, d_km))
    if len(steps) < 10:
        return FuelResult(flight.typecode, False, "too few usable steps")

    dist_km = sum(s[4] for s in steps)
    duration = sum(s[0] for s in steps)

    trip_fuel = 0.25 * (mtow - oew)   # first guess
    burned = 0.0
    for _ in range(iters):
        m0 = min(oew + payload + reserve_kg + trip_fuel, mtow)
        mass = m0
        burned = 0.0
        for (dt, alt, vs, tas, _d) in steps:
            fps = float(ff.enroute(mass=mass, tas=tas, alt=alt, vs=vs))  # kg/s
            if not math.isfinite(fps) or fps < 0:
                fps = 0.0
            step_burn = fps * dt
            burned += step_burn
            mass = max(mass - step_burn, oew)
        trip_fuel = burned

    # phase breakdown + cruise ff, one final pass at converged m0
    m0 = min(oew + payload + reserve_kg + trip_fuel, mtow)
    mass = m0
    phase_fuel = {"climb": 0.0, "cruise": 0.0, "descent": 0.0,
                  "ground": 0.0, "unknown": 0.0}
    cruise_burn = 0.0
    cruise_time = 0.0
    for (dt, alt, vs, tas, _d) in steps:
        fps = float(ff.enroute(mass=mass, tas=tas, alt=alt, vs=vs))
        if not math.isfinite(fps) or fps < 0:
            fps = 0.0
        b = fps * dt
        ph = phase_of(alt, vs)
        phase_fuel[ph] += b
        if ph == "cruise":
            cruise_burn += b
            cruise_time += dt
        mass = max(mass - b, oew)

    cruise_ff = (cruise_burn / cruise_time * 3600.0) if cruise_time > 0 else 0.0

    return FuelResult(
        typecode=flight.typecode, ok=True, fuel_kg=burned,
        co2_kg=burned * CO2_PER_KG_FUEL, duration_s=duration,
        dist_flown_km=dist_km, init_mass_kg=m0, cruise_ff_kgph=cruise_ff,
        tas_mode=tas_mode, phase_fuel=phase_fuel,
    )
