"""
Wind-aware excess CO2 (phase-1 gate).

Why the fase-0 excess had a per-direction bias: the REAL flight's fuel already
reflects wind, because it is integrated over the flight's actual airborne time
(a tailwind shortens the flight -> less fuel; a headwind lengthens it -> more).
The fase-0 NOMINAL baseline did not: it timed the great-circle cruise at true
airspeed with no wind, so the same route got the same baseline both ways while
the real flights diverged. Excess = real - nominal therefore carried a raw wind
term -> large dir_spread.

Fix: give the nominal the SAME wind term. Time the nominal's cruise at ground
speed = cruise_TAS + along-track_wind on the great circle at the flight's time
(fuel flow still uses TAS — airspeed is unchanged, only the clock changes). Now
the wind cancels between real and nominal, leaving genuine inefficiency
(extra distance, non-optimal levels, holding).

Real fuel is taken as the IAS-based v0 already computed by the Pi (validated in
fase 0, wind-correct via duration). Only the nominal is rebuilt here.
"""

from __future__ import annotations

import math

import numpy as np

import openap.aero as _aero
from trajectories import Flight, Point, haversine_km
from emissions import estimate_fuel, openap_model, _get_ac

CLIMB_RATE_FPM = 2000.0
DESCENT_RATE_FPM = 1800.0
STEP_S = 20.0
KTS_TO_MS = 0.514444


def _sound_speed_ms(alt_ft: float) -> float:
    try:
        _, _, T = _aero.vatmos(alt_ft * 0.3048)
        return math.sqrt(1.4 * 287.05287 * T)
    except Exception:
        return 295.0


def _cruise_alt_ft(ac) -> float:
    cruise_h_m = ac.get("cruise", {}).get("height", 11000)
    ceiling_m = ac.get("ceiling", cruise_h_m + 1000)
    return min(cruise_h_m, ceiling_m - 500) / 0.3048


# ---- great-circle sampling -------------------------------------------------

def _gc_sample(lat1, lon1, lat2, lon2, n):
    """Return arrays of (lat, lon, bearing_deg) at n points along the GC."""
    p1, l1 = math.radians(lat1), math.radians(lon1)
    p2, l2 = math.radians(lat2), math.radians(lon2)
    dp = p2 - p1
    dl = l2 - l1
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    d = 2 * math.asin(min(1.0, math.sqrt(a)))  # angular distance
    lats, lons, brgs = [], [], []
    if d == 0:
        return [lat1], [lon1], [0.0]
    for i in range(n):
        f = i / (n - 1) if n > 1 else 0.0
        A = math.sin((1 - f) * d) / math.sin(d)
        B = math.sin(f * d) / math.sin(d)
        x = A * math.cos(p1) * math.cos(l1) + B * math.cos(p2) * math.cos(l2)
        y = A * math.cos(p1) * math.sin(l1) + B * math.cos(p2) * math.sin(l2)
        z = A * math.sin(p1) + B * math.sin(p2)
        lat = math.atan2(z, math.hypot(x, y))
        lon = math.atan2(y, x)
        # bearing toward destination
        bl = math.radians(lon2) - lon
        by = math.sin(bl) * math.cos(p2)
        bx = math.cos(lat) * math.sin(p2) - math.sin(lat) * math.cos(p2) * math.cos(bl)
        brg = math.degrees(math.atan2(by, bx)) % 360
        lats.append(math.degrees(lat))
        lons.append(math.degrees(lon))
        brgs.append(brg)
    return lats, lons, brgs


def mean_along_track_wind_ms(lat1, lon1, lat2, lon2, dep_ts, cruise_tas_kt,
                             cruise_alt_ft, windfield, gc_km, n=24) -> float:
    """Mean tailwind component (m/s, +=tailwind) along the GC at cruise level."""
    lats, lons, brgs = _gc_sample(lat1, lon1, lat2, lon2, n)
    if len(lats) < 2:
        return 0.0
    dur_s = gc_km / (cruise_tas_kt * 1.852) * 3600.0 if cruise_tas_kt else 3600.0
    ts = [dep_ts + (i / (len(lats) - 1)) * dur_s for i in range(len(lats))]
    alt = np.full(len(lats), cruise_alt_ft)
    u, v = windfield.uv(np.array(ts), np.array(lats), np.array(lons), alt)
    br = np.radians(np.array(brgs))
    wpar = u * np.sin(br) + v * np.cos(br)   # track unit vec = (sin b, cos b)
    return float(np.nanmean(wpar))


