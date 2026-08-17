"""
Fase 3: split the VERTICAL excess by where in the flight it happened.

Why this exists. The figure the site attributes to an airport is the whole
flight's, counted under both of its ends, so a share of what appears under one
airport was produced at the other and a share in cruise, far from either. The
`on dep.`/`on arr.` columns already on the site are a TEST of that (does the
deviation travel with the airport or with its partners?), not an attribution:
they cannot say WHERE in the flight the gap occurred, because the pipeline only
ever held whole-flight totals. This module produces the missing thing.

It splits only the VERTICAL term. The lateral one is already a pure distance
ratio reconciled against EUROCONTROL's KEA and is not in question here.

---------------------------------------------------------------------------
The cut, and why it is what it is
---------------------------------------------------------------------------

The vertical term is (real - hybrid)/ideal, where the hybrid is the optimal
profile flown over the REAL ground track. Splitting it means partitioning two
fuel integrals — the real one and the hybrid one — and the partition has to fall
in the same place on both, or the difference between the parts measures the
misalignment of the buckets instead of inefficiency.

"The same place" must be defined by NORMALISED ARC LENGTH (fraction of the path
already flown), not by a geometric rule. Applying, say, "inside 40 NM of the
airport" to both tracks does NOT give the same cut: a vectored real flight can
cover 120 km while still inside the cylinder, while the hybrid is a straight
segment and leaves it at 74.1 km. Cutting both at the same FRACTION of the path
does, because the two tracks cover the same journey — the hybrid is defined as
the optimal profile over the real flown distance — so fraction-of-path is the
one parameter they genuinely share. Additivity then holds identically:

    real = sum_p real_p,  hybrid = sum_p hybrid_p
    =>  sum_p (real_p - hybrid_p)/ideal  ==  (real - hybrid)/ideal

Two partitions are produced, because the prompt asks two different questions and
one cut cannot answer both. Once the per-step burn arrays exist, a second
bucketing is an array operation, so the second one is nearly free.

  CUT A — BY PHASE (climb / cruise / descent).  Boundaries are the NOMINAL
  profile's own, read off the profile that was actually built (the sign of its
  altitude change per step), expressed as a fraction of its path, and applied to
  the real track at the same fraction.

  The boundaries are taken from the nominal and not from the real flight on
  purpose: they must be EXOGENOUS. A flight held low for 150 km has a distant
  top of climb, and if it set its own boundary its "climb" bucket would grow
  until it swallowed part of the cruise — the measurement would then contain the
  choice of boundary. The nominal's boundaries depend only on typecode, cruise
  altitude and flown distance, all fixed by the frozen baseline.

  This is also the axis the external literature uses, which is what makes the
  result checkable: Pasutto et al. (AIAA 2021) measure CRUISE ONLY (4.6% median
  fuel excess, 200-1500 NM); EUROCONTROL's CCO/CDO figures are climb and descent
  (~4.3 kg per departure against ~35 kg per arrival); Alcabin et al. (AIAA 2009)
  put 80% of the US vertical excess in descent/arrival. Our whole-profile 13.1%
  on Pasutto's perimeter is not comparable to their 4.6%; a cruise-only column
  is.

  CUT B — BY POSITION (departure TMA / en route / arrival TMA), on the 40 NM
  cylinder already used for the lateral part. The fraction is taken where the
  REAL track leaves the departure cylinder and re-enters the arrival one, and
  the same fraction is applied to the hybrid. This is the cut homogeneous with
  KEA and with the lateral component, and it is the honest answer to "how much
  of it was burnt near this airport" — which is what the airport table needs.

Alternatives considered and rejected, since the choice is not obvious:

  * A cut by ALTITUDE (below FL100, say). On the nominal that is already a
    distance cut, so it adds nothing; on the real track it is endogenous, since
    where FL100 falls depends on how steeply the aircraft climbed. And FL100
    sits around 50 km out, close enough to the cylinder to be a worse version of
    cut B.

  * Measured TOC/TOD on the real profile as the boundary. Rejected for the
    endogeneity above. Kept as a DIAGNOSTIC (`real_toc_frac`/`real_tod_frac`):
    the gap between the real top of climb and the nominal's is itself evidence
    of a constrained climb, and it costs nothing.

  * The vertical-speed classifier already in `estimate_fuel.phase_fuel`
    (vs > 350 climb, < -350 descent). It looks free because it already runs, and
    it is still wrong for this: it labels each POINT independently, so a level
    segment inside the climb is called "cruise" and a step climb in cruise is
    called "climb". The buckets then hold different amounts of path on the real
    track (varying rates) than on the nominal (a clean 2000 fpm ramp) — exactly
    the failure this module is built to avoid. Measured on a sample day it also
    puts 9.1% of the burn under "ground", which is an artefact of the threshold
    and not a phase.

  * The 40 NM cylinder as the ONLY cut. Measured on a sample day: the nominal's
    climb ends at a median of 193.8 km and its descent begins 215.3 km before
    the end, against a cylinder radius of 74.1 km, and on 0.0% of flights does
    the nominal climb finish inside the cylinder. A single 40 NM cut would push
    nearly all of the climb inefficiency into "en route", which is the opposite
    of what an airport attribution needs. Hence it is cut B and not the only cut.

---------------------------------------------------------------------------
The stored track is THINNED, and that has to be handled, not ignored
---------------------------------------------------------------------------

`co2_kg_v0` — the frozen real figure — was integrated by the daily pipeline over
the NATIVE trace. What is kept on disk is the THINNED track (>= 10 s spacing;
`flightproc._thin` stores a subset and never interpolates), with a measured
ratio of 2.15 native points per stored point. Re-integrating the real flight
from the stored track therefore does NOT reproduce `co2_kg_v0`: measured over a
sample day it comes out 0.31% low in aggregate (median -0.25%, -0.61% under
300 km falling to -0.11% over 2000 km — coarser steps lose most where the fuel
flow changes fastest, which is climb and descent, which is why short flights
lose most).

Left uncorrected that would move the published vertical term from 14.55 to about
14.17 — a third of a point off a frozen figure that has been sent to four
organisations. So the thinned track supplies only the SHARES and the frozen
figure supplies the level:

    real_p = co2_kg_v0 * burn_p / sum(burn)

sum_p real_p is then exactly `co2_kg_v0`, the phases close exactly on the
published `excess_vertical_pct`, and no published number moves.

The assumption this makes, which must be declared rather than buried: that the
thinning bias is spread across the phases in proportion to their burn. It is not
exactly — it concentrates where the vertical rate changes fastest. The
uncorrected total is therefore kept per flight (`real_co2_thin_kg`) so the bias
can be measured rather than assumed, and a re-thinning sensitivity test at 20 s
and 40 s bounds how it splits between the phases.
"""

