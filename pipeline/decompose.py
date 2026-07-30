"""
Fase 2a, task 2: split the excess into a LATERAL and a VERTICAL/SPEED part.

Why this exists: our excess is measured against a fuel-optimal great-circle
baseline, so it bundles two very different inefficiencies —

  * flying FURTHER than the direct route (routing, vectoring, airway
    structure). This is the part homogeneous to EUROCONTROL's KEA, which is
    a pure DISTANCE ratio and runs at a few percent in Europe.
  * flying the path you were given BADLY in the vertical/speed sense
    (non-optimal cruise level, step climbs denied, early descent, holding).
    KEA cannot see this at all.

Without the split our number is not comparable to any published benchmark,
which is criterion 5 of the go/no-go. With it, the lateral part becomes
directly checkable against ~3% KEA.

Method — an INTERMEDIATE baseline between reality and the GC optimum:

    ideal_gc  = optimal vertical profile over the GREAT-CIRCLE distance
    hybrid    = optimal vertical profile over the REAL GROUND-TRACK distance
    real      = what the aircraft actually burned

    excess_lateral  = (hybrid - ideal_gc) / ideal_gc * 100
    excess_vertical = (real   - hybrid  ) / ideal_gc * 100

    excess_total    = (real   - ideal_gc) / ideal_gc * 100
                    = excess_lateral + excess_vertical      (exactly additive)

The vertical term is then opened up further, because on its own it is the
largest and least explained part of the result: it cannot be acted on, and
it has no external benchmark. Two more baselines on the same real ground
track each move ONE factor away from the hybrid:

    C = real cruise altitude, optimal speed for that altitude
    D = optimal cruise altitude, real cruise Mach

    excess_vert_alt      = (C - hybrid) / ideal_gc * 100
    excess_vert_speed    = (D - hybrid) / ideal_gc * 100
    excess_vert_residual = excess_vertical - alt - speed

They are deliberately NOT chained into "first altitude, then speed". In a
chain the final step is always synthetic -> real, and that step silently
absorbs everything the baselines do not model: climb and descent shape,
step climbs, level-offs, holding, and plain model residual. Which factor
ends up carrying that depends on the order — measured on a real day, the
two orders disagreed by ~5 points on a vertical term of ~11 — and a column
called "speed" would be holding things that are not speed.

So the two named parts are clean single-factor effects against a common
reference, and everything else is put in a residual that is named rather
than hidden. The residual is genuinely large; that is a fact about how
much of the vertical term is neither cruise level nor cruise Mach, not a
defect of the split.

D carries the real cruise MACH, not the real TAS: crews hold a Mach in
cruise, so imposing a TAS measured at FL280 onto FL380 would price a
profile nobody would ever fly.

Both components use the SAME denominator (ideal_gc), which is what makes
them additive and lets them be read as "percentage points of the total".

Two deliberate choices, both of which matter for interpretation:

  * The cruise ALTITUDE of the hybrid is the one optimal for the GC
    distance, not for the (longer) flown distance. We want the lateral
    component to isolate ONE thing — extra distance — so everything else is
    held fixed. Letting the altitude re-optimise over the longer path would
    quietly credit a detour with a better cruise level and shrink the
    lateral term.
  * The hybrid's along-track WIND is sampled along the REAL polyline, not
    the great circle. A detour flown to catch a tailwind should show up as
    cheap lateral excess, and one flown into a headwind as expensive; using
    GC wind for both would erase exactly the effect the fase-1 gate was
    built to model.

Also returned are two pure geometric quantities, which carry none of our
fuel-model assumptions:

  * dist_ratio        = flown / great-circle over the WHOLE track
  * dist_ratio_enroute= the same restricted to the en-route portion, i.e.
    with the terminal areas cut out (points within TMA_RADIUS_NM of either
    airport dropped, then track length compared to the great circle between
    the entry and exit points of what remains).

The second one exists because EUROCONTROL's KEA — the ~3% figure we want to
reconcile against — is an EN-ROUTE metric computed outside a 40 NM cylinder
around origin and destination. Comparing our gate-to-gate ratio against it
would overstate our disagreement: terminal vectoring, SIDs and STARs are
real extra distance, but KEA deliberately does not count them. Note the
remaining reference difference we CANNOT remove: KEA measures against the
shortest CONSTRAINED route (the shortest one the route network actually
allows), while we measure against the unconstrained great circle, so our
figure should still sit somewhat above KEA even after trimming.
"""

from __future__ import annotations

import math

import numpy as np

from excess_wind import (_build_profile, _cruise_tas_kt, optimal_cruise_alt_ft,
                         mean_along_track_wind_ms, _sound_speed_ms)
from emissions import estimate_fuel, openap_model, _get_ac, _ias_to_tas_ms

