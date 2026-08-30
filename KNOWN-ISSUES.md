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

## 6. The frozen anchor metadata describes a calculation that was not used

**Closed in the generator on `hardening-2027-01`; historical for the September
release metadata.** `data/anchored_cruise_ff.json` says its fuel-flow anchors
use the slope between the last two ICAO distance points. The numbers do not:
**19 of 20 anchored types use 1,500-2,000 NM**, and the one regional fallback,
E170, uses **1,000-1,500 NM**. Each record's `segment_nm` field is correct; only
the shared `_meta.method` string is false. The module docstring contained both
descriptions, contradicting itself.

The exact effect on every numeric anchor, flight estimate and published figure
is **zero**: this correction changes only the description emitted by a future
run. The frozen JSON is not edited now because that would change a release
input after its figures and provenance were fixed.

**Fix, at the next release:** regenerate `anchored_cruise_ff.json` with the
correct method string during the single January rerun and update its checksum
together with the other release inputs. The 20 numeric records need not move
unless their source table, OpenAP version or segment-selection rule changes.

## 7. An early-ending dump could be promoted as a complete day

**Closed for future ingestion on `hardening-2027-01`; the September release was
not affected.** `pipeline/run_daily.py` used to write the final flight and point
parquet before checking how much of the split tar had actually been consumed.
Below `MIN_DUMP_COVERAGE` it set `summary["incomplete"] = True` but returned
success. The nightly job could therefore report `pipeline OK`, and its next run
could accept the readable pair as finished.

The two real dumps named in the incident comment, **2026-04-30** and
**2026-05-04**, do not contaminate the published release. They were downloaded
again after the damage was observed and the frozen population marks both
`complete`, with respectively **11,021** and **14,642** flights. This was a risk
to future ingestion, not evidence of missing published flights.

New runs preserve the downloader's complete asset list, require every declared
part at its declared byte size, write into a sibling staging directory, and
promote only after the tar reaches normal completion and dump coverage is at
least 90%. The final source contract records the asset-manifest SHA-256, each
asset name/size/URL, consumed and declared bytes, coverage threshold and result.
A failure exits non-zero and removes only its staging tree. An older promoted
day that fails the new contract is moved intact into a sibling quarantine and
reported; no acquisition script deletes it automatically.

## 8. Phase attribution could disappear from a site build without an error

**Closed for release builds on `hardening-2027-01`; the September site was not
affected.** `lab/site_build.py` used to catch every exception while importing
`lab/phase_attrib.py` and return `None`. With no release manifest, an absent or
partial phase directory also returned `None`. All three cases selected older
wording that says the gap cannot be located inside the flight, while the build
still exited successfully.

The frozen release manifest contains and verifies **197 phase parquet files,
234,489,062 bytes**, with set SHA-256
`c10c5f585de9386e831a45dbd3f48ac8177324de6139c4e2053a8a846de5afb7`; the
published page contains the phase-attribution paragraph. This defect could have
removed a finding from a later build, but it did not change the September
headline values or the currently published wording.

Site generation now requires an explicit profile. `--profile release` refuses
to start unless manifest, decomposition, phase, ground, calibration, airport,
coverage, headline and output paths are all explicit; it verifies the manifest
and treats import, read, day-set and keyset phase failures as fatal.
`--profile exploratory` is the only mode allowed to omit phase attribution. It
announces at startup that release guarantees are disabled, prints the precise
fallback reason to stderr, and does not apply the frozen release-headline gate.

## 9. Historical attrition before the durable flight tables is unobservable

**Measured at the quality gate; open upstream for the September release.** The
frozen `flights_ecac` tables precede the four analysis predicates, so
`lab/uncertainty.py selection` can rebuild those predicates and requires their
passing `(day, flight_id)` keyset to equal the decomposition exactly. It can
therefore state the gate's conditional coverage without sampling or inference.
For the frozen 197 days the durable denominator is **2,115,824 flights** and the
gate retains **1,833,127 (86.64%)**, representing **85.57% of great-circle
kilometres**, **85.58% of flown kilometres** and **86.50% of first-pass
gate-to-gate CO2**. The latter is only an exposure proxy, not the published
airborne inventory. These shares measure coverage, not the bias in the 12.1%
headline: the excluded rows do not have a trustworthy decomposition.

