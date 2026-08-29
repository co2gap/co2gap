"""
Turn a cleaned Flight into the *durable* per-flight record: a thinned
trajectory plus honest coverage-quality metrics.

Design intent (phase 1): the daily cron on the Pi parses the global dump (the
expensive step, ~global cost) and writes per-flight parquet that we keep
forever. Excess CO2 is recomputed later on the Mac when the wind baseline
improves — from these stored trajectories, without re-parsing. So this module
must store *enough* trajectory (position/altitude/time at usable resolution)
and *enough* quality signal to reject bad flights downstream, and nothing that
identifies a person.

Two deliberate choices:
  * We THIN, we never INTERPOLATE. Stored points are a subset of the real ADS-B
    points (drop points closer than MIN_SPACING_S to the last kept one), so we
    never invent positions. Coverage holes stay holes and are measured, not
    filled.
  * Flown distance and coverage are computed on the FULL cleaned path (before
    thinning), so thinning never changes the quality numbers.
"""

from __future__ import annotations

from trajectories import Flight, haversine_km

# thin the stored trajectory to at most ~1 point / MIN_SPACING_S seconds.
# 10 s at cruise is ~1.3 km spacing — plenty for wind lookup (0.25 deg grid)
# and fuel integration, while bounding parquet size for a forever dataset.
MIN_SPACING_S = 10.0

# a native sampling gap longer than this is a real coverage hole (aircraft out
# of receiver range), not just sparse sampling.
# Unica definizione: lab/gate.py, che la pubblica anche in metodologia. Qui
# resta un valore letterale perche' pipeline/ gira sul Pi senza lab/ nel path —
# ma se cambia va cambiato la' e riportato qui, e i due devono restare uguali.
GAP_THRESHOLD_S = 120.0     # == lab.gate.GAP_THRESHOLD_S


def _flown_km(points) -> float:
    tot = 0.0
    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        tot += haversine_km(a.lat, a.lon, b.lat, b.lon)
    return tot


def _thin(points):
    """Keep first and last; drop points within MIN_SPACING_S of the last kept."""
    if len(points) <= 2:
        return list(points)
    kept = [points[0]]
    for p in points[1:-1]:
        if p.t - kept[-1].t >= MIN_SPACING_S:
            kept.append(p)
    kept.append(points[-1])
    return kept


def process_flight(flight: Flight, gc_km: float) -> tuple[list, dict]:
    """Return (thinned_points, quality_dict) for one complete flight."""
    pts = flight.points
    duration_s = pts[-1].t - pts[0].t

    gaps = [pts[i + 1].t - pts[i].t for i in range(len(pts) - 1)]
    max_gap_s = max(gaps) if gaps else 0.0
    hole_time_s = sum(g for g in gaps if g > GAP_THRESHOLD_S)
    coverage_frac = (1.0 - hole_time_s / duration_s) if duration_s > 0 else 0.0

    flown_km = _flown_km(pts)
    detour_pct = (flown_km - gc_km) / gc_km * 100.0 if gc_km > 0 else None
    # the phase-0 data-quality gate: enough of the great-circle path was actually
    # observed. Holes make flown *under*-count, so flown >= 0.9*GC is the guard.
    flown_ge_09gc = gc_km > 0 and flown_km >= 0.9 * gc_km

    kept = _thin(pts)

    quality = {
        "duration_s": duration_s,
        "gc_km": gc_km,
        "flown_km": flown_km,
        "detour_pct": detour_pct,
        "n_pts_native": len(pts),
        "n_pts_stored": len(kept),
        "coverage_frac": coverage_frac,
        "max_gap_s": max_gap_s,
        "hole_time_s": hole_time_s,
        "flown_ge_09gc": flown_ge_09gc,
    }
    return kept, quality