def build_nominal_windaware(typecode, gc_km, mean_wpar_ms) -> Flight | None:
    model = openap_model(typecode)
    if model is None:
        return None
    ac = _get_ac(model)
    cruise_alt = _cruise_alt_ft(ac)
    mach = ac.get("cruise", {}).get("mach", 0.78)
    cruise_tas_kt = mach * _sound_speed_ms(cruise_alt) / KTS_TO_MS
    cruise_tas_ms = cruise_tas_kt * KTS_TO_MS
    gs_cruise_ms = max(cruise_tas_ms + mean_wpar_ms, 50.0)  # guard absurd headwind

    climb_time_s = cruise_alt / CLIMB_RATE_FPM * 60.0
    desc_time_s = cruise_alt / DESCENT_RATE_FPM * 60.0
    mean_tas = 0.5 * (250 + cruise_tas_kt)
    climb_dist_km = mean_tas * 1.852 * (climb_time_s / 3600.0)
    desc_dist_km = mean_tas * 1.852 * (desc_time_s / 3600.0)
    cruise_dist_km = max(gc_km - climb_dist_km - desc_dist_km, 0.0)
    # wind enters here: cruise clock uses ground speed, fuel model still uses TAS
    cruise_time_s = cruise_dist_km * 1000.0 / gs_cruise_ms

    pts = []
    t = 0.0
    lat = 40.0
    lon = 9.0

    def push(alt, tas_kt):
        nonlocal t, lat
        step_km = tas_kt * 1.852 * (STEP_S / 3600.0)
        pts.append(Point(t=t, lat=lat, lon=lon, alt=alt, gs=tas_kt, ias=None, vs_rep=None))
        lat = lat + step_km / 111.32
        t += STEP_S

    n = max(int(climb_time_s / STEP_S), 1)
    for i in range(n):
        frac = i / n
        push(frac * cruise_alt, 250 + frac * (cruise_tas_kt - 250))
    n = max(int(cruise_time_s / STEP_S), 1)
    for i in range(n):
        push(cruise_alt, cruise_tas_kt)
    n = max(int(desc_time_s / STEP_S), 1)
    for i in range(n):
        frac = i / n
        push(max(cruise_alt * (1 - frac), 0.0), cruise_tas_kt - frac * (cruise_tas_kt - 250))
    push(0.0, 250)
    return Flight(icao="NOMINAL", typecode=typecode, reg=None, points=pts)


def ideal_co2_windaware(typecode, o_lat, o_lon, d_lat, d_lon, dep_ts, gc_km,
                        windfield, load_factor=0.82, reserve_kg=2000.0):
    """CO2 of the wind-aware great-circle optimum for this flight. None if unmodelable."""
    model = openap_model(typecode)
    if model is None:
        return None
    ac = _get_ac(model)
    cruise_alt = _cruise_alt_ft(ac)
    mach = ac.get("cruise", {}).get("mach", 0.78)
    cruise_tas_kt = mach * _sound_speed_ms(cruise_alt) / KTS_TO_MS
    wpar = mean_along_track_wind_ms(o_lat, o_lon, d_lat, d_lon, dep_ts,
                                    cruise_tas_kt, cruise_alt, windfield, gc_km)
    nominal = build_nominal_windaware(typecode, gc_km, wpar)
    if nominal is None:
        return None
    res = estimate_fuel(nominal, load_factor=load_factor, reserve_kg=reserve_kg,
                        tas_mode="gs")
    if not res.ok:
        return None
    return {"ideal_co2_kg": res.co2_kg, "mean_wpar_ms": wpar,
            "cruise_tas_kt": cruise_tas_kt}
