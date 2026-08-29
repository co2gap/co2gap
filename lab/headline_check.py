#!/usr/bin/env python3
"""Exact, release-specific guard for the numbers the site is about to publish.

Unlike ``freeze_check.py``, this module does not inspect rendered prose.  The
caller supplies the unrounded numeric values and counts used by the renderer;
they must match the reviewed release snapshot exactly, including the binary
value of every float.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Mapping


class HeadlineError(RuntimeError):
    """The calculated release headlines differ from the reviewed snapshot."""


def _read_snapshot(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text())
    except Exception as exc:
        raise HeadlineError(f"cannot read headline snapshot {path}: {exc}") from exc
    if data.get("schema_version") != 1:
        raise HeadlineError(
            f"unsupported headline schema {data.get('schema_version')!r} in {path}")
    if not isinstance(data.get("release_id"), str):
        raise HeadlineError(f"headline snapshot has no release_id: {path}")
    if not isinstance(data.get("values"), dict) or not data["values"]:
        raise HeadlineError(f"headline snapshot has no values: {path}")
    return data


def verify_release_headlines(
    actual: Mapping[str, int | float],
    snapshot_path: Path,
    *,
    release_id: str,
) -> None:
    """Require an exact keyset and exact numeric values for one release."""
    snapshot = _read_snapshot(snapshot_path)
    if snapshot["release_id"] != release_id:
        raise HeadlineError(
            f"headline snapshot is for release {snapshot['release_id']!r}, "
            f"not {release_id!r}")
    expected = snapshot["values"]
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    if missing or extra:
        raise HeadlineError(
            f"headline keyset differs: missing {missing}, extra {extra}")

    errors = []
    for key, wanted in expected.items():
        got = actual[key]
        if isinstance(wanted, bool) or not isinstance(wanted, (int, float)):
            raise HeadlineError(
                f"headline snapshot value {key!r} is not a JSON number")
        if isinstance(got, bool) or not isinstance(got, (int, float)):
            errors.append(f"{key}: calculated value is not numeric ({got!r})")
            continue
        if isinstance(wanted, int):
            if not isinstance(got, int) or got != wanted:
                errors.append(f"{key}: {got!r}, expected integer {wanted!r}")
        else:
            got_float = float(got)
            if (not math.isfinite(got_float)
                    or got_float.hex() != wanted.hex()):
                errors.append(
                    f"{key}: {got_float!r} [{got_float.hex()}], expected "
                    f"{wanted!r} [{wanted.hex()}]")
    if errors:
        raise HeadlineError(
            f"release {release_id} headline mismatch:\n  " + "\n  ".join(errors))
    print(f"release {release_id}: {len(expected)} exact headlines verified")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare two release-headline JSON snapshots exactly.")
    parser.add_argument("expected", type=Path)
    parser.add_argument("actual", type=Path)
    args = parser.parse_args()
    measured = _read_snapshot(args.actual)
    verify_release_headlines(
        measured["values"], args.expected, release_id=measured["release_id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
