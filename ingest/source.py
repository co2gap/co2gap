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
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence


class _MultiFileReader(io.RawIOBase):
    """Read a sequence of files as one continuous stream (for split tars)."""

    def __init__(self, paths: Sequence[Path]):
        self._paths = [Path(p) for p in paths]
        self._idx = 0
        self._fh = open(self._paths[0], "rb") if self._paths else None

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        if self._fh is None:
            return 0
        while True:
            n = self._fh.readinto(b)
            if n:
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


def _decode_member(raw: bytes) -> dict | None:
    """Decode one trace file. readsb stores them gzip-compressed."""
    if raw[:2] == b"\x1f\x8b":  # gzip magic
        try:
            raw = gzip.decompress(raw)
        except OSError:
            return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None


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
        # r|  == streaming mode, no seeking, constant memory
        with tarfile.open(fileobj=stream, mode="r|") as tar:
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