KTS_TO_MS = 0.514444
EARTH_R_KM = 6371.0088
TMA_RADIUS_NM = 40.0                      # EUROCONTROL KEA terminal exclusion
TMA_RADIUS_KM = TMA_RADIUS_NM * 1.852


def _haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = p2 - p1
    dlmb = np.radians(np.asarray(lon2) - np.asarray(lon1))
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2) ** 2
    return 2 * EARTH_R_KM * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def enroute_dist_ratio(lat, lon):
    """
    KEA-homogeneous distance ratio: drop the terminal areas, then compare the
    remaining track length to the great circle between where it enters and
    leaves them. Returns (ratio, enroute_track_km) or (nan, nan) when the
    flight is too short to have an en-route phase at all (both cylinders
    overlap), which is the honest answer rather than a fabricated 1.0.
    """
    lat = np.asarray(lat, float)
    lon = np.asarray(lon, float)
    d_o = _haversine_km(lat[0], lon[0], lat, lon)
    d_d = _haversine_km(lat[-1], lon[-1], lat, lon)
    outside = (d_o > TMA_RADIUS_KM) & (d_d > TMA_RADIUS_KM)
    idx = np.flatnonzero(outside)
    if idx.size < 3:
        return float("nan"), float("nan")
    # contiguous span from first to last en-route point: a flight that dips
    # back inside a cylinder mid-route (rare) still counts as one en-route leg
    i0, i1 = idx[0], idx[-1]
    seg_lat, seg_lon = lat[i0:i1 + 1], lon[i0:i1 + 1]
    flown = _track_len_km(seg_lat, seg_lon)
    gc = float(_haversine_km(seg_lat[0], seg_lon[0], seg_lat[-1], seg_lon[-1]))
    if gc <= 0:
        return float("nan"), float("nan")
    return flown / gc, flown


def _track_len_km(lat, lon):
    """Cumulative great-circle length of a polyline (km), vectorised."""
    p1, p2 = np.radians(lat[:-1]), np.radians(lat[1:])
    dphi = p2 - p1
    dlmb = np.radians(lon[1:] - lon[:-1])
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2) ** 2
    return float(np.sum(2 * EARTH_R_KM * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))))


def _resample_track(lat, lon, n):
    """Resample a polyline to n points equally spaced along its arc length.

    The raw trajectory is time-sampled, so it is dense where the aircraft is
    slow (climb/descent, holding) and sparse in cruise. Averaging wind over
    time-sampled points would over-weight the terminal areas; we want a
    DISTANCE-weighted mean because that is how the wind enters the cruise
    clock in the baseline.
    """
    lat = np.asarray(lat, float)
    lon = np.asarray(lon, float)
    p1, p2 = np.radians(lat[:-1]), np.radians(lat[1:])
    dphi = p2 - p1
    dlmb = np.radians(lon[1:] - lon[:-1])
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2) ** 2
    seg = 2 * EARTH_R_KM * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))
    s = np.concatenate([[0.0], np.cumsum(seg)])
    total = s[-1]
    if total <= 0:
        return lat[:1], lon[:1]
    targets = np.linspace(0.0, total, n)
    return np.interp(targets, s, lat), np.interp(targets, s, lon)


def _bearings_deg(lat, lon):
    """Initial bearing of each segment of a polyline; length n-1."""
    p1, p2 = np.radians(lat[:-1]), np.radians(lat[1:])
    dl = np.radians(lon[1:] - lon[:-1])
    y = np.sin(dl) * np.cos(p2)
    x = np.cos(p1) * np.sin(p2) - np.sin(p1) * np.cos(p2) * np.cos(dl)
    return np.degrees(np.arctan2(y, x)) % 360.0


def mean_wind_along_track(lat, lon, dep_ts, dur_s, cruise_alt_ft,
                          windfield, n=24) -> float:
    """Distance-weighted mean tailwind component (m/s, + = tailwind) along a
    real ground track, at the baseline's cruise level."""
    rlat, rlon = _resample_track(lat, lon, n)
    if len(rlat) < 2:
        return 0.0
    brgs = _bearings_deg(rlat, rlon)
    # midpoint of each segment, and the time the baseline would pass it
    mlat = 0.5 * (rlat[:-1] + rlat[1:])
    mlon = 0.5 * (rlon[:-1] + rlon[1:])
    frac = (np.arange(len(brgs)) + 0.5) / len(brgs)
    ts = dep_ts + frac * dur_s
    alt = np.full(len(brgs), cruise_alt_ft)
    u, v = windfield.uv(ts, mlat, mlon, alt)
    br = np.radians(brgs)
    wpar = u * np.sin(br) + v * np.cos(br)
    return _effective_wind_ms(wpar)


