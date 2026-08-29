# Known issues

Three defects found before the first release, measured rather than estimated,
and deliberately left in the frozen data. Two of them are also stated on the
site — in methodology §11 and in the FAQ — because a reader who trusts a figure
should not have to read the source to learn its limits. This file is the
engineer's copy: what is wrong, how much it moves, and what fixing it requires.

The operational runbook that schedules the work is not in this repository. This
file is, so that a clone carries the defects along with the code.

## 1. The airport resolver used one longitude scale for all of Europe

`pipeline/airports.py` matched a trajectory's endpoints to an airport using
`cos(43°)` — the centre of the original, smaller study area — at every latitude.
It survived the move to the full ECAC area, which reaches 72°N, where the factor
is wrong by 46%: an endpoint 5.5 km east of its airport measured 8.0 km and lost
its ICAO. Corrected on 2026-08-29.

Measured on the 197 published days **before** changing it: 0.28% of origins and
0.53% of destinations move, and 229 airports change their movement count (Palma
−15%, Belfast Aldergrove +16%, Billund +18%). The ranking does not move at all —
same 152 airports, Spearman 1.0000, identical top twenty, median change 0.000
points, 0.162 at the worst and at unchanged rank.

**Consequence for reproduction:** re-running today's code reproduces the
rankings exactly and the movement counts approximately. The frozen window is
left alone rather than recomputed under a rule that changed mid-window.

**Fix, at the next release:** re-resolve every endpoint from the `o_lat`/`o_lon`
stored in `flights.parquet`, so the whole window is matched one way. The raw
dumps are not needed. The decomposition is per-flight physics and does not
depend on the ICAO; only the aggregation has to be redone.

## 2. The quality gate never looks at the speed channel

`pipeline/emissions.py` derives true airspeed as IAS where present and ground
speed otherwise. When **both** are absent there is no third branch and no guard:
TAS is NaN, the step contributes no fuel, and the flight leaves with an absurd
burn instead of being rejected. The gate cannot catch it — `coverage_frac` and
`max_gap_s` measure positional coverage, never speed.

The worst case in the published window is an A320 with 0 of 203 points carrying
IAS and 4 carrying ground speed: 62.7 kg of fuel for 55 minutes, a total gap of
−95.8%. 37 flights sit below −50%.

Removing them shifts the headline by 0.002 of a point and leaves the airport and
route rankings identical. **The defect pushes the gap down, never up:** a flight
that burns too little lowers the aggregate, so it cannot have inflated anything.

`lab/run_phase_split.py` already rejects that flight — it runs on ground speed
and finds none — which is why the phase split holds 1,833,126 rows against the
decomposition's 1,833,127. Two code paths disagree about one flight and the one
that discards it is right.

**Fix, at the next release:** a guard on the fraction of points carrying a
usable speed, and that fraction stored in `flights.parquet` next to
`coverage_frac`, since today it cannot be measured after the fact without
re-reading the points.

## 3. The thresholds are shared with the code but not stamped on the data

The track-quality thresholds now live in one place, `pipeline/track_quality.py`,
read by the gate, by the pipeline and by the sentence published in methodology
§6. The parquet files, however, store the gate **already applied**:
`coverage_frac` and `flown_ge_09gc` were computed with the thresholds of the
day. Change a threshold, rebuild only the site, and the page would state a
criterion the data do not meet.

`lab/site_build.py` checks `GC_MIN_KM` against the shortest published sector and
exits if it fails. That check is one-directional: it proves every row satisfies
the current threshold, not which threshold produced the file. Raising it fires;
lowering it passes in silence.

No equivalent check is possible for the other three. On `FLOWN_MIN_FRAC` it
could not fail at all — in the published window the minimum `flown/gc` ratio is
1.0001, so the 90% rule is not binding here — and a check that cannot fail is
worse than none. `coverage_frac` and the gap threshold are not carried by the
decomposition at all.

**Fix, at the next release:** write the threshold values and a version into the
parquet files (or a manifest beside them) and have the site compare what it read
with what it is about to publish. And rename `flown_ge_09gc`, whose name goes
false the day `FLOWN_MIN_FRAC` stops being 0.9.
