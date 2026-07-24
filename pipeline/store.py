"""
Parquet writer for the durable per-flight dataset.

Two tables per day, written to data/flights/<YYYY-MM-DD>/:
  * points.parquet   one row per stored trajectory point (long format)
  * flights.parquet  one row per flight (metadata, first-pass fuel, quality)

They join on `flight_id` (a per-day surrogate integer). We deliberately store
NO icao / registration / callsign in the durable dataset: downstream needs only
type, route and trajectory, and the published product is always aggregate
(n>=10). Keeping the forever-dataset free of aircraft identifiers is the
cleanest GDPR posture.

Excess columns are intentionally absent here — excess is recomputed on the Mac
with the wind baseline and written to its own table.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

PIPELINE_VER = "phase1-v1"

_POINTS_SCHEMA = pa.schema([
    ("flight_id", pa.int32()),
    ("t", pa.float64()),        # absolute unix seconds (needed for wind time lookup)
    ("lat", pa.float32()),
    ("lon", pa.float32()),
    ("alt_ft", pa.float32()),
    ("gs_kt", pa.float32()),
    ("ias_kt", pa.float32()),   # nullable
    ("vs_fpm", pa.float32()),
])

_FLIGHTS_SCHEMA = pa.schema([
    ("flight_id", pa.int32()),
    ("day", pa.string()),
    ("typecode", pa.string()),
    ("model", pa.string()),
    ("dep_ts", pa.int64()),
    ("arr_ts", pa.int64()),
    ("duration_s", pa.float32()),
    ("o_lat", pa.float32()), ("o_lon", pa.float32()),
    ("d_lat", pa.float32()), ("d_lon", pa.float32()),
    ("origin_icao", pa.string()), ("dest_icao", pa.string()),
    ("gc_km", pa.float32()),
    ("flown_km", pa.float32()),
    ("detour_pct", pa.float32()),
    ("n_pts_native", pa.int32()),
    ("n_pts_stored", pa.int32()),
    ("coverage_frac", pa.float32()),
    ("max_gap_s", pa.float32()),
    ("hole_time_s", pa.float32()),
    ("flown_ge_09gc", pa.bool_()),
    ("max_alt_ft", pa.float32()),
    # first-pass emissions (IAS-based TAS, uncalibrated) — refined on the Mac
    ("fuel_kg_v0", pa.float32()),
    ("co2_kg_v0", pa.float32()),
    ("cruise_ff_kgph_v0", pa.float32()),
    ("init_mass_kg", pa.float32()),
    ("load_factor", pa.float32()),
    ("reserve_kg", pa.float32()),
    ("tas_mode", pa.string()),
    ("pipeline_ver", pa.string()),
])


class DayWriter:
    """Accumulate per-flight rows and write the two parquet files for a day."""

    def __init__(self, out_dir: Path, day_iso: str):
        self.out_dir = Path(out_dir)
        self.day_iso = day_iso
        self._pt_cols = {n: [] for n in _POINTS_SCHEMA.names}
        self._fl_cols = {n: [] for n in _FLIGHTS_SCHEMA.names}
        self._n = 0

    def add(self, meta: dict, points: list) -> None:
        """meta: flight metadata dict; points: thinned list of trajectory Points."""
        fid = self._n
        self._n += 1
        for n in _FLIGHTS_SCHEMA.names:
            if n == "flight_id":
                self._fl_cols[n].append(fid)
            else:
                self._fl_cols[n].append(meta.get(n))
        for p in points:
            self._pt_cols["flight_id"].append(fid)
            self._pt_cols["t"].append(p.t)
            self._pt_cols["lat"].append(p.lat)
            self._pt_cols["lon"].append(p.lon)
            self._pt_cols["alt_ft"].append(p.alt)
            self._pt_cols["gs_kt"].append(p.gs)
            self._pt_cols["ias_kt"].append(p.ias)
            self._pt_cols["vs_fpm"].append(p.vs_rep)

    @property
    def n_flights(self) -> int:
        return self._n

    def flush(self) -> dict:
        d = self.out_dir / self.day_iso
        d.mkdir(parents=True, exist_ok=True)
        pts_tbl = pa.table(
            {n: pa.array(self._pt_cols[n], type=_POINTS_SCHEMA.field(n).type)
             for n in _POINTS_SCHEMA.names},
            schema=_POINTS_SCHEMA)
        fl_tbl = pa.table(
            {n: pa.array(self._fl_cols[n], type=_FLIGHTS_SCHEMA.field(n).type)
             for n in _FLIGHTS_SCHEMA.names},
            schema=_FLIGHTS_SCHEMA)
        pq.write_table(pts_tbl, d / "points.parquet", compression="zstd")
        pq.write_table(fl_tbl, d / "flights.parquet", compression="zstd")
        return {
            "points_rows": pts_tbl.num_rows,
            "flights_rows": fl_tbl.num_rows,
            "points_file": str(d / "points.parquet"),
            "flights_file": str(d / "flights.parquet"),
        }
