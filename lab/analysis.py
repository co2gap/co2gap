"""
Shared analysis layer for the Mac lab: load accumulated flights, compute
wind-aware excess per flight, apply per-type calibration, aggregate.

Note on calibration and excess: the per-type factor multiplies the OpenAP fuel
of BOTH the real flight and its nominal (same type, same model), so it CANCELS
in excess% — excess is calibration-invariant. Calibration only rescales the
ABSOLUTE CO2 used for tonnage in the report.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "ingest"))
sys.path.insert(0, str(ROOT))

from excess import build_nominal_flight            # noqa: E402
from excess_wind import ideal_co2_windaware        # noqa: E402
from emissions import estimate_fuel                 # noqa: E402
from wind.era5 import WindField                     # noqa: E402

FLIGHTS_DIR = ROOT / "data/flights"
ERA5_DIR = ROOT / "data/era5"
CALIB = ROOT / "data/calibration.json"
LOAD_FACTOR = 0.82
RESERVE_KG = 2000.0


def load_flights(days=None) -> tuple[pd.DataFrame, list]:
    dirs = sorted(FLIGHTS_DIR.glob("*")) if not days else [FLIGHTS_DIR / d for d in days]
    frames, loaded = [], []
    for d in dirs:
        f = d / "flights.parquet"
        if f.exists():
            frames.append(pq.read_table(f).to_pandas())
            loaded.append(d.name)
    if not frames:
        raise SystemExit(f"no flights.parquet under {FLIGHTS_DIR}")
    return pd.concat(frames, ignore_index=True), loaded


def load_calibration() -> dict:
    if CALIB.exists():
        return json.loads(CALIB.read_text())
    return {}


def build_windfield(days) -> WindField | None:
    ncs = [ERA5_DIR / f"{d}.nc" for d in days if (ERA5_DIR / f"{d}.nc").exists()]
    return WindField(ncs) if ncs else None


def quality_gate(df) -> pd.DataFrame:
    return df[(df.origin_icao.notna()) & (df.dest_icao.notna())
              & (df.flown_ge_09gc) & (df.coverage_frac >= 0.85)
              & (df.gc_km >= 150)].copy()


_free_cache: dict = {}


def _ideal_free(typecode, gc_km):
    key = (typecode, round(gc_km / 25) * 25)
    if key not in _free_cache:
        nf = build_nominal_flight(typecode, gc_km)
        val = None
        if nf is not None:
            r = estimate_fuel(nf, load_factor=LOAD_FACTOR, reserve_kg=RESERVE_KG, tas_mode="gs")
            if r.ok and r.co2_kg > 0:
                val = r.co2_kg
        _free_cache[key] = val
    return _free_cache[key]


def enrich(df, wf, calib) -> pd.DataFrame:
    """Add excess_free, excess_wind (%), and co2_cal (kg) columns."""
    ex_free, ex_wind, co2_cal = [], [], []
    for r in df.itertuples(index=False):
        k = calib.get(r.typecode, 1.0)
        real = r.co2_kg_v0
        co2_cal.append(real * k)
        idf = _ideal_free(r.typecode, r.gc_km)
        ex_free.append((real - idf) / idf * 100 if idf else np.nan)
        if wf is not None:
            iw = ideal_co2_windaware(r.typecode, r.o_lat, r.o_lon, r.d_lat, r.d_lon,
                                     int(r.dep_ts), r.gc_km, wf,
                                     load_factor=LOAD_FACTOR, reserve_kg=RESERVE_KG)
            ex_wind.append((real - iw["ideal_co2_kg"]) / iw["ideal_co2_kg"] * 100
                           if iw else np.nan)
        else:
            ex_wind.append(np.nan)
    out = df.copy()
    out["excess_free"] = ex_free
    out["excess_wind"] = ex_wind
    out["co2_cal_kg"] = co2_cal
    return out
