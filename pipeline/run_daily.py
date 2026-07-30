#!/usr/bin/env python3
"""
Production daily orchestrator (phase 1).

One adsb.lol day dump -> EU-South box -> complete flights -> thinned trajectory
+ first-pass OpenAP fuel/CO2 + coverage quality -> per-flight parquet on the WD.

Excess CO2 is NOT computed here: it is recomputed on the Mac with the ERA5 wind
baseline, from the stored trajectories. This job only does the expensive,
never-repeat work (parse the global dump, reconstruct flights) and persists it.

Parallelism: the global-dump parse is CPU-bound (gzip + json + model), not I/O
(4.3 GB read over ~20-40 min = a couple MB/s). The main process streams the
split-tar and reads raw member bytes (cheap); a pool of workers does the decode,
box test, flight reconstruction and fuel model. A bounded in-flight window keeps
memory well under the 1.5 GB budget.

Run inside the venv (Pi):
    WORKERS=3 nice -n15 ionice -c3 venv/bin/python pipeline/run_daily.py --day 2026.07.19
"""

from __future__ import annotations

import argparse
import os
import resource
import sys
import tarfile
import time
import warnings
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

warnings.filterwarnings("ignore", category=RuntimeWarning)

ROOT = Path(os.environ.get("ADSB_ROOT", "/mnt/wd_elements/adsb-co2"))
sys.path.insert(0, str(ROOT / "ingest"))
sys.path.insert(0, str(ROOT / "pipeline"))

from source import (BBox, _MultiFileReader, _decode_member,  # noqa: E402
                    decode_failures)
from trajectories import flights_from_trace, haversine_km    # noqa: E402
from emissions import openap_model, estimate_fuel            # noqa: E402
from flightproc import process_flight                        # noqa: E402
from airports import Airports                                # noqa: E402
from store import DayWriter, PIPELINE_VER                    # noqa: E402

# Default box: EU-South, lat 35-52, lon -10..25 (Iberia, France, Italy, Alps,
# Balkans, Greece, Malta, N-Africa coast). A wider box (full ECAC) is selected
# with ADSB_BBOX="27,72,-32,45" as lat_min,lat_max,lon_min,lon_max.
#
# CHANGING THE BOX MUST ALSO CHANGE OUT_DIR. Parquet is written to
# <OUT_DIR>/<YYYY-MM-DD>/ with no box in the path, so a second box aimed at the
# same directory would silently overwrite the first and the aggregates would
# then mix two different geographies with no error anywhere. Same failure mode
# as the ERA5 cache; both are guarded by making the directory explicit.
_bbox = os.environ.get("ADSB_BBOX")
if _bbox:
    _la0, _la1, _lo0, _lo1 = (float(x) for x in _bbox.split(","))
    BOX = BBox(lat_min=_la0, lat_max=_la1, lon_min=_lo0, lon_max=_lo1)
else:
    BOX = BBox(lat_min=35.0, lat_max=52.0, lon_min=-10.0, lon_max=25.0)
OUT_DIR = Path(os.environ.get("ADSB_FLIGHTS_DIR") or (ROOT / "data/flights"))
AIRPORTS_CSV = Path(os.environ.get("ADSB_AIRPORTS_CSV") or (ROOT / "data/airports.csv"))
LOAD_FACTOR = 0.82
RESERVE_KG = 2000.0
BATCH = 150                      # trace members per work unit
MIN_DUMP_COVERAGE = 0.90         # below this the dump was not read to the end

# per-worker globals (set by _init)
_BOX = None
_APT = None


def _init(box, airports_csv):
    global _BOX, _APT
    _BOX = box
    _APT = Airports(airports_csv)


