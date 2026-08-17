"""
Fase 3: turn the per-flight phase split into the attribution figures.

One place, because two callers need the same numbers and a second
implementation of the same normalisation would eventually drift from the first:
`lab/phase_report.py` prints them, and `lab/site_build.py` puts one sentence of
them on the page. A figure quoted on the site that no longer matches the report
it came from is the failure this module exists to prevent.

WHY THE MEAN AND NOT THE MEDIAN, here and only here. Everywhere else the project
normalises against the MEDIAN of comparable flights, which is the robust way to
rank an airport and is what the site publishes. That normalisation cannot be
decomposed: each bucket would be measured against its own median, and medians of
parts never add to the median of the whole, so the parts miss the total by a few
points. Read as shares anyway they gave one airport 327% and another 199%, which
is how the error announced itself. Means do add, cell by cell, exactly — so the
decomposition closes with a machine-epsilon residual. The cost is the usual one,
a reference pulled by the tail, and it is acceptable because "what share of the
gap came from where" is a question about sums to begin with.
"""

from __future__ import annotations

import glob
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

PHASE_A = ["excess_vert_climb_pct", "excess_vert_cruise_pct",
           "excess_vert_desc_pct"]
PHASE_B = ["excess_vert_dep_pct", "excess_vert_enr_pct", "excess_vert_arr_pct"]

# A share is only worth reporting for an airport whose deviation is big enough
# for the denominator to mean something; below this the ratio is noise wearing a
# percent sign.
MIN_DEV_PP = 2.0


def load_phase(phase_dir: Path) -> pd.DataFrame | None:
    """The phase parquet for every day, or None when there is none."""
    files = sorted(glob.glob(str(Path(phase_dir) / "*.parquet")))
    if not files:
        return None
    return pd.concat([pq.read_table(f).to_pandas() for f in files],
                     ignore_index=True)


def add_mean_norm(df: pd.DataFrame, bins, min_n_cell: int) -> pd.DataFrame:
    """Add `m_*` columns: deviation from the MEAN of comparable flights.

    Comparable = same distance band and same type, falling back to the band
    alone where a cell is too thin to be a reference. Rows missing any bucket
    are excluded from the references AND flagged, so a partial flight cannot
    quietly pull one of them.
    """
    df = df.copy()
    b = pd.cut(df.gc_km, bins).astype(str)
    cell = b + "|" + df.typecode
    enough = cell.map(cell.value_counts()) >= min_n_cell
    ok = df[PHASE_A + PHASE_B].notna().all(axis=1)
    for src in ["excess_vertical_pct"] + PHASE_A + PHASE_B:
        avg_bin = b.map(df[ok].groupby(b[ok])[src].mean()).to_numpy()
        m = ok & enough
        avg_cell = cell.map(df[m].groupby(cell[m])[src].mean()).to_numpy()
        ref = np.where(enough.to_numpy() & np.isfinite(avg_cell), avg_cell,
                       avg_bin)
        name = ("m_" + src.replace("excess_", "").replace("_pct", "")
                .replace("vertical", "vert"))
        df[name] = df[src].to_numpy() - ref
    df["decomposable"] = ok
    return df


def by_airport(df: pd.DataFrame, min_n_airport: int) -> pd.DataFrame:
    """One row per (airport, role) with the summed deviation and its parts.

    A flight is counted under both of its ends, as the site does, but the two
    roles are kept APART. Averaging over them is what hid the case that
    justifies this whole exercise: an airport can be at the norm on arrival and
    far above it on departure, and one number over both roles reports neither.
    """
    d = df[df.decomposable]
    both = pd.concat([
        d.assign(ap=d.origin_icao, own=d.m_vert_dep, far=d.m_vert_arr,
                 role="dep"),
        d.assign(ap=d.dest_icao, own=d.m_vert_arr, far=d.m_vert_dep,
                 role="arr")])
    tot_n = both.groupby("ap").size()
    g = both.groupby(["ap", "role"]).agg(
        n=("m_vert", "size"), sv=("m_vert", "sum"), own=("own", "sum"),
        far=("far", "sum"), enr=("m_vert_enr", "sum"),
        climb=("m_vert_climb", "sum"), cruise=("m_vert_cruise", "sum"),
        desc=("m_vert_desc", "sum"))
    g = g[g.index.get_level_values("ap").map(tot_n) >= min_n_airport]
    return g.assign(mean_v=g.sv / g.n)


def headline(g: pd.DataFrame) -> dict:
    """The few numbers the site sentence needs, as medians of per-airport shares.

    Summing every airport's deviation and dividing once would divide by nearly
    zero: a deviation from the norm sums to zero over the whole population by
    construction, so the airports above and below cancel and the ratio is noise.
    Each airport gets its own share, and the median of those is reported.
    """
    out = {"n_airports": int(g.index.get_level_values("ap").nunique())}
    for role in ("dep", "arr"):
        s = g.xs(role, level="role")
        s = s[s.mean_v.abs() >= MIN_DEV_PP]
        out[f"n_{role}"] = len(s)
        for k in ("own", "far", "enr", "climb", "cruise", "desc"):
            out[f"{role}_{k}"] = float((s[k] / s.sv).median() * 100.0)
    return out