from __future__ import annotations

import numpy as np

from emissions import estimate_fuel, openap_model
from excess_wind import _build_profile
from trajectories import Flight, Point

EARTH_R_KM = 6371.0088
TMA_RADIUS_KM = 40.0 * 1.852        # same cylinder as the lateral/KEA metric

# "level enough and high enough to be the cruise" — used only for the real
# TOC/TOD diagnostic, never as a bucket boundary.
TOC_ALT_FRAC = 0.95


def _haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = p2 - p1
    dlmb = np.radians(np.asarray(lon2) - np.asarray(lon1))
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2) ** 2
    return 2 * EARTH_R_KM * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def _arc_profile(burn_kg, dist_km):
    """(u_start, u_end, burn) per step, with `u` the arc length normalised to
    [0, 1]: the fraction of the path already flown.

    `u` is the coordinate the real track and the nominal genuinely share — the
    hybrid is by definition the optimal profile over the real flown distance —
    so it is the coordinate the cut is expressed in. Returns None when the track
    has no length to speak of.
    """
    d = np.asarray(dist_km, float)
    b = np.asarray(burn_kg, float)
    total = float(d.sum())
    if not np.isfinite(total) or total <= 0:
        return None
    edges = np.concatenate([[0.0], np.cumsum(d)]) / total
    return edges[:-1], edges[1:], b


