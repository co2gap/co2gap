"""
Turn a raw readsb trace into clean, per-flight trajectories.

A single aircraft trace covers a whole UTC day and can contain several flights
(legs). readsb already marks leg boundaries with flag bit 1 (value & 2 ==
"start of new leg", i.e. a landing/takeoff separation); we use that, plus a
time-gap fallback, to split legs. Then we clean each leg and decide whether it
is a *complete* flight inside the region of interest (takeoff and landing both
visible in the bounding box) — those are the ones we can honestly model
end to end.

Everything here is aggregate/flight-level. No per-person anything.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from source import BBox

# ---- tunables (documented; a phase-0 wants these visible, not hidden) ----
LEG_GAP_S = 30 * 60          # >30 min with no data -> treat as a new leg
GROUND_ALT_FT = 3000.0       # endpoint must be at/below this to count as on/near ground
MIN_CRUISE_ALT_FT = 15000.0  # must actually climb to real altitude (not a circuit)
MIN_DURATION_S = 20 * 60     # at least 20 min airborne
MAX_DURATION_S = 6 * 3600
MIN_POINTS = 30
MAX_GROUNDSPEED_KT = 700.0   # airliner sanity ceiling for outlier rejection
MAX_IMPLIED_SPEED_KT = 1400.0  # consecutive-point teleport rejection


_ALPHA = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def icao_operator(callsign: Optional[str]) -> Optional[str]:
    """ICAO airline designator from a callsign, or None if it isn't one.

    We keep the *operator*, not the registration, because for this project they
    are not interchangeable: the registration says whose asset the airframe is,
    while the callsign says who actually operated that flight. Under wet lease
    and ACMI the two differ, and an efficiency metric is about the operator.

    A commercial callsign is three letters (the designator) followed by a flight
    identifier that begins with a digit: RYR1234, DLH2AB. An aircraft with no
    operator broadcasts its registration instead — DEABC, GABCD, N12345 — so
    requiring the fourth character to be a digit rejects every registration
    format while accepting the ICAO flight-identifier grammar.

    Deliberately fails closed: anything it cannot parse returns None rather than
    a plausible-looking wrong airline. A missing operator is recoverable, a
    silently wrong one contaminates every aggregate built on top of it.
    """
    if not callsign:
        return None
    cs = callsign.strip().upper()
    if len(cs) < 4 or not cs[3].isdigit():
        return None
    if any(c not in _ALPHA for c in cs[:3]):
        return None
    return cs[:3]


def _dominant(counter: Counter) -> Optional[str]:
    """Most frequent callsign of a leg.

    Not the first one: the field can still carry the previous flight's value
    across a leg boundary, and readsb sometimes emits a partial callsign while
    the identity message is still being assembled.
    """
    return counter.most_common(1)[0][0] if counter else None


def _alt_to_ft(a) -> Optional[float]:
    if a == "ground":
        return 0.0
    if a is None:
        return None
    try:
        return float(a)
    except (TypeError, ValueError):
        return None


@dataclass
class Point:
    t: float          # absolute unix seconds
    lat: float
    lon: float
    alt: Optional[float]   # ft (baro), 0 == ground
    gs: Optional[float]    # knots
    ias: Optional[float]   # knots (trace index 12), often present
    vs_rep: Optional[float]  # reported vertical rate fpm (index 7)
    mcp: Optional[float] = None  # autopilot selected altitude ft, Mode S, ~57% of aircraft


@dataclass
class Flight:
    icao: str
    typecode: str
    reg: Optional[str]
    points: list = field(default_factory=list)
    operator: Optional[str] = None   # ICAO airline designator, None if not a commercial callsign

    @property
    def t_start(self) -> float:
        return self.points[0].t

    @property
    def t_end(self) -> float:
        return self.points[-1].t

    @property
    def duration_s(self) -> float:
        return self.points[-1].t - self.points[0].t

    @property
    def max_alt(self) -> float:
        return max((p.alt for p in self.points if p.alt is not None), default=0.0)


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _raw_points(trace_dict: dict) -> list:
    base = float(trace_dict["timestamp"])
    out = []
    for row in trace_dict["trace"]:
        dt = row[0]
        lat, lon = row[1], row[2]
        if lat is None or lon is None:
            continue
        alt = _alt_to_ft(row[3])
        gs = row[4]
        vs = row[7] if len(row) > 7 else None
        ias = row[12] if len(row) > 12 else None
        flags = row[6] if len(row) > 6 else 0
        # index 8 is readsb's Mode S object, present on ~25% of points; the rest
        # of it (selected altitude, TAS, on-board wind) is not read here yet.
        det = row[8] if len(row) > 8 and isinstance(row[8], dict) else None
        cs = det.get("flight") if det else None
        mcp = det.get("nav_altitude_mcp") if det else None
        out.append((base + dt, lat, lon, alt, gs, ias, vs, flags, cs, mcp))
    out.sort(key=lambda r: r[0])
    return out


def split_legs(trace_dict: dict) -> list:
    """Split a day-long trace into legs using readsb 'new leg' flag + time gaps.

    Returns (points, callsign) per leg. The callsign is collected per leg and
    not per aircraft: one airframe flies several sectors a day under different
    flight numbers, and under wet lease possibly for different operators.
    """
    rows = _raw_points(trace_dict)
    legs, cur, cur_cs = [], [], Counter()
    prev_t = None
    for (t, lat, lon, alt, gs, ias, vs, flags, cs, mcp) in rows:
        new_leg = bool(int(flags or 0) & 2)
        gap = prev_t is not None and (t - prev_t) > LEG_GAP_S
        if cur and (new_leg or gap):
            legs.append((cur, _dominant(cur_cs)))
            cur, cur_cs = [], Counter()
        cur.append(Point(t, lat, lon, alt, gs, ias, vs, mcp))
        if cs:
            cur_cs[cs.strip()] += 1
        prev_t = t
    if cur:
        legs.append((cur, _dominant(cur_cs)))
    return legs


def _clean(points: list) -> list:
    """Drop dup timestamps and teleport/outlier points."""
    cleaned = []
    for p in points:
        if cleaned:
            last = cleaned[-1]
            dt = p.t - last.t
            if dt <= 0:
                continue  # duplicate / out-of-order
            d_km = haversine_km(last.lat, last.lon, p.lat, p.lon)
            implied_kt = (d_km / 1.852) / (dt / 3600.0) if dt > 0 else 0.0
            if implied_kt > MAX_IMPLIED_SPEED_KT:
                continue  # position jump, drop
        if p.gs is not None and p.gs > MAX_GROUNDSPEED_KT:
            p.gs = None
        cleaned.append(p)
    return cleaned


def flights_from_trace(trace_dict: dict, bbox: BBox) -> list:
    """Yield complete, in-box flights for one aircraft trace."""
    typecode = (trace_dict.get("t") or "").upper()
    icao = trace_dict.get("icao", "")
    reg = trace_dict.get("r")
    out = []
    for leg, callsign in split_legs(trace_dict):
        pts = _clean(leg)
        if len(pts) < MIN_POINTS:
            continue
        f = Flight(icao=icao, typecode=typecode, reg=reg, points=pts,
                   operator=icao_operator(callsign))
        if is_complete_in_box(f, bbox):
            out.append(f)
    return out


def mcp_summary(points) -> dict:
    """Autopilot selected altitude, summarised per flight.

    `nav_altitude_mcp` is the level dialled into the autopilot, which in normal
    operation is the level ATC has cleared. It matters here because the vertical
    part of the excess turns out to live in climb and descent rather than in the
    cruise level, and this is the only field in the data that says what the
    aircraft was CLEARED to rather than what it did.

    Deliberately reported as counts and levels, not as an attribution. Reading a
    gap between selected and actual altitude as "held down by ATC" needs
    validation this does not attempt: during a normal climb the selected level is
    above the current one for perfectly ordinary reasons.
    """
    v = [p for p in points if p.mcp is not None and p.mcp > 0]
    if not v:
        return {"mcp_n_pts": 0, "mcp_n_levels": 0,
                "mcp_first_ft": None, "mcp_max_ft": None}
    lv = [round(p.mcp / 100.0) * 100.0 for p in v]
    # Distinct levels are counted only where the aircraft is LEVEL. Counting them
    # over the whole flight gives a median of 13 and a max of 24, which is not a
    # count of clearances at all: in climb and descent the crew winds the
    # selector and every intermediate value is captured. Restricted to level
    # flight the figure means what it should: the distinct levels actually held.
    # Measured median 4, max 10. Note this counts level-offs in DESCENT too, not
    # only cruise step climbs — which is a feature here, since descent level-offs
    # are precisely the CDO inefficiency EUROCONTROL quantifies.
    held = {round(p.mcp / 100.0) * 100.0 for p in v
            if p.vs_rep is not None and abs(p.vs_rep) < 300.0}
    return {"mcp_n_pts": len(v),
            "mcp_n_levels": len(held),
            "mcp_first_ft": lv[0],                  # first level seen: initial clearance
            "mcp_max_ft": max(lv)}


def is_complete_in_box(f: Flight, bbox: BBox) -> bool:
    p0, p1 = f.points[0], f.points[-1]
    if not (bbox.contains(p0.lat, p0.lon) and bbox.contains(p1.lat, p1.lon)):
        return False
    a0 = p0.alt if p0.alt is not None else 99999
    a1 = p1.alt if p1.alt is not None else 99999
    if a0 > GROUND_ALT_FT or a1 > GROUND_ALT_FT:
        return False
    if f.max_alt < MIN_CRUISE_ALT_FT:
        return False
    if not (MIN_DURATION_S <= f.duration_s <= MAX_DURATION_S):
        return False
    return True
