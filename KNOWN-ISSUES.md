# Known issues

Defects found around the first release, measured rather than estimated, and
deliberately left in the frozen data. Some are also stated on the
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

**Closed for newly generated artefacts on `hardening-2027-01`; historical for
the September release.** New daily source, decomposition, phase and ground
parquet carry a versioned contract with schema, row count, keyset hash, input
fingerprints and configuration. The immutable September files predate that
format and are accepted only after their full-file checksums match
`release-manifest.json`.

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

The remaining cleanup is to rename `flown_ge_09gc`, whose name goes false the
day `FLOWN_MIN_FRAC` stops being 0.9. The stored contract already records the
numeric threshold independently of that legacy column name.

## 4. Late baselines extrapolated the 23:00 ERA5 field past midnight

**Closed for newly generated decomposition artefacts on
`hardening-2027-01`; historical for the September release.** The decomposer
used only the NetCDF named for the flight's departure day. Its interpolator was
allowed to extrapolate in time, so a baseline sampled after 23:00
kept extending that day's last wind field instead of reading the following UTC
day. New runs require both days, fingerprint both as inputs, and reject every
time outside actual hourly coverage or inside a temporal hole.

The two affected populations are different. Only **6 flights** have real track
points after midnight. The larger count, **17,082 flights (0.932%)**, refers to
time samples along the synthetic ideal or hybrid baseline after 23:00; it does
not mean that 17,082 observed trajectories crossed midnight. The latest
synthetic sample is 00:12:31.7 on the following day.

Recomputing only those affected baselines with the adjacent ERA5 file changes
the aggregate excess by **-0.532 tonnes CO2** and the total excess rate by
**-0.0000029 percentage points**. `real_mt` is unchanged. The largest absolute
per-flight shifts are **112.16 kg CO2** for the ideal baseline and **131.52 kg
CO2** for the hybrid baseline. Published rounding remains 23.37 Mt, 2.50 Mt,
12.1%, 7.51% lateral and 4.59% vertical.

**Consequence for reproduction:** the September parquet remains byte-for-byte
frozen rather than being silently regenerated with later wind inputs. A fresh
run of the corrected code will differ below the published precision.

**Fix, at the next release:** regenerate decomposition, phase, ground-adjusted
headlines and rankings in the single January rerun, together with the airport
resolver, speed-coverage gate and threshold-contract cleanup already listed
above; then replace this historical note with the new release identifiers and
checksums.

## 5. Calibration used four days outside the published population

**Closed for new release calibrations on `hardening-2027-01`; historical for
the September release.** `lab/calibrate.py` fitted its per-type factors on every
directory in the accumulating flight cache: **201 days through 24 July**, while
the published analysis contains **197 days through 20 July**. The immutable
manifest records that wider historical population because it is what actually
produced the frozen factors. Corrected runs instead select and verify the exact
`manifest.days`; newly generated manifests make the calibration and analysis
populations identical.

Refitting on the 197 published days changes 12 of the 15 stored type factors.
Applied to the same frozen flight-only population, it raises calibrated real
CO2 by **270.373 tonnes** (23.367308901 to 23.367579274 Mt), calibrated ideal CO2
by **243.275 tonnes**, calibrated hybrid CO2 by **259.086 tonnes**, and the
calibrated absolute excess by **27.098 tonnes** (2.495498543 to 2.495525641 Mt).
The uncalibrated 12.1%, 7.51% and 4.59% ratios do not change, and the published
23.37 Mt and 2.50 Mt roundings remain unchanged.

**Consequence for reproduction:** the September calibration JSON and its
201-day input checksum remain frozen in `release-manifest.json`. Recomputing the
factors with corrected code intentionally differs from that historical file.

**Fix, at the next release:** refit the factors on the exact January release
manifest and regenerate all calibrated totals, phase/ground-adjusted outputs,
headlines and rankings in the same single rerun as the other historical fixes;
then publish the new calibration and input checksums together.
