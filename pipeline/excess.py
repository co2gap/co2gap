"""
Excess CO2 = extra emissions of the real trajectory versus the great-circle
optimum, attributable to routing / levels / holding / congestion.

We build a *nominal* flight for the same aircraft over the great-circle
origin->destination distance (standard climb, cruise at the type's cruise
Mach and altitude, standard descent) and run it through the SAME fuel
integrator with the SAME load assumption. So the only thing that differs
between real and ideal is the flown path — which is exactly what we want to
isolate.

excess% = (co2_real - co2_ideal) / co2_ideal * 100
"""

from __future__ import annotations

import math

import openap.aero as _aero

from trajectories import Flight, Point, haversine_km
from emissions import estimate_fuel, openap_model, FuelResult, _get_ac

CLIMB_RATE_FPM = 2000.0
DESCENT_RATE_FPM = 1800.0
STEP_S = 20.0


def _sound_speed_ms(alt_ft: float) -> float:
    h_m = alt_ft * 0.3048
    try:
        _, _, T = _aero.vatmos(h_m)
        return math.sqrt(1.4 * 287.05287 * T)
    except Exception:
        return 295.0


def _cruise_alt_ft(ac) -> float:
    cruise_h_m = ac.get("cruise", {}).get("height", 11000)
    ceiling_m = ac.get("ceiling", cruise_h_m + 1000)
    alt_ft = min(cruise_h_m, ceiling_m - 500) / 0.3048
    return alt_ft


def build_nominal_flight(typecode: str, gc_km: float) -> Flight | None:
    model = openap_model(typecode)
    if model is None:
        return None
    ac = _get_ac(model)
    cruise_alt = _cruise_alt_ft(ac)
    mach = ac.get("cruise", {}).get("mach", 0.78)
    cruise_tas_kt = mach * _sound_speed_ms(cruise_alt) / 0.514444

    climb_time_s = cruise_alt / CLIMB_RATE_FPM * 60.0
    desc_time_s = cruise_alt / DESCENT_RATE_FPM * 60.0

    # horizontal distance eaten by climb/descent (mean TAS over the phase)
    climb_mean_tas = 0.5 * (250 + cruise_tas_kt)
    desc_mean_tas = 0.5 * (250 + cruise_tas_kt)
    climb_dist_km = climb_mean_tas * 1.852 * (climb_time_s / 3600.0)
    desc_dist_km = desc_mean_tas * 1.852 * (desc_time_s / 3600.0)
    cruise_dist_km = max(gc_km - climb_dist_km - desc_dist_km, 0.0)
    cruise_time_s = cruise_dist_km * 1000.0 / (cruise_tas_kt * 0.514444) if cruise_tas_kt else 0.0

    pts = []
    t = 0.0
    lat = 40.0  # dummy line; fuel model ignores lat/lon, only alt/vs/tas/dt matter
    lon = 9.0

    def push(alt, tas_kt):
        nonlocal t, lat
        # advance a dummy position north so haversine gives ~ the step distance
        step_km = tas_kt * 1.852 * (STEP_S / 3600.0)
        lat_next = lat + step_km / 111.32
        pts.append(Point(t=t, lat=lat, lon=lon, alt=alt, gs=tas_kt, ias=None, vs_rep=None))
        lat = lat_next
        t += STEP_S

    # climb
    n = max(int(climb_time_s / STEP_S), 1)
    for i in range(n):
        frac = i / n
        alt = frac * cruise_alt
        tas = 250 + frac * (cruise_tas_kt - 250)
        push(alt, tas)
    # cruise
    n = max(int(cruise_time_s / STEP_S), 1)
    for i in range(n):
        push(cruise_alt, cruise_tas_kt)
    # descent
    n = max(int(desc_time_s / STEP_S), 1)
    for i in range(n):
        frac = i / n
        alt = cruise_alt * (1 - frac)
        tas = cruise_tas_kt - frac * (cruise_tas_kt - 250)
        push(max(alt, 0.0), tas)
    push(0.0, 250)

    return Flight(icao="NOMINAL", typecode=typecode, reg=None, points=pts)


def excess_for_flight(flight, load_factor, reserve_kg) -> dict:
    p0, p1 = flight.points[0], flight.points[-1]
    gc_km = haversine_km(p0.lat, p0.lon, p1.lat, p1.lon)

    real = estimate_fuel(flight, load_factor=load_factor, reserve_kg=reserve_kg,
                         tas_mode="ias")
    nominal = build_nominal_flight(flight.typecode, gc_km)
    ideal = None
    if nominal is not None:
        ideal = estimate_fuel(nominal, load_factor=load_factor,
                              reserve_kg=reserve_kg, tas_mode="gs")

    out = {
        "gc_km": gc_km,
        "real": real,
        "ideal": ideal,
        "excess_pct": None,
        "detour_pct": None,
    }
    if real.ok and ideal is not None and ideal.ok and ideal.co2_kg > 0:
        out["excess_pct"] = (real.co2_kg - ideal.co2_kg) / ideal.co2_kg * 100.0
        if gc_km > 0:
            out["detour_pct"] = (real.dist_flown_km - gc_km) / gc_km * 100.0
    return out
