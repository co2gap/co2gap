#!/usr/bin/env python3
"""
Days where the source data is visibly incomplete.

Why this exists: on 2026-08-09 the source dump was missing six hours of the
world's traffic. It downloaded cleanly, the reader consumed 100% of the bytes,
the parquet had a valid footer and 6,821 rows, and every guard in the pipeline
passed. Those guards answer "did we read the whole file" and "is the file
readable" — neither answers "does the file contain a whole day". A day like that
does not fail, it just quietly weighs half as much in any monthly aggregate.

The test is deliberately narrow: an hour with ZERO departures where the same
hour normally carries hundreds. Nothing else. Earlier versions also flagged
"thin" hours and days below a fraction of the local median, and that produced
days nobody could defend as broken — 2026-03-24 was flagged without a single
empty hour. Marking only the unambiguous cases means the list can be published
as fact rather than as suspicion.

Which hours count as normally busy is not hand-picked: each hour is compared
with the median of the SAME hour over the surrounding days, and hours whose
reference is small (the night) are not judged at all. Traffic has a strong daily
shape and a fixed "busy window" mistakes the evening wind-down for missing data.

    ADSB_FLIGHTS_DIR=... lab-venv/bin/python lab/coverage_audit.py

Writes data/coverage.json, meant to be published next to the figures and handed
to anyone asking how complete the dataset is.
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

WINDOW_DAYS = 15        # days either side used to build the reference profile
MIN_HOUR_MEDIAN = 200   # an hour normally below this is night: not judged


def hourly(day_dir: Path):
    """Departures per UTC hour, or None if the parquet is unreadable."""
    try:
        ts = pq.read_table(day_dir / "flights.parquet",
                           columns=["dep_ts"]).column("dep_ts").to_numpy()
    except Exception:
        return None
    if ts.size == 0:
        return np.zeros(24, dtype=int)
    return np.bincount(((ts // 3600) % 24).astype(int), minlength=24)


def main() -> int:
    days = sorted(d for d in FLIGHTS_DIR.glob("*") if (d / "flights.parquet").exists())
    if not days:
        print(f"no days under {FLIGHTS_DIR}", file=sys.stderr)
        return 1

    recs, profile = [], []
    for d in days:
        h = hourly(d)
        recs.append({"day": d.name, "status": "unreadable" if h is None else "complete",
                     "flights": None if h is None else int(h.sum())})
        profile.append(h)

    for i, r in enumerate(recs):
        if profile[i] is None:
            continue
        lo, hi = max(0, i - WINDOW_DAYS), min(len(recs), i + WINDOW_DAYS + 1)
        near = [profile[j] for j in range(lo, hi) if j != i and profile[j] is not None]
        if not near:
            continue
        ref = np.median(np.vstack(near), axis=0)
        gaps = [h for h in range(24)
                if ref[h] >= MIN_HOUR_MEDIAN and profile[i][h] == 0]
        if gaps:
            r["status"] = "incomplete"
            r["missing_hours_utc"] = gaps
            r["flights_typical"] = int(np.median([n.sum() for n in near]))
            r["flights_missing_estimate"] = int(sum(ref[h] for h in gaps))

    have = {r["day"] for r in recs}
    first, last = (dt.date.fromisoformat(recs[k]["day"]) for k in (0, -1))
    absent, cur = [], first
    while cur <= last:
        if cur.isoformat() not in have:
            absent.append(cur.isoformat())
        cur += dt.timedelta(days=1)

    known = []
    if MISSING_TXT.exists():
        known = [ln.strip()[:10] for ln in MISSING_TXT.read_text().splitlines() if ln.strip()]

    bad = [r for r in recs if r["status"] == "incomplete"]
    doc = {
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "first_day": recs[0]["day"], "last_day": recs[-1]["day"],
        "days_present": len(recs),
        "days_absent": absent,
        "days_absent_never_published": sorted(set(absent) & set(known)),
        "days_incomplete": [r["day"] for r in bad],
        "criterion": ("a day is marked incomplete when one or more whole hours have zero "
                      f"departures while the same hour over the surrounding "
                      f"{WINDOW_DAYS} days normally carries at least {MIN_HOUR_MEDIAN}. "
                      "Only whole missing hours are marked; no other test is applied."),
        "days": recs,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, indent=1))

    print(f"{len(recs)} days  {recs[0]['day']} -> {recs[-1]['day']}")
    print(f"absent from the source: {len(absent)}  {absent}")
    print(f"incomplete: {len(bad)}")
    for r in bad:
        print(f"  {r['day']}  {r['flights']:>6,} flights (typical {r['flights_typical']:,})"
              f"  missing hours UTC {r['missing_hours_utc']}"
              f"  ~{r['flights_missing_estimate']:,} flights not in the source")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
