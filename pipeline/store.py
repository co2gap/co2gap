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

import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

PIPELINE_VER = "phase1-v2-contracts"
SOURCE_CONTRACT_KEY = b"co2gap.source-contract"
SOURCE_CONTRACT_VERSION = 1


def _schema_checksum(schema: pa.Schema) -> str:
    fields = [(f.name, str(f.type), f.nullable) for f in schema]
    return hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()


def _flight_keyset_checksum(day: str, ids) -> str:
    h = hashlib.sha256()
    for fid in sorted(int(x) for x in ids):
        h.update(f"{day}\0{fid}\n".encode())
    return h.hexdigest()

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

# WARNING: this schema is a gate, not a description. `add()` iterates over
# _FLIGHTS_SCHEMA.names and takes meta.get(n), so any key present in the meta
# dict but ABSENT here is dropped with no error and no warning. Adding a field
# to run_daily.py is not enough — it must be declared here too. Cost of learning
# this the hard way: operator and MCP were wired up on 29 July, ran nightly for
# two weeks, and wrote nothing.
_FLIGHTS_SCHEMA = pa.schema([
    ("flight_id", pa.int32()),
    ("day", pa.string()),
    ("typecode", pa.string()),
    ("operator", pa.string()),      # ICAO airline designator from callsign, nullable
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
    # autopilot selected altitude (Mode S), ~99% of flights that pass the pipeline
    ("mcp_n_pts", pa.int32()),
    ("mcp_n_levels", pa.int32()),   # distinct levels held in LEVEL flight
    ("mcp_first_ft", pa.float32()),
    ("mcp_max_ft", pa.float32()),
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
    """
    Accumulate per-flight rows and stream them to two parquet files.

    Rows are flushed as parquet row groups every FLUSH_EVERY flights and the
    in-memory buffers cleared, so peak RAM stays flat regardless of how many
    flights a day holds (the whole-EU day would otherwise grow the buffers to
    ~1 GB+). flight_id is a monotonic counter, so points keep their link across
    row groups.
    """

    FLUSH_EVERY = 1500

    def __init__(self, out_dir: Path, day_iso: str, *, source=None, configuration=None):
        self.out_dir = Path(out_dir)
        self.day_iso = day_iso
        self._d = self.out_dir / day_iso
        self._d.mkdir(parents=True, exist_ok=True)
        self._pt_cols = {n: [] for n in _POINTS_SCHEMA.names}
        self._fl_cols = {n: [] for n in _FLIGHTS_SCHEMA.names}
        self._n = 0
        self._pt_rows = 0
        self._since = 0
        self._pw = None
        self._fw = None
        self.source = source or {}
        self.configuration = configuration or {}

    def add(self, meta: dict, points: list) -> None:
        fid = self._n
        self._n += 1
        for n in _FLIGHTS_SCHEMA.names:
            self._fl_cols[n].append(fid if n == "flight_id" else meta.get(n))
        for p in points:
            self._pt_cols["flight_id"].append(fid)
            self._pt_cols["t"].append(p.t)
            self._pt_cols["lat"].append(p.lat)
            self._pt_cols["lon"].append(p.lon)
            self._pt_cols["alt_ft"].append(p.alt)
            self._pt_cols["gs_kt"].append(p.gs)
            self._pt_cols["ias_kt"].append(p.ias)
            self._pt_cols["vs_fpm"].append(p.vs_rep)
        self._since += 1
        if self._since >= self.FLUSH_EVERY:
            self._write_rowgroup()

    def _write_rowgroup(self) -> None:
        if not self._fl_cols["flight_id"]:
            return
        pts_tbl = pa.table(
            {n: pa.array(self._pt_cols[n], type=_POINTS_SCHEMA.field(n).type)
             for n in _POINTS_SCHEMA.names}, schema=_POINTS_SCHEMA)
        fl_tbl = pa.table(
            {n: pa.array(self._fl_cols[n], type=_FLIGHTS_SCHEMA.field(n).type)
             for n in _FLIGHTS_SCHEMA.names}, schema=_FLIGHTS_SCHEMA)
        if self._pw is None:
            self._pw = pq.ParquetWriter(self._d / "points.parquet",
                                        _POINTS_SCHEMA, compression="zstd")
            self._fw = pq.ParquetWriter(self._d / "flights.parquet",
                                        _FLIGHTS_SCHEMA, compression="zstd")
        self._pw.write_table(pts_tbl)
        self._fw.write_table(fl_tbl)
        self._pt_rows += pts_tbl.num_rows
        for n in self._pt_cols:
            self._pt_cols[n].clear()
        for n in self._fl_cols:
            self._fl_cols[n].clear()
        self._since = 0

    @property
    def n_flights(self) -> int:
        return self._n

    def flush(self) -> dict:
        self._write_rowgroup()
        if self._pw is not None:
            key_hash = _flight_keyset_checksum(self.day_iso, range(self._n))
            common = {
                "contract_version": SOURCE_CONTRACT_VERSION,
                "day": self.day_iso,
                "pipeline_version": PIPELINE_VER,
                "flights_rows": self._n,
                "points_rows": self._pt_rows,
                "flight_keyset_sha256": key_hash,
                "source": self.source,
                "configuration": self.configuration,
            }
            flights_contract = {
                **common, "table": "flights", "rows": self._n,
                "schema_sha256": _schema_checksum(_FLIGHTS_SCHEMA),
            }
            points_contract = {
                **common, "table": "points", "rows": self._pt_rows,
                "schema_sha256": _schema_checksum(_POINTS_SCHEMA),
            }
            self._fw.add_key_value_metadata({
                SOURCE_CONTRACT_KEY.decode(): json.dumps(
                    flights_contract, sort_keys=True, separators=(",", ":"))})
            self._pw.add_key_value_metadata({
                SOURCE_CONTRACT_KEY.decode(): json.dumps(
                    points_contract, sort_keys=True, separators=(",", ":"))})
            self._pw.close()
            self._fw.close()
        return {
            "points_rows": self._pt_rows,
            "flights_rows": self._n,
            "points_file": str(self._d / "points.parquet"),
            "flights_file": str(self._d / "flights.parquet"),
        }


def validate_day_pair(day_dir: Path) -> dict:
    """Validate the two durable source tables as one indivisible day."""
    day_dir = Path(day_dir)
    contracts = {}
    metadata = {}
    for table in ("flights", "points"):
        path = day_dir / f"{table}.parquet"
        md = pq.read_metadata(path)
        raw = (md.metadata or {}).get(SOURCE_CONTRACT_KEY)
        if raw is None:
            raise ValueError(f"{path} has no source contract")
        contract = json.loads(raw)
        if contract.get("contract_version") != SOURCE_CONTRACT_VERSION:
            raise ValueError(f"{path} has unsupported source contract")
        if contract.get("table") != table or contract.get("day") != day_dir.name:
            raise ValueError(f"{path} contract identifies another table or day")
        if contract.get("rows") != md.num_rows:
            raise ValueError(f"{path} row count differs from contract")
        schema = pq.read_schema(path).remove_metadata()
        expected_schema = _FLIGHTS_SCHEMA if table == "flights" else _POINTS_SCHEMA
        if _schema_checksum(schema) != _schema_checksum(expected_schema):
            raise ValueError(f"{path} schema differs from current pipeline")
        contracts[table] = contract
        metadata[table] = md
    common = ("pipeline_version", "flights_rows", "points_rows",
              "flight_keyset_sha256", "source", "configuration")
    for key in common:
        if contracts["flights"].get(key) != contracts["points"].get(key):
            raise ValueError(f"source pair disagrees on {key}")
    flight_ids = pq.read_table(day_dir / "flights.parquet",
                               columns=["flight_id"])["flight_id"].to_pylist()
    if len(flight_ids) != len(set(flight_ids)):
        raise ValueError("duplicate flight_id in flights.parquet")
    if _flight_keyset_checksum(day_dir.name, flight_ids) != contracts["flights"]["flight_keyset_sha256"]:
        raise ValueError("flight keyset differs from source contract")
    point_ids = set(pq.read_table(day_dir / "points.parquet",
                                  columns=["flight_id"])["flight_id"].to_pylist())
    if point_ids != set(flight_ids):
        raise ValueError("flights.parquet and points.parquet keysets differ")
    return contracts["flights"]