# A representative cruise TAS, used only to weight the segments below. The
# result is insensitive to it at the second decimal: it enters the correction
# as (sigma_w/TAS)^2.
_TAS_REF_MS = 230.0


def _effective_wind_ms(wpar) -> float:
    """Wind component that reproduces the correct FLIGHT TIME, not the correct
    average wind.

    The baseline is clocked as time = D / (TAS + w). Over a path where the wind
    varies, the true time is the integral of dx/(TAS+w), so the value that must
    be fed in is the one satisfying D/(TAS+w_eff) = sum(dx_i/(TAS+w_i)) — a
    harmonic mean of ground speed, not an arithmetic mean of wind.

    Using the arithmetic mean instead understates the baseline's time (Jensen),
    hence understates its fuel, hence OVERSTATES the excess we publish. The bias
    is second order, ~(sigma_w/TAS)^2, but it is systematic and it flatters us,
    which is the worst direction for an error to point.
    """
    w = np.asarray(wpar, float)
    w = w[np.isfinite(w)]
    if w.size == 0:
        return 0.0
    gs = _TAS_REF_MS + w
    # Segments are equal length by construction (_resample_track), so the
    # distance-weighted harmonic mean is the plain harmonic mean of gs.
    if np.any(gs <= 1.0):
        return float(np.mean(w))       # degenerate wind, fall back
    return float(w.size / np.sum(1.0 / gs) - _TAS_REF_MS)


MIN_CRUISE_PTS = 10
CRUISE_ALT_FRAC = 0.85       # "upper part of the flight"
CRUISE_VS_FPM = 300.0        # level enough to count as cruise


def _cruise_state(alt_ft, ias_kt, vs_fpm):
    """Real cruise altitude (ft) and airspeed (kt), or None where unavailable.

    Cruise is the level portion of the upper flight: within 15% of the maximum
    altitude and under 300 fpm. The MEDIAN is used, not the mean, because on a
    step climb the mean sits between two levels at an altitude that was never
    actually flown, while the median gives the level held for most of the time.
    """
    if alt_ft is None or ias_kt is None or vs_fpm is None:
        return None, None
    alt = np.asarray(alt_ft, dtype=np.float64)
    if alt.size == 0:
        return None, None
    mx = np.nanmax(alt)
    if not np.isfinite(mx) or mx <= 0:
        return None, None
    vs = np.nan_to_num(np.asarray(vs_fpm, dtype=np.float64), nan=0.0)
    m = (alt >= CRUISE_ALT_FRAC * mx) & (np.abs(vs) < CRUISE_VS_FPM)
    if int(m.sum()) < MIN_CRUISE_PTS:
        return None, None
    a = float(np.nanmedian(alt[m]))
    ias = np.asarray(ias_kt, dtype=np.float64)[m]
    ias = ias[np.isfinite(ias)]
    if ias.size < MIN_CRUISE_PTS:
        return a, None
    # same IAS->TAS path the real fuel figure uses, so the two are comparable
    return a, _ias_to_tas_ms(float(np.median(ias)), a) / KTS_TO_MS


