"""
Data-source abstraction for the ADS-B CO2 observatory.

The rest of the pipeline consumes *trace dicts* and knows nothing about where
they came from. Today the source is a daily adsb.lol history dump (ODbL); a
future source (e.g. Wingbits) only needs to implement `iter_traces()` yielding
the same shape.

A "trace dict" is exactly the readsb trace_full structure:
    {
      "icao": "3c6444",
      "r":    "D-AI...",      # registration (may be missing)
      "t":    "A320",         # ICAO type code (may be missing)
      "desc": "AIRBUS A-320", # long description (may be missing)
      "dbFlags": 0,
      "timestamp": 1750000000.0,   # unix seconds, trace origin
      "trace": [ [dt, lat, lon, alt, gs, trk, flags, vrate, extra, src,
                  galt, gvrate, ias, roll], ... ]
    }

Memory model: everything is a generator. One aircraft trace is held at a time.
The split tar is read as a single sequential stream, so we never materialise
the whole day.

Data: adsb.lol historical (https://www.adsb.lol/docs/open-data/historical/),
licensed ODbL. Attribution required — see README.
"""

from __future__ import annotations

import gzip
import io
import json
import tarfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence


class _MultiFileReader(io.RawIOBase):
    """Read a sequence of files as one continuous stream (for split tars)."""

    def __init__(self, paths: Sequence[Path]):
        self._paths = [Path(p) for p in paths]
        self._idx = 0
        self.n_bytes = 0          # consumed so far -> lets the caller check
                                  # it actually reached the end of the dump
        self._fh = open(self._paths[0], "rb") if self._paths else None

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        if self._fh is None:
            return 0
        while True:
            n = self._fh.readinto(b)
            if n:
                self.n_bytes += n
                return n
            # current part exhausted -> advance
            self._fh.close()
            self._idx += 1
            if self._idx >= len(self._paths):
                self._fh = None
                return 0
            self._fh = open(self._paths[self._idx], "rb")

    def close(self):
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        super().close()


DECODE_ERRORS = (OSError, zlib.error, EOFError)
# ^ gzip.decompress raises THREE unrelated families on corrupt input:
#   * gzip.BadGzipFile (an OSError)  -> bad magic/header
#   * zlib.error                     -> corrupt deflate stream
#   * EOFError                       -> truncated member
# zlib.error and EOFError are NOT subclasses of OSError, so catching OSError
# alone (as this did until 2026-07-25) let a single bad member escape the
# worker and kill the WHOLE day: 500+ s of work and ~7.000 flights lost
# because one trace out of ~45.000 was damaged. Two days of the YTD backfill
# died this way (2026-05-04, 2026-04-30).

_decode_failures = 0


def _decode_member(raw: bytes) -> dict | None:
    """Decode one trace file. readsb stores them gzip-compressed.

    Returns None on a damaged member instead of raising: a corrupt trace is
    one aircraft missing from one day, which is noise at our aggregation
    level, while an exception costs the entire day. Failures are counted in
    `_decode_failures` so the caller can report them — silently dropping bad
    data without ever surfacing how much is the failure mode to avoid.
    """
    global _decode_failures
    if raw[:2] == b"\x1f\x8b":  # gzip magic
        try:
            raw = gzip.decompress(raw)
        except DECODE_ERRORS:
            _decode_failures += 1
            return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        _decode_failures += 1
        return None


def decode_failures() -> int:
    """How many members failed to decode in this process so far."""
    return _decode_failures


class TraceSource:
    """Abstract source of aircraft trace dicts."""

    def iter_traces(self) -> Iterator[dict]:  # pragma: no cover - interface
        raise NotImplementedError


@dataclass
class BBox:
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float

    def contains(self, lat: float, lon: float) -> bool:
        return (
            self.lat_min <= lat <= self.lat_max
            and self.lon_min <= lon <= self.lon_max
        )


class AdsbLolDaySource(TraceSource):
    """
    A single day of adsb.lol history, given as the split-tar parts.

    Only `trace_full_*` members are considered. If `bbox` is set, a trace is
    yielded only when at least one of its points falls inside the box — this is
    a cheap pre-filter that cuts a global day (~150k aircraft) down to the
    regional subset before any heavy processing.
    """

    def __init__(self, part_paths: Sequence[Path], bbox: BBox | None = None):
        self.part_paths = [Path(p) for p in part_paths]
        self.bbox = bbox

    def _in_box(self, trace: list) -> bool:
        if self.bbox is None:
            return True
        for p in trace:
            lat, lon = p[1], p[2]
            if lat is None or lon is None:
                continue
            if self.bbox.contains(lat, lon):
                return True
        return False

    def iter_traces(self) -> Iterator[dict]:
        stream = _MultiFileReader(self.part_paths)
        # r|  == streaming mode, no seeking, constant memory.
        # ignore_zeros=True for the same reason as in pipeline/run_daily.py,
        # where the full rationale is written out: some adsb.lol dumps carry a
        # pair of zero blocks at the seam between split parts, and without this
        # the reader stops there silently — no error, day written from its first
        # part alone. Measured once at 37% of a day dropped while reporting
        # success. This reader is the documented extension point for a future
        # data source, so it must not be the one carrying the bug.
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
                obj = _decode_member(f.read())
                if obj is None or "trace" not in obj:
                    continue
                if not self._in_box(obj["trace"]):
                    continue
                yield obj
