#!/usr/bin/env python3
"""
Which days are complete, which are short, and which are not there at all.

Why this exists: on 2026-08-09 the source dump was missing six hours of the
world's traffic. It downloaded cleanly, the reader consumed 100% of the bytes,
the parquet had a valid footer and 6,821 rows, and every guard in the pipeline
passed. Those guards answer "did we read the whole file" and "is the file
readable" — neither answers "does the file contain a whole day".

A day like that does not fail, it just quietly weighs half as much in any
monthly aggregate. So coverage has to be measured from the CONTENT, and the
result has to be published rather than kept in a log.

Detection is the hourly histogram of departures. Night hours are legitimately
near-empty even on healthy days (hour 23 UTC is routinely zero), so the test is
restricted to the hours that are always busy: 05:00-21:00 UTC, where a normal
day carries 500-900 departures per hour. A zero there is unambiguous.

    lab-venv/bin/python lab/coverage_audit.py            # writes data/coverage.json

Output is a JSON file, one record per day, meant to be published alongside the
figures and handed to anyone who asks how complete the dataset is.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
FLIGHTS_DIR = Path(os.environ.get("ADSB_FLIGHTS_DIR") or (ROOT / "data/flights"))
OUT = Path(os.environ.get("ADSB_COVERAGE_JSON") or (ROOT / "data/coverage.json"))
MISSING_TXT = FLIGHTS_DIR.parent / "backfill_missing.txt"

BUSY_FROM, BUSY_TO = 5, 21      # UTC hours that are always busy in ECAC
LOW_HOUR_FRAC = 0.25            # an hour under this share of the day's busy median is partial
LOW_DAY_FRAC = 0.70             # a day under this share of the trailing median is short


def hourly(day_dir: Path):
    """Departures per UTC hour, or None if the parquet is unreadable."""
    p = day_dir / "flights.parquet"
    try:
        ts = pq.read_table(p, columns=["dep_ts"]).column("dep_ts").to_numpy()
    except Exception:
        return None
    if ts.size == 0:
        return np.zeros(24, dtype=int)
    hours = ((ts // 3600) % 24).astype(int)
    return np.bincount(hours, minlength=24)


def main() -> int:
    days = sorted(d for d in FLIGHTS_DIR.glob("*") if (d / "flights.parquet").exists())
    if not days:
        print(f"no days under {FLIGHTS_DIR}", file=sys.stderr)
        return 1

    recs = []
    for d in days:
        h = hourly(d)
        if h is None:
            recs.append({"day": d.name, "status": "unreadable"})
            continue
        busy = h[BUSY_FROM:BUSY_TO + 1]
        med_busy = float(np.median(busy))
        empty = [BUSY_FROM + i for i, v in enumerate(busy) if v == 0]
        partial = [BUSY_FROM + i for i, v in enumerate(busy)
                   if 0 < v < LOW_HOUR_FRAC * med_busy] if med_busy > 0 else []
        recs.append({"day": d.name, "flights": int(h.sum()),
                     "empty_busy_hours": empty, "partial_busy_hours": partial,
                     "status": "ok"})

    # A day can also be short without any single hour going to zero, so compare
    # each day with the 28-day trailing median of its neighbours.
    counts = [r.get("flights", 0) for r in recs]
    for i, r in enumerate(recs):
        if r["status"] == "unreadable":
            continue
        lo, hi = max(0, i - 14), min(len(recs), i + 15)
        window = [c for j, c in enumerate(counts) if lo <= j < hi and j != i and c > 0]
        med = float(np.median(window)) if window else 0.0
        r["local_median_flights"] = int(med)
        short = med > 0 and r["flights"] < LOW_DAY_FRAC * med
        if r["empty_busy_hours"] or r["partial_busy_hours"] or short:
            r["status"] = "partial"

    # Days the source never published at all, so the record is complete.
    have = {r["day"] for r in recs}
    first = dt.date.fromisoformat(recs[0]["day"])
    last = dt.date.fromisoformat(recs[-1]["day"])
    absent, cur = [], first
    while cur <= last:
        if cur.isoformat() not in have:
            absent.append(cur.isoformat())
        cur += dt.timedelta(days=1)

    known_missing = []
    if MISSING_TXT.exists():
        known_missing = [ln.strip()[:10] for ln in MISSING_TXT.read_text().splitlines()
                         if ln.strip()]

    part = [r for r in recs if r["status"] == "partial"]
    doc = {
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "flights_dir": str(FLIGHTS_DIR),
        "first_day": recs[0]["day"], "last_day": recs[-1]["day"],
        "days_present": len(recs),
        "days_absent": absent,
        "days_absent_confirmed_unpublished": sorted(set(absent) & set(known_missing)),
        "days_partial": [r["day"] for r in part],
        "method": {
            "busy_hours_utc": [BUSY_FROM, BUSY_TO],
            "empty_hour": "zero departures in a busy hour",
            "partial_hour": f"under {LOW_HOUR_FRAC:.0%} of that day's busy-hour median",
            "short_day": f"under {LOW_DAY_FRAC:.0%} of the 28-day local median",
        },
        "days": recs,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, indent=1))

    print(f"{len(recs)} days {recs[0]['day']} -> {recs[-1]['day']}")
    print(f"absent: {len(absent)} {absent}")
    print(f"partial: {len(part)}")
    for r in part:
        print(f"  {r['day']}  {r['flights']:,} flights vs local median "
              f"{r['local_median_flights']:,}  empty hours {r['empty_busy_hours']}"
              f"  partial hours {r['partial_busy_hours']}")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
