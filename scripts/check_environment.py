#!/usr/bin/env python3
"""Verify the Python version and direct dependency lock for one pipeline role."""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = re.compile(r"^# python==(\d+(?:\.\d+){1,2})$")
PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s]+)$")
IMPORT_NAMES = {
    "cdsapi": "cdsapi",
    "netCDF4": "netCDF4",
    "numpy": "numpy",
    "openap": "openap",
    "pandas": "pandas",
    "pyarrow": "pyarrow",
    "scipy": "scipy",
    "xarray": "xarray",
}


def read_lock(path: Path) -> tuple[tuple[int, ...], dict[str, str]]:
    python = None
    packages = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        match = PYTHON.fullmatch(line)
        if match:
            python = tuple(int(part) for part in match.group(1).split("."))
            continue
        match = PIN.fullmatch(line)
        if match:
            packages[match.group(1)] = match.group(2)
    if python is None or not packages:
        raise SystemExit(f"incomplete environment lock: {path}")
    return python, packages


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=("pi", "lab"))
    args = parser.parse_args()
    lock = ROOT / f"requirements-{args.role}.lock"
    expected_python, packages = read_lock(lock)
    actual_python = sys.version_info[:len(expected_python)]
    errors = []
    if actual_python != expected_python:
        errors.append(
            f"Python {'.'.join(map(str, actual_python))}, expected "
            f"{'.'.join(map(str, expected_python))}")
    for distribution, expected in packages.items():
        try:
            actual = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            errors.append(f"{distribution} is not installed (expected {expected})")
            continue
        if actual != expected:
            errors.append(f"{distribution} {actual}, expected {expected}")
            continue
        module = IMPORT_NAMES.get(distribution)
        if module:
            try:
                importlib.import_module(module)
            except Exception as exc:
                errors.append(f"{distribution} {actual} cannot import: {exc}")
    if errors:
        raise SystemExit("environment mismatch:\n  " + "\n  ".join(errors))
    print(
        f"{args.role}: Python {'.'.join(map(str, expected_python))} and "
        f"{len(packages)} direct dependencies match {lock.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