def _burn_between(prof, ua, ub, last=False) -> float:
    """Burn over the path fraction [ua, ub), and [ua, ub] for the final bucket.

    A step is split in proportion to how much of it falls inside the interval,
    so a boundary landing mid-step divides that step instead of rounding it into
    one side — which would push quantisation noise straight into the smallest
    bucket.

    ZERO-LENGTH STEPS are handled separately and that is not a detail. A stopped
    aircraft — on the ground at either end, or a repeated position — still burns
    fuel in the model while covering no distance, so it has a `u` but no width,
    and any scheme based on interpolating cumulative burn against `u` drops it:
    the burn sits on a plateau where the interpolation cannot see it. Measured on
    a sample day this hit 0.27% of flights and lost up to 63 kg on one of them,
    which is exactly how much the additivity check was short. Such a step is
    assigned whole to the bucket its position falls in, which is also the
    physically right answer: fuel burnt sitting at the departure end belongs to
    the departure end.
    """
    u0, u1, b = prof
    w = u1 - u0
    wide = w > 0
    lo = np.maximum(u0, ua)
    hi = np.minimum(u1, ub)
    frac = np.zeros_like(b)
    np.divide(np.clip(hi - lo, 0.0, None), w, out=frac, where=wide)
    np.clip(frac, 0.0, 1.0, out=frac)
    # point-like steps: the bucket that contains them, half-open except at the
    # very end so that [0,1] is tiled exactly once
    pt = ~wide
    if pt.any():
        inside = (u0 >= ua) & ((u0 < ub) | (last & (u0 <= ub)))
        frac[pt] = inside[pt].astype(float)
    return float(np.sum(b * frac))


def _nominal_phase_bounds(vs_step, dist_step):
    """Path fractions at which the NOMINAL leaves the climb and enters the
    descent, read off the profile that was actually built.

    Read from the built profile rather than recomputed from CLIMB_RATE_FPM and
    the cruise TAS: duplicating that arithmetic here would be a second copy of
    `_build_profile`'s internals, free to drift away from it in silence.
    """
    vs = np.asarray(vs_step, float)
    d = np.asarray(dist_step, float)
    total = float(d.sum())
    if total <= 0:
        return float("nan"), float("nan")
    cum = np.cumsum(d) / total          # fraction at the END of each step
    climbing = vs > 1.0
    descending = vs < -1.0
    u1 = float(cum[climbing][-1]) if climbing.any() else 0.0
    # start of the descent = fraction at the BEGINNING of its first step
    if descending.any():
        i = int(np.flatnonzero(descending)[0])
        u2 = float(cum[i - 1]) if i > 0 else 0.0
    else:
        u2 = 1.0
    # A sector too short for the optimal profile to hold any cruise can leave
    # the climb ending after the descent begins. Collapse the cruise bucket to
    # nothing rather than let it go negative and steal from its neighbours.
    if u2 < u1:
        u1 = u2 = 0.5 * (u1 + u2)
    return u1, u2


def _point_fractions(lat, lon, valid_step):
    """Path fraction of every POINT, on the same axis as the burn integral.

    The integral runs over the steps that survived the filter in
    `_steps_from_flight`, so a position measured over ALL segments would sit on
    a different axis and the cut would land somewhere else on the real track
    than on the nominal. Dropped segments contribute zero length here, which is
    exactly how they contribute to the integral.
    """
    lat = np.asarray(lat, float)
    lon = np.asarray(lon, float)
    seg = _haversine_km(lat[:-1], lon[:-1], lat[1:], lon[1:])
    v = np.asarray(valid_step, bool)
    n = min(seg.size, v.size)
    kept = np.where(v[:n], seg[:n], 0.0)
    total = float(kept.sum())
    if total <= 0:
        return None
    # length n+1, one per point, monotone non-decreasing, 0 .. 1
    return np.concatenate([[0.0], np.cumsum(kept)]) / total