def _process_batch(raw_batch):
    """Worker: decode a batch of raw trace members.

    Returns (list of (meta, points), n_undecodable). The counter travels back
    with the results because the workers are separate processes: a corrupt
    member is now skipped rather than fatal, and the only way to notice how
    much data that costs is to add it up in the parent.
    """
    bad_before = decode_failures()
    out = []
    for raw in raw_batch:
        obj = _decode_member(raw)
        if obj is None or "trace" not in obj:
            continue
        # box pre-filter (cheap: first in-box point wins)
        if not _any_in_box(obj["trace"]):
            continue
        for fl in flights_from_trace(obj, _BOX):
            model = openap_model(fl.typecode)
            if model is None:
                continue
            p0, p1 = fl.points[0], fl.points[-1]
            gc_km = haversine_km(p0.lat, p0.lon, p1.lat, p1.lon)
            pts, q = process_flight(fl, gc_km)
            res = estimate_fuel(fl, load_factor=LOAD_FACTOR,
                                reserve_kg=RESERVE_KG, tas_mode="ias")
            if not res.ok:
                continue
            meta = {
                "day": None,  # filled in main
                "typecode": fl.typecode,
                "operator": fl.operator,
                "model": model,
                "dep_ts": int(fl.t_start),
                "arr_ts": int(fl.t_end),
                "duration_s": q["duration_s"],
                "o_lat": p0.lat, "o_lon": p0.lon,
                "d_lat": p1.lat, "d_lon": p1.lon,
                "origin_icao": _APT.nearest(p0.lat, p0.lon),
                "dest_icao": _APT.nearest(p1.lat, p1.lon),
                "gc_km": q["gc_km"], "flown_km": q["flown_km"],
                "detour_pct": q["detour_pct"],
                "n_pts_native": q["n_pts_native"], "n_pts_stored": q["n_pts_stored"],
                "coverage_frac": q["coverage_frac"], "max_gap_s": q["max_gap_s"],
                "hole_time_s": q["hole_time_s"], "flown_ge_09gc": q["flown_ge_09gc"],
                "max_alt_ft": fl.max_alt,
                "fuel_kg_v0": res.fuel_kg, "co2_kg_v0": res.co2_kg,
                "cruise_ff_kgph_v0": res.cruise_ff_kgph,
                "init_mass_kg": res.init_mass_kg,
                "load_factor": LOAD_FACTOR, "reserve_kg": RESERVE_KG,
                "tas_mode": res.tas_mode, "pipeline_ver": PIPELINE_VER,
            }
            out.append((meta, pts))
    return out, decode_failures() - bad_before


def _any_in_box(trace):
    for p in trace:
        lat, lon = p[1], p[2]
        if lat is None or lon is None:
            continue
        if _BOX.contains(lat, lon):
            return True
    return False


def _iter_raw_members(part_paths, progress=None):
    """Yield raw (still-gzipped) bytes of every trace_full member, streaming.

    `progress`, if given, is a one-element list that receives the number of
    bytes actually consumed from the dump, so the caller can verify the
    stream reached the end instead of stopping early on a damaged archive.
    """
    stream = _MultiFileReader(part_paths)
    if progress is not None:
        progress[0] = stream          # expose the reader; n_bytes read later
    # ignore_zeros=True is REQUIRED, not a nicety. A tar stream normally ends
    # at the first pair of zero blocks, and some adsb.lol dumps carry that
    # pattern at the seam between split parts: the reader then stops there,
    # silently, with no error, and the day is written with only its first
    # part. Measured on 2026-05-04: 51.052 traces read vs 80.583 actually
    # present -- 37% of the day dropped while reporting success.
    # Verified safe on a healthy dump (2026-04-29): 67.673 traces either way,
    # byte-for-byte the whole archive, and identical to what the Pi reports.
    with tarfile.open(fileobj=stream, mode="r|", ignore_zeros=True) as tar:
        for member in tar:
            if not member.isfile():
                continue
            name = member.name
            if "trace_full_" not in name or not name.endswith(".json"):
                continue
            f = tar.extractfile(member)
            if f is None:
                continue
            yield f.read()


def _batched(iterable, n):
    batch = []
    for x in iterable:
        batch.append(x)
        if len(batch) >= n:
            yield batch
            batch = []
    if batch:
        yield batch


