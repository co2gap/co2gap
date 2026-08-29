"""Immutable release boundaries and checksums shared by every pipeline stage.

The accumulating caches deliberately contain more days than a published
release.  A release manifest selects the exact published population and records
the inputs that produced it.  Consumers may ignore extra days in an input cache,
but release output directories must contain exactly the manifest day set.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class ManifestError(RuntimeError):
    """A release input, output or configuration does not match its manifest."""


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def set_checksum(root: Path, relative_paths: Iterable[str]) -> dict:
    """Digest a named set without making the digest depend on its root path.

    The outer digest covers each relative name, byte size and full file SHA-256.
    This makes truncation, replacement and renaming observable while allowing an
    immutable release to be reproduced in a different directory.
    """
    root = Path(root)
    outer = hashlib.sha256()
    count = total = 0
    for rel in sorted(relative_paths):
        path = root / rel
        if not path.is_file():
            raise ManifestError(f"missing release input: {path}")
        size = path.stat().st_size
        digest = sha256_file(path)
        outer.update(f"{rel}\0{size}\0{digest}\n".encode())
        count += 1
        total += size
    return {"sha256": outer.hexdigest(), "files": count, "bytes": total}


def role_paths(kind: str, days: Iterable[str]) -> list[str]:
    days = list(days)
    if kind == "flight-parquet-pairs":
        return [rel for day in days
                for rel in (f"{day}/flights.parquet", f"{day}/points.parquet")]
    if kind == "daily-netcdf":
        return [f"{day}.nc" for day in days]
    if kind == "daily-parquet":
        return [f"{day}.parquet" for day in days]
    raise ManifestError(f"unknown manifest artifact kind: {kind}")


def visible_days(root: Path, kind: str) -> set[str]:
    root = Path(root)
    if not root.exists():
        return set()
    if kind == "flight-parquet-pairs":
        return {p.name for p in root.iterdir() if p.is_dir()}
    if kind == "daily-netcdf":
        return {p.stem for p in root.glob("*.nc")}
    if kind == "daily-parquet":
        return {p.stem for p in root.glob("*.parquet")}
    raise ManifestError(f"unknown manifest artifact kind: {kind}")


@dataclass(frozen=True)
class ReleaseManifest:
    path: Path
    data: dict

    @classmethod
    def load(cls, path: str | os.PathLike) -> "ReleaseManifest":
        p = Path(path).resolve()
        try:
            data = json.loads(p.read_text())
        except Exception as exc:
            raise ManifestError(f"cannot read release manifest {p}: {exc}") from exc
        if data.get("schema_version") != 1:
            raise ManifestError(
                f"unsupported manifest schema {data.get('schema_version')!r} in {p}")
        days = data.get("release", {}).get("days")
        if not isinstance(days, list) or not days or days != sorted(set(days)):
            raise ManifestError("release.days must be a non-empty sorted unique list")
        return cls(p, data)

    @property
    def release_id(self) -> str:
        return str(self.data["release"]["id"])

    @property
    def days(self) -> list[str]:
        return list(self.data["release"]["days"])

    @property
    def era5_days(self) -> list[str]:
        return list(self.data["release"].get("era5_days", self.days))

    @property
    def calibration_days(self) -> list[str]:
        return list(self.data["release"].get("calibration_days", self.days))

    def require_day(self, day: str) -> None:
        if day not in set(self.days):
            raise ManifestError(
                f"{day} is outside release {self.release_id} ({len(self.days)} days)")

    def select_required_days(self, available: Iterable[str], label: str,
                             *, era5: bool = False) -> list[str]:
        wanted = self.era5_days if era5 else self.days
        missing = sorted(set(wanted) - set(available))
        if missing:
            raise ManifestError(
                f"{label}: {len(missing)} release day(s) missing; first {missing[0]}")
        return list(wanted)

    def require_exact_output_days(self, root: Path, role: str) -> None:
        spec = self.data["artifacts"][role]
        actual = visible_days(root, spec["kind"])
        wanted = set(self.days)
        missing, extra = sorted(wanted - actual), sorted(actual - wanted)
        if missing or extra:
            detail = []
            if missing:
                detail.append(f"{len(missing)} missing (first {missing[0]})")
            if extra:
                detail.append(f"{len(extra)} extra (first {extra[0]})")
            raise ManifestError(f"{role} is not release {self.release_id}: " + ", ".join(detail))

    def require_no_extra_output_days(self, root: Path, role: str) -> None:
        """Permit a resumable subset, but never artefacts outside the release."""
        spec = self.data["artifacts"][role]
        extra = sorted(visible_days(root, spec["kind"]) - set(self.days))
        if extra:
            raise ManifestError(
                f"{role} contains {len(extra)} day(s) outside release "
                f"{self.release_id}; first {extra[0]}")

    def verify_file(self, role: str, path: Path) -> None:
        expected = self.data["inputs"][role]
        actual = {"sha256": sha256_file(Path(path)), "bytes": Path(path).stat().st_size}
        for key in ("sha256", "bytes"):
            if actual[key] != expected[key]:
                raise ManifestError(
                    f"{role} does not match release {self.release_id}: "
                    f"{key} {actual[key]!r}, expected {expected[key]!r}")

    def verify_set(self, role: str, root: Path, *, artifact: bool = False) -> None:
        section = "artifacts" if artifact else "inputs"
        spec = self.data[section][role]
        if role == "era5":
            days = self.era5_days
        elif role == "calibration_flights":
            days = self.calibration_days
        else:
            days = self.days
        actual = set_checksum(Path(root), role_paths(spec["kind"], days))
        for key in ("sha256", "files", "bytes"):
            if actual[key] != spec[key]:
                raise ManifestError(
                    f"{role} does not match release {self.release_id}: "
                    f"{key} {actual[key]!r}, expected {spec[key]!r}")

    def verify_track_quality(self, module) -> None:
        expected = self.data["configuration"]["track_quality"]
        actual = {
            "gap_threshold_s": module.GAP_THRESHOLD_S,
            "coverage_min_fraction": module.COV_MIN,
            "flown_min_fraction": module.FLOWN_MIN_FRAC,
            "great_circle_min_km": module.GC_MIN_KM,
        }
        if actual != expected:
            raise ManifestError(
                f"track-quality configuration differs from release {self.release_id}: "
                f"{actual!r} != {expected!r}")


def optional_manifest(cli_path: str | None = None) -> ReleaseManifest | None:
    path = cli_path or os.environ.get("ADSB_RELEASE_MANIFEST")
    return ReleaseManifest.load(path) if path else None