def decompose_flight(typecode, real_co2_kg, gc_km, flown_km,
                     lat, lon, dep_ts, windfield,
                     load_factor=0.82, reserve_kg=2000.0,
                     alt_ft=None, ias_kt=None, vs_fpm=None) -> dict | None:
    """
    Split one flight's excess into lateral and vertical/speed components.

    `lat`/`lon` are the flight's stored ground track. `real_co2_kg` is the
    already-computed v0 CO2 (IAS-based, wind-correct through duration), so
    the real side is not recomputed here.
    """
    if openap_model(typecode) is None:
        return None
    ac = _get_ac(openap_model(typecode))

    # one altitude for BOTH baselines: isolate distance as the only variable
    cruise_alt = optimal_cruise_alt_ft(typecode, gc_km)
    cruise_tas_kt = _cruise_tas_kt(ac, cruise_alt)

    # --- baseline A: great circle, optimal profile, GC wind ----------------
    wpar_gc = mean_along_track_wind_ms(lat[0], lon[0], lat[-1], lon[-1],
                                       dep_ts, cruise_tas_kt, cruise_alt,
                                       windfield, gc_km)
    nom_gc = _build_profile(typecode, gc_km, cruise_alt, wpar_gc)
    if nom_gc is None:
        return None
    r_gc = estimate_fuel(nom_gc, load_factor=load_factor,
                         reserve_kg=reserve_kg, tas_mode="gs")
    if not r_gc.ok or r_gc.co2_kg <= 0:
        return None

    # --- baseline B: real ground track, same optimal profile, track wind ---
    # duration estimate for the wind time-sampling: the hybrid's own flight time
    dur_s = flown_km / (cruise_tas_kt * 1.852) * 3600.0 if cruise_tas_kt else 3600.0
    wpar_tr = mean_wind_along_track(lat, lon, dep_ts, dur_s, cruise_alt, windfield)
    nom_tr = _build_profile(typecode, flown_km, cruise_alt, wpar_tr)
    if nom_tr is None:
        return None
    r_tr = estimate_fuel(nom_tr, load_factor=load_factor,
                         reserve_kg=reserve_kg, tas_mode="gs")
    if not r_tr.ok or r_tr.co2_kg <= 0:
        return None

    ideal = r_gc.co2_kg
    hybrid = r_tr.co2_kg

    # --- splitting the vertical term into altitude and speed ---------------
    # Two more baselines on the SAME real ground track, each moving one factor
    # from optimal to real:
    #   C = real cruise altitude, optimal speed for that altitude
    #   D = optimal altitude,     real cruise speed
    # Both are measured against the SAME reference (the hybrid), each moving one
    # factor on its own. They are deliberately not chained: in a chain the last
    # step is always synthetic -> real, and that step absorbs everything the
    # baselines do not model — climb and descent shape, step climbs, level-offs,
    # holding, model residual. Chaining therefore makes the split depend on
    # which factor is left to act as the dump (measured: the two orders
    # disagreed by ~5 points on a vertical term of ~11), and it would let a
    # figure called "speed" carry things that are not speed.
    #
    # So we publish two clean single-factor effects plus an explicit remainder:
    #     vertical = altitude + speed + residual
    # The residual is real and often large. It holds the altitude-speed
    # interaction and every vertical inefficiency that is not a cruise level or
    # a cruise Mach. Naming it is the honest alternative to hiding it inside one
    # of the other two.
    v_alt = v_spd = v_resid = float("nan")
    real_alt, real_tas = _cruise_state(alt_ft, ias_kt, vs_fpm)
    if real_alt is not None and real_tas is not None and real_tas > 50.0:
        wpar_c = mean_wind_along_track(lat, lon, dep_ts, dur_s, real_alt, windfield)
        nom_c = _build_profile(typecode, flown_km, real_alt, wpar_c)
        # D carries the real speed to the optimal altitude, and what it carries
        # is the real cruise MACH, not the real TAS. Crews hold a Mach number in
        # cruise, so a TAS taken from FL280 and imposed at FL380 is a speed
        # nobody would fly, and pricing that infeasible profile inflates the
        # speed side of the split.
        mach_real = real_tas * KTS_TO_MS / _sound_speed_ms(real_alt)
        tas_d = mach_real * _sound_speed_ms(cruise_alt) / KTS_TO_MS
        nom_d = _build_profile(typecode, flown_km, cruise_alt, wpar_tr,
                               cruise_tas_kt=tas_d)
        if nom_c is not None and nom_d is not None:
            r_c = estimate_fuel(nom_c, load_factor=load_factor,
                                reserve_kg=reserve_kg, tas_mode="gs")
            r_d = estimate_fuel(nom_d, load_factor=load_factor,
                                reserve_kg=reserve_kg, tas_mode="gs")
            if r_c.ok and r_c.co2_kg > 0 and r_d.ok and r_d.co2_kg > 0:
                vertical = (real_co2_kg - hybrid) / ideal * 100.0
                v_alt = (r_c.co2_kg - hybrid) / ideal * 100.0   # cruise level alone
                v_spd = (r_d.co2_kg - hybrid) / ideal * 100.0   # cruise Mach alone
                v_resid = vertical - v_alt - v_spd

    ratio_er, flown_er = enroute_dist_ratio(lat, lon)
    return {
        "excess_vert_alt_pct": v_alt,
        "excess_vert_speed_pct": v_spd,
        "excess_vert_residual_pct": v_resid,
        "real_cruise_alt_ft": real_alt if real_alt is not None else float("nan"),
        "real_cruise_tas_kt": real_tas if real_tas is not None else float("nan"),
        "ideal_gc_co2_kg": ideal,
        "hybrid_co2_kg": hybrid,
        "excess_total_pct": (real_co2_kg - ideal) / ideal * 100.0,
        "excess_lateral_pct": (hybrid - ideal) / ideal * 100.0,
        "excess_vertical_pct": (real_co2_kg - hybrid) / ideal * 100.0,
        "dist_ratio": flown_km / gc_km if gc_km > 0 else float("nan"),
        "dist_ratio_enroute": ratio_er,
        "flown_enroute_km": flown_er,
        "mean_wpar_gc_ms": wpar_gc,
        "mean_wpar_track_ms": wpar_tr,
        "cruise_alt_ft": cruise_alt,
    }
