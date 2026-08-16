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

Detection is the hourly histogram of departures, with each hour compared against
the median of THAT SAME HOUR over the surrounding days. Traffic has a strong
daily shape — hour 23 UTC is routinely empty, hours 20 and 21 run at 45% and 25%
of the peak — so a fixed "busy window" mistakes the normal evening wind-down for
missing data. A first version of this script did exactly that and flagged
fourteen healthy days, one of which carried more traffic than its neighbours.
Comparing like with like calibrates itself and needs no window to be guessed.

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

WINDOW_DAYS = 15                # days either side used to build the reference profile
MIN_HOUR_MEDIAN = 200           # below this an hour carries too little to judge (night)
LOW_HOUR_FRAC = 0.25            # an hour under this share of its own median is missing data
LOW_DAY_FRAC = 0.70             # a day under this share of the local median is short


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

    recs, profile = [], []
    for d in days:
        h = hourly(d)
        if h is None:
            recs.append({"day": d.name, "status": "unreadable"})
            profile.append(None)
            continue
        recs.append({"day": d.name, "flights": int(h.sum()), "status": "ok"})
        profile.append(h)

    for i, r in enumerate(recs):
        if r["status"] == "unreadable":
            continue
        lo, hi = max(0, i - WINDOW_DAYS), min(len(recs), i + WINDOW_DAYS + 1)
        near = [profile[j] for j in range(lo, hi) if j != i and profile[j] is not None]
        if not near:
            continue
        ref = np.median(np.vstack(near), axis=0)          # per-hour reference
        missing, thin = [], []
        for hh in range(24):
            if ref[hh] < MIN_HOUR_MEDIAN:                 # night: nothing to judge
                continue
            v = profile[i][hh]
            if v == 0:
                missing.append(hh)
            elif v < LOW_HOUR_FRAC * ref[hh]:
                thin.append(hh)
        med_day = float(np.median([n.sum() for n in near]))
        short = med_day > 0 and r["flights"] < LOW_DAY_FRAC * med_day
        r["local_median_flights"] = int(med_day)
        r["missing_hours_utc"] = missing
        r["thin_hours_utc"] = thin
        if missing or thin or short:
            r["status"] = "partial"
            r["lost_flights_estimate"] = int(sum(
                max(ref[hh] - profile[i][hh], 0) for hh in missing + thin))

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
            "reference": f"median of the same hour over +/-{WINDOW_DAYS} days",
            "hours_judged": f"only hours whose reference exceeds {MIN_HOUR_MEDIAN} departures",
            "missing_hour": "zero departures where the reference is substantial",
            "thin_hour": f"under {LOW_HOUR_FRAC:.0%} of the same hour's reference",
            "short_day": f"under {LOW_DAY_FRAC:.0%} of the local daily median",
        },
        "days": recs,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, indent=1))

    print(f"{len(recs)} days {recs[0]['day']} -> {recs[-1]['day']}")
    print(f"absent: {len(absent)} {absent}")
    print(f"partial: {len(part)}")
    for r in part:
        print(f"  {r['day']}  {r['flights']:>6,} vs {r['local_median_flights']:>6,}"
              f"  (-{r.get('lost_flights_estimate',0):,})"
              f"  missing {r['missing_hours_utc']}  thin {r['thin_hours_utc']}")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
