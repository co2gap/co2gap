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


@dataclass
class Flight:
    icao: str
    typecode: str
    reg: Optional[str]
    points: list = field(default_factory=list)

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
        out.append((base + dt, lat, lon, alt, gs, ias, vs, flags))
    out.sort(key=lambda r: r[0])
    return out


def split_legs(trace_dict: dict) -> list:
    """Split a day-long trace into legs using readsb 'new leg' flag + time gaps."""
    rows = _raw_points(trace_dict)
    legs, cur = [], []
    prev_t = None
    for (t, lat, lon, alt, gs, ias, vs, flags) in rows:
        new_leg = bool(int(flags or 0) & 2)
        gap = prev_t is not None and (t - prev_t) > LEG_GAP_S
        if cur and (new_leg or gap):
            legs.append(cur)
            cur = []
        cur.append(Point(t, lat, lon, alt, gs, ias, vs))
        prev_t = t
    if cur:
        legs.append(cur)
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
    for leg in split_legs(trace_dict):
        pts = _clean(leg)
        if len(pts) < MIN_POINTS:
            continue
        f = Flight(icao=icao, typecode=typecode, reg=reg, points=pts)
        if is_complete_in_box(f, bbox):
            out.append(f)
    return out


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