def peak_rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def run(day_tag: str, workers: int, max_flights: int | None = None):
    # dump tag date -> ISO day for the output dir
    d = datetime.strptime(day_tag.split("-")[0].lstrip("v"), "%Y.%m.%d").date()
    day_iso = d.isoformat()
    # accept either "2026.07.19" or the full release tag
    base = day_tag if day_tag.startswith("v") else f"v{day_tag}-planes-readsb-prod-0"
    # the dump has a day-dependent number of parts (aa, ab, [ac], ...) — take all
    parts = sorted((ROOT / "data/raw").glob(f"{base}.tar.*"))
    if not parts:
        raise SystemExit(f"no dump parts for {base} in data/raw")

    writer = DayWriter(OUT_DIR, day_iso)
    t0 = time.time()
    n_batches = n_traces_est = 0
    n_undecodable = 0

    import multiprocessing as mp
    ctx = mp.get_context("fork")

    def _consume(result):
        nonlocal n_undecodable
        results, n_bad = result
        n_undecodable += n_bad
        for meta, pts in results:
            meta["day"] = day_iso
            writer.add(meta, pts)

    reader_ref = [None]

    if workers <= 1:
        _init(BOX, AIRPORTS_CSV)
        for batch in _batched(_iter_raw_members(parts, reader_ref), BATCH):
            n_batches += 1
            n_traces_est += len(batch)
            _consume(_process_batch(batch))
            if max_flights and writer.n_flights >= max_flights:
                break
            if n_batches % 100 == 0:
                print(f"  ... {n_traces_est} members, {writer.n_flights} flights, "
                      f"{time.time()-t0:.0f}s, peakRSS {peak_rss_mb():.0f} MB", flush=True)
    else:
        pool = ctx.Pool(workers, initializer=_init, initargs=(BOX, AIRPORTS_CSV))
        pending = deque()
        max_pending = workers * 3
        try:
            for batch in _batched(_iter_raw_members(parts, reader_ref), BATCH):
                n_batches += 1
                n_traces_est += len(batch)
                pending.append(pool.apply_async(_process_batch, (batch,)))
                if len(pending) >= max_pending:
                    _consume(pending.popleft().get())
                if n_batches % 100 == 0:
                    print(f"  ... {n_traces_est} members, {writer.n_flights} flights, "
                          f"{time.time()-t0:.0f}s, peakRSS {peak_rss_mb():.0f} MB", flush=True)
                if max_flights and writer.n_flights >= max_flights:
                    break
            while pending:
                _consume(pending.popleft().get())
        finally:
            pool.terminate()
            pool.join()

    info = writer.flush()
    elapsed = time.time() - t0
    consumed = reader_ref[0].n_bytes if reader_ref[0] is not None else 0
    dump_size = sum(p.stat().st_size for p in parts)
    coverage = consumed / dump_size if dump_size else 0.0
    summary = {
        "day": day_iso, "day_tag": base,
        "box": [BOX.lat_min, BOX.lat_max, BOX.lon_min, BOX.lon_max],
        "workers": workers,
        "n_parts": len(parts),
        "dump_bytes": dump_size,
        "bytes_consumed": consumed,
        "dump_coverage": round(coverage, 4),
        "n_members_scanned": n_traces_est,
        "n_undecodable_members": n_undecodable,
        "n_flights": writer.n_flights,
        "points_rows": info["points_rows"],
        "elapsed_s": round(elapsed, 1),
        "peak_rss_mb": round(peak_rss_mb(), 1),
        "load_factor": LOAD_FACTOR, "reserve_kg": RESERVE_KG,
    }
    print("\n=== SUMMARY ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")

    # Coverage guard. A tar stream ends at the first pair of zero blocks, and a
    # damaged dump can carry that pattern at the seam between split parts: the
    # reader then stops early, reports no error, and the day looks like a clean
    # success while a whole part was silently dropped. Observed on the two
    # damaged dumps 2026-05-04 and 2026-04-30, which stop at the end of part
    # aa. Across 80 healthy days the member count never fell below 57.744, so a
    # day that reads well under its own file size is anomalous by construction.
    if coverage < MIN_DUMP_COVERAGE:
        print(f"\n!!! ATTENZIONE: letti solo {coverage*100:.1f}% dei byte del "
              f"dump ({consumed:,} di {dump_size:,}).")
        print("!!! Il tar si e' chiuso prima della fine: la giornata e' "
              "INCOMPLETA, non fidarsi del conteggio voli.")
        summary["incomplete"] = True
    print(f"wrote {info['flights_file']} ({info['flights_rows']} flights) "
          f"and {info['points_file']} ({info['points_rows']} points)")
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", help="dump date YYYY.MM.DD or full tag; default: yesterday UTC")
    ap.add_argument("--workers", type=int, default=int(os.environ.get("WORKERS", "3")))
    ap.add_argument("--max-flights", type=int,
                    default=int(os.environ["MAX_FLIGHTS"]) if os.environ.get("MAX_FLIGHTS") else None)
    args = ap.parse_args()
    day = args.day or (datetime.now(timezone.utc).date() - timedelta(days=1)).strftime("%Y.%m.%d")
    run(day, args.workers, args.max_flights)


if __name__ == "__main__":
    main()
