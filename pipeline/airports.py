"""
Nearest-airport resolution for flight endpoints.

Airports come from OurAirports (public domain, CC0), pre-filtered to the
EU-South box (large + medium airports with an ICAO ident) in data/airports.csv.
We resolve the nearest airport to each endpoint within a small radius; endpoints
with no airport nearby (e.g. flights crossing the box edge) keep a null ICAO and
are simply not attributed to an airport in aggregates.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

_MAX_KM = 8.0            # endpoint-to-airport match radius
_COS_LAT0 = math.cos(math.radians(43.0))  # box-centre latitude, for cheap prefilter


class Airports:
    def __init__(self, csv_path: Path):
        self.rows = []  # (icao, lat, lon)
        with open(csv_path, newline="") as f:
            for r in csv.DictReader(f):
                try:
                    self.rows.append((r["icao"], float(r["lat"]), float(r["lon"])))
                except (KeyError, ValueError):
                    continue

    def nearest(self, lat: float, lon: float, max_km: float = _MAX_KM):
        """Return ICAO of nearest airport within max_km, else None."""
        best, bestd = None, max_km
        for icao, alat, alon in self.rows:
            # cheap equirectangular distance is fine at this scale
            dx = (lon - alon) * 111.32 * _COS_LAT0
            dy = (lat - alat) * 110.57
            d = math.hypot(dx, dy)
            if d < bestd:
                best, bestd = icao, d
        return best
