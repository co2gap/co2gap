#!/usr/bin/env python3
"""
Fase 2a, task 1: bulk ERA5 download for the full year-to-date range.

Reuses wind/era5.py download_day() as-is (the fase-1 module, unchanged) —
this script is only the orchestration around it: a day list, a bounded
thread pool (CDS is I/O/queue-bound, not CPU-bound, so threads are fine and
avoid pickling the cdsapi client across processes), idempotent skip of
already-valid files, an integrity check, and a per-day log.

Usage:
    lab-venv/bin/python scripts/era5_backfill.py 2026-01-01 2026-07-23
    ERA5_WORKERS=4 lab-venv/bin/python scripts/era5_backfill.py            # defaults below

Idempotent & resumable: rerun any time, already-valid days are skipped after
the integrity check. Safe to Ctrl-C — download_day() writes to a .part path
and only renames on success, so a killed download never leaves a corrupt .nc
in place of a good one; a corrupt file already on disk (e.g. from an old
external interruption) is caught by the integrity check and redownloaded.
"""

from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wind.era5 import download_day, ERA5_DIR, LEVELS, AREA  # noqa: E402

FLIGHTS_DIR = ROOT / "data/flights"

DEFAULT_FROM = "2026-01-01"
DEFAULT_TO = "2026-07-23"      # covers the instructed 07-21 plus the 2 extra
                                # days the Pi backfill has already produced
WORKERS = int(os.environ.get("ERA5_WORKERS", "4"))   # CDS fair-use: keep 3-5
LOG = ROOT / "logs" / "era5_backfill.log"


def daterange(d_from: str, d_to: str):
    a = date.fromisoformat(d_from)
    b = date.fromisoformat(d_to)
    step = 1 if b >= a else -1
    d = a
    out = []
    while True:
        out.append(d.isoformat())
        if d == b:
            break
        d += timedelta(days=step)
    return out


def prioritise(days: list[str]) -> list[str]:
    """
    Days that already have flight parquet come FIRST.

    ERA5 is indexed by date, so the natural order is chronological — but the
    Pi backfill runs newest-first, so a plain chronological ERA5 sweep would
    deliver January (no parquet yet) before May-July (parquet already on
    disk), and the decomposition would sit idle for hours with nothing it
    could actually process. Fetching the ready days first means the two
    backfills converge instead of racing in opposite directions.
    """
    ready = {d.name for d in FLIGHTS_DIR.glob("*")
             if (d / "flights.parquet").exists()}
    have, rest = [d for d in days if d in ready], [d for d in days if d not in ready]
    return have + rest


def log(msg: str):
    line = f"{time.strftime('%F %T')} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def is_valid(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 10_000:
        return False
    try:
        import xarray as xr
        ds = xr.open_dataset(str(path))
        ok = ("u" in ds.variables and "v" in ds.variables
              and ds.sizes.get("valid_time", 0) >= 20
              and ds.sizes.get("pressure_level", 0) == len(LEVELS))
        ds.close()
        return ok
    except Exception:
        return False


def fetch_one(day_iso: str):
    out = ERA5_DIR / f"{day_iso}.nc"
    if is_valid(out):
        return day_iso, "SKIP", 0.0, out.stat().st_size
    if out.exists():
        out.unlink()  # stale/corrupt -> force a clean redownload
    t0 = time.time()
    try:
        download_day(day_iso, levels=LEVELS, area=AREA, force=True)
        dt = time.time() - t0
        if not is_valid(out):
            return day_iso, "FAIL(integrity)", dt, 0
        return day_iso, "OK", dt, out.stat().st_size
    except Exception as e:
        return day_iso, f"FAIL({e.__class__.__name__}: {e})", time.time() - t0, 0


def main():
    d_from = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_FROM
    d_to = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_TO
    days = prioritise(daterange(d_from, d_to))
    log(f"==== ERA5 BACKFILL START {d_from} -> {d_to} ({len(days)} days, "
        f"{WORKERS} workers) ====")

    todo = [d for d in days if not is_valid(ERA5_DIR / f"{d}.nc")]
    already = len(days) - len(todo)
    log(f"{already} day(s) already valid, {len(todo)} to fetch")

    t_start = time.time()
    n_ok = n_skip = n_fail = 0
    sizes = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch_one, d): d for d in days}
        n_done = 0
        for fut in as_completed(futs):
            day_iso, status, dt, size = fut.result()
            n_done += 1
            if status == "OK":
                n_ok += 1
                sizes.append(size)
                elapsed = time.time() - t_start
                rate = elapsed / n_ok
                remaining = len(todo) - n_ok
                eta_s = remaining * rate
                log(f"{day_iso}  OK      {dt:6.1f}s  {size/1e6:5.1f} MB   "
                    f"[{n_done}/{len(days)}]  rate~{rate:.0f}s/day  "
                    f"ETA {eta_s/60:.0f} min ({remaining} left)")
            elif status == "SKIP":
                n_skip += 1
            else:
                n_fail += 1
                log(f"{day_iso}  {status}  ({dt:.1f}s)  [{n_done}/{len(days)}]")

    el = time.time() - t_start
    avg_mb = (sum(sizes) / len(sizes) / 1e6) if sizes else 0.0
    log(f"==== ERA5 BACKFILL DONE: {n_ok} ok, {n_fail} failed, {n_skip} already "
        f"valid, {el/60:.1f} min total, avg {avg_mb:.1f} MB/day ====")
    if n_fail:
        log("re-run the same command to retry only the failed/missing days")


if __name__ == "__main__":
    main()