The average hides a material time pattern. Retention falls to **30.28% on 24
March** and **52.56% on 25 March**. Both source dumps are correctly marked
`complete`; the loss is instead inside flight tracks, where the coverage
criterion alone fails 67.45% and 42.82% of rows. Source-byte completeness and
trajectory representativeness are different properties. The accepted
population therefore underweights those two days, and block-resampling accepted
days cannot recreate the flights rejected from them.

The same files are already downstream of four earlier decisions: a trace must
touch the geographic box, yield a complete flight, map to an OpenAP-supported
aircraft and produce a successful first-pass fuel estimate. The September
artefacts retained neither rejected records nor aggregate counts for those
stages. Calling the durable pre-gate table “all ECAC flights” would therefore be
false, and no retrospective percentage is reported.

New ingestion closes the prospective part of the problem. Each promoted day
stores, in both parquet source contracts, an internally validated aggregate
funnel from declared dump members through trace location, exclusive leg
rejection reasons, aircraft support, fuel-model success and all 16 combinations
of the four quality predicates. No rejected trace, aircraft or flight identifier
is retained. Historical contracts without the optional block remain valid; the
missing September denominator can only be closed by rerunning the raw dumps or
by measuring a future representative period.

The observable side has also been stress-tested without decomposing rejected
flights. Tightening coverage to 95% raises the total gap by **+0.344 percentage
points after standardising the ideal-CO2 mix by aircraft type and distance**;
requiring no gap above 300 seconds raises it by **+0.375 points**. Almost the
whole movement is vertical. Removing the two lowest-retention days changes the
standardised result by only **-0.001 points**, so those days are conspicuous but
do not drive the headline. This is evidence of a within-gate quality gradient,
not a correction or a bound for excluded flights. The strict 99%/120-second
cases require maximum weights above 41 and are retained only as instability
diagnostics.

An independent validation design is now executable but has no outcome source
yet. `lab/uncertainty.py selection-validation-sample` pre-registers 5,000
private flights in 843 failure/quality/fleet cells and expands exactly to the
2,115,824-flight durable pre-gate population. A minimum-plus-proportional
allocation reduced the measured maximum weight from 22,541 under equal
allocation to 701.23, and raised Kish effective size from about 299 to
3,537.75. The separate external match list withholds release id, gate result,
quality fields and resolved airports. Until a genuinely independent trajectory
or operational source returns complete outcomes, the analyzer exits with an
explicit blocked estimate and the headline bias remains unbounded. The design
also cannot recover the earlier regional, incomplete-leg, unsupported-type or
fuel-model exclusions.

A free OpenSky scientific snapshot now supplies a narrower observed test for
1 March 2026. The matcher is blind to the primary gate and finds 8,726 of the
10,290 durable pre-gate flights, but response is differential: **86.17%** among
gate passes and **74.65%** among rejects. Source quality leaves **7,615** and
**829** model outcomes, or 83.99% and 67.78% of the original groups. The raw
state-vector pilot also exposed sporadic one-second coordinate contaminants;
the recorded post-pilot protocol uses fixed one-minute medians and is labelled
exploratory rather than retroactively pre-registered.

Conditional on those two filters, the OpenSky proxy gap is 8.17% for passes and
6.65% for rejects, but the combined number hides different failure mechanisms:
coverage-only failures are at **11.96%**, whereas unresolved-endpoint failures
are at **5.94%**. More importantly, on the same 7,615 controls the source proxy
is **4.61 percentage points below** the frozen like-for-like airborne primary
gap (-1.62 lateral and -2.98 vertical). OpenSky therefore demonstrates that a
free external-day audit is feasible and that failure masks must not be pooled
blindly; it does not yet provide an unbiased missing outcome. Possible overlap
among community receivers, one-day scope, differential matching and the proxy
offset keep the release-wide selection bias unbounded.