def _tma_bounds(lat, lon, u_pt):
    """Path fractions at which the REAL track leaves the departure cylinder and
    re-enters the arrival one. (nan, nan) when the two cylinders overlap, i.e.
    the flight is too short to have an en-route phase at all — the same honest
    answer `enroute_dist_ratio` already gives for the lateral metric.
    """
    lat = np.asarray(lat, float)
    lon = np.asarray(lon, float)
    d_o = _haversine_km(lat[0], lon[0], lat, lon)
    d_d = _haversine_km(lat[-1], lon[-1], lat, lon)
    outside = (d_o > TMA_RADIUS_KM) & (d_d > TMA_RADIUS_KM)
    idx = np.flatnonzero(outside)
    if idx.size < 3:
        return float("nan"), float("nan")
    n = u_pt.size
    i0, i1 = min(int(idx[0]), n - 1), min(int(idx[-1]), n - 1)
    return float(u_pt[i0]), float(u_pt[i1])


def _real_toc_tod(alt_ft, u_pt):
    """Path fractions of the real top of climb and top of descent. Diagnostic
    only: never a bucket boundary (see the module docstring)."""
    alt = np.asarray(alt_ft, float)
    n = min(alt.size, u_pt.size)
    alt, u = alt[:n], u_pt[:n]
    mx = np.nanmax(alt) if n else float("nan")
    if not np.isfinite(mx) or mx <= 0:
        return float("nan"), float("nan")
    hi = np.flatnonzero(alt >= TOC_ALT_FRAC * mx)
    if hi.size == 0:
        return float("nan"), float("nan")
    return float(u[hi[0]]), float(u[hi[-1]])


def _flight_from_arrays(typecode, t, lat, lon, alt_ft, gs_kt, ias_kt, vs_fpm):
    pts = [Point(t=float(a), lat=float(b), lon=float(c),
                 alt=(float(e) if np.isfinite(e) else None),
                 gs=(float(f) if np.isfinite(f) else None),
                 ias=(float(g) if np.isfinite(g) else None),
                 vs_rep=(float(h) if np.isfinite(h) else None))
           for a, b, c, e, f, g, h in zip(t, lat, lon, alt_ft, gs_kt,
                                          ias_kt, vs_fpm)]
    return Flight(icao="REAL", typecode=typecode, reg=None, points=pts)


_NAN6 = {k: float("nan") for k in (
    "excess_vert_climb_pct", "excess_vert_cruise_pct", "excess_vert_desc_pct",
    "excess_vert_dep_pct", "excess_vert_enr_pct", "excess_vert_arr_pct")}


