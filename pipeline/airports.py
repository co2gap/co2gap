"""
Nearest-airport resolution for flight endpoints.

Airports come from OurAirports (public domain, CC0), pre-filtered to the box
being accumulated (large + medium airports with an ICAO ident) — data/airports.csv
for the original EU-South box, data/airports_ecac.csv for ECAC.
We resolve the nearest airport to each endpoint within a small radius; endpoints
with no airport nearby (e.g. flights crossing the box edge) keep a null ICAO and
are simply not attributed to an airport in aggregates.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

_MAX_KM = 8.0            # endpoint-to-airport match radius

# The east-west scale of a degree of longitude is cos(latitude), and it has to be
# the latitude of the point being matched, not a constant. This was cos(43°) —
# the centre of the original EU-South box, where the error never exceeded ±12%
# — and it survived the move to ECAC, which reaches 72°N. At 60°N cos(43°)/cos(60°)
# is 1.46, so an endpoint 5.5 km east of its airport measured 8.0 and lost its
# ICAO; below 43°N the sign flips and endpoints past 8 km were matched anyway.
#
# Measured on the 197 published days before changing it: 0.28% of origins and
# 0.53% of destinations move, and 229 airports change their movement count
# (Palma −15%, Belfast Aldergrove +16%, Billund +18%). The published RANKING does
# not move — same 152 airports, Spearman 1.0000, top twenty identical, median
# shift 0.000 points and 0.162 at the worst (Lamezia, same rank). So this is
# corrected for the days accumulated from here on, and the published figures are
# left alone: they are frozen until the January 2027 release, and that release
# must re-resolve every endpoint from the stored o_lat/o_lon so the whole window
# is matched one way. The raw dumps are not needed for that — flights.parquet
# keeps the coordinates.


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
        cos_lat = math.cos(math.radians(lat))   # fuori dal ciclo: dipende dal
                                                # punto, non dall'aeroporto
        for icao, alat, alon in self.rows:
            # cheap equirectangular distance is fine at this scale
            dx = (lon - alon) * 111.32 * cos_lat
            dy = (lat - alat) * 110.57
            d = math.hypot(dx, dy)
            if d < bestd:
                best, bestd = icao, d
        return best