def phase_split_flight(typecode, co2_kg_v0, ideal_gc_co2_kg, hybrid_co2_kg,
                       flown_km, cruise_alt_ft, mean_wpar_track_ms,
                       t, lat, lon, alt_ft, gs_kt, ias_kt, vs_fpm,
                       load_factor=0.82, reserve_kg=2000.0) -> dict | None:
    """Split one flight's vertical excess by phase and by position.

    Everything the nominal needs comes from the FROZEN decomposition row
    (`flown_km`, `cruise_alt_ft`, `mean_wpar_track_ms`), so the hybrid is
    rebuilt without ERA5 and without redoing the great-circle baseline — and
    reproducing `hybrid_co2_kg` exactly is then the gate proving we are
    re-splitting the same flight and not a differently-parameterised one.
    """
    if openap_model(typecode) is None or not (ideal_gc_co2_kg > 0):
        return None

    # ---- the nominal, rebuilt from the frozen row ------------------------
    nom = _build_profile(typecode, float(flown_km), float(cruise_alt_ft),
                         float(mean_wpar_track_ms))
    if nom is None:
        return None
    r_nom = estimate_fuel(nom, load_factor=load_factor, reserve_kg=reserve_kg,
                          tas_mode="gs", with_steps=True)
    if not r_nom.ok or r_nom.co2_kg <= 0:
        return None
    prof_nom = _arc_profile(r_nom.burn_kg_step, r_nom.dist_km_step)
    if prof_nom is None:
        return None

    # ---- the real flight, re-integrated from the stored (thinned) track ---
    real = _flight_from_arrays(typecode, t, lat, lon, alt_ft, gs_kt,
                               ias_kt, vs_fpm)
    r_real = estimate_fuel(real, load_factor=load_factor, reserve_kg=reserve_kg,
                           tas_mode="ias", with_steps=True)
    if not r_real.ok or r_real.co2_kg <= 0:
        return None
    prof_real = _arc_profile(r_real.burn_kg_step, r_real.dist_km_step)
    if prof_real is None:
        return None

    u_pt = _point_fractions(lat, lon, r_real.valid_step)
    if u_pt is None:
        return None

    ideal = float(ideal_gc_co2_kg)
    real_total_thin = float(r_real.co2_kg)
    # The buckets hold FUEL; these turn fuel into CO2. On the real side the
    # thinned track sets only the SHARES and the frozen figure sets the level —
    # see the module docstring, using the re-integrated total instead would move
    # a published number by a third of a point. On the nominal side there is
    # nothing to reconcile against, so the factor is just CO2 per kg of fuel.
    k_real = float(co2_kg_v0) / float(r_real.fuel_kg)
    k_nom = float(r_nom.co2_kg) / float(r_nom.fuel_kg)

    def split(bounds):
        """Excess percentage points for the buckets delimited by `bounds`.

        The last bucket closes at +inf rather than at 1.0. Normalising the arc
        length leaves the final edge a couple of ULPs either side of 1.0
        (cumsum and sum do not agree to the last bit), and a bucket that stopped
        exactly at 1.0 would drop whatever sits beyond it — silently, and only
        on some flights.
        """
        out, prev = [], 0.0
        edges = list(bounds) + [np.inf]
        for i, ub in enumerate(edges):
            last = (i == len(edges) - 1)
            real_p = _burn_between(prof_real, prev, ub, last) * k_real
            nom_p = _burn_between(prof_nom, prev, ub, last) * k_nom
            out.append((real_p - nom_p) / ideal * 100.0)
            prev = ub
        return out

    # ---- cut A: by phase, boundaries from the nominal --------------------
    u1, u2 = _nominal_phase_bounds(r_nom.vs_fpm_step, r_nom.dist_km_step)
    if np.isfinite(u1) and np.isfinite(u2):
        v_climb, v_cruise, v_desc = split([u1, u2])
    else:
        v_climb = v_cruise = v_desc = float("nan")

    # ---- cut B: by position, the 40 NM cylinder on the real track --------
    b1, b2 = _tma_bounds(lat, lon, u_pt)
    if np.isfinite(b1) and np.isfinite(b2):
        v_dep, v_enr, v_arr = split([b1, b2])
    else:
        v_dep = v_enr = v_arr = float("nan")

    toc, tod = _real_toc_tod(alt_ft, u_pt)

    # Additivity is exact by construction, so this is not a tolerance to tune
    # but a tripwire: anything but ~0 means the buckets stopped partitioning.
    vertical = (float(co2_kg_v0) - float(hybrid_co2_kg)) / ideal * 100.0
    resid = vertical - (v_climb + v_cruise + v_desc)

    return {
        "excess_vert_climb_pct": v_climb,
        "excess_vert_cruise_pct": v_cruise,
        "excess_vert_desc_pct": v_desc,
        "excess_vert_dep_pct": v_dep,
        "excess_vert_enr_pct": v_enr,
        "excess_vert_arr_pct": v_arr,
        "nom_climb_frac": u1,
        "nom_desc_frac": u2,
        "tma_dep_frac": b1,
        "tma_arr_frac": b2,
        "real_toc_frac": toc,
        "real_tod_frac": tod,
        "real_co2_thin_kg": real_total_thin,
        "hybrid_co2_rebuilt_kg": float(r_nom.co2_kg),
        "resid_add_pct": resid,
    }
