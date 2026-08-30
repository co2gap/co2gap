# Paired sensitivity around an explicit corrected-wind nominal

Started 30 August 2026, on `uncertainty-v1`, after explicit user approval.
This is a laboratory follow-up to [SENSITIVITY-AUDIT.md](SENSITIVITY-AUDIT.md),
not a revision of the September release or preparation of the January release.
The earlier third run remains rejected under its frozen-reference contract.

## 1. What changed, and what did not

The default `frozen-release` profile still requires each nominal ideal and
hybrid fuel estimate to equal its frozen value within numerical roundoff:
relative tolerance `1e-10`, absolute tolerance `1e-6 kg`. It fails before
writing an aggregate when a changed reference violates that contract.

The opt-in `corrected-wind` profile instead asks: **how do the same finite
scenario changes behave around the nominal that uses the corrected ERA5
time boundary?** It uses the departure day's file and the following UTC day;
it does not extrapolate past available hours. There is no automatic fallback.

The experimental nominal changes wind handling only. It explicitly requires
load factor 0.82, reserve 2,000 kg, IAS-preferred observed speed, zero altitude
offset and the release's `a3000t70` ground definition. Altitude remains the
stored per-flight `cruise_alt_ft`. Real-track anchoring, ground shares, type
mapping, fuel model, calibration, selected flights and scenario steps remain
historical. No production optimiser or wind code was modified for this work.

The profile has two compulsory reconstruction checks:

1. The current profile/fuel calculation, fed the **stored mean winds**, must
   reproduce the frozen ideal and hybrid CO2 for each accepted flight.
2. The same calculation, changing **only those mean winds** to the newly sampled
   values, must reproduce the corrected nominal. A downstream fuel change that
   is not explained by the winds is still an error.

These checks use the scalar winds already in the immutable parquet, not a
revived historical interpolator. They check internal reconstruction; they do
not independently validate ERA5, aircraft performance or the real flight burn.
Changing the underlying wind model later would require its own methodological
review; the profile name is not permission to call arbitrary model changes
the known boundary correction. Output hashes identify the code used here.

## 2. The three comparisons must not be conflated

Schema 2 returns separate objects:

| Object | Comparison and meaning |
|---|---|
| `frozen_same_sample_reference` | Frozen calculation on the same accepted flights and weights. Not the exact full-population headline. |
| `nominal_minus_frozen_same_sample` | Change of reference, before scenario perturbations. |
| Scenario `delta_from_nominal` | Finite difference from that run's own explicitly named nominal. |
| `stored_wind_replay` | Checked count, maximum reconstruction errors and changes in mean winds. |
| `nominal_baseline_reconstruction` | Current nominal versus stored fuel, including the intentionally separated wind-handling change. |

A comparison against the full frozen headline also contains sampling error.
It cannot isolate the wind correction. All comparisons within a run use the
same accepted population. If any scenario fails on a flight, it is excluded
from every scenario **and** the frozen same-sample reference; the output is
marked incomplete. No failure can silently produce a complete population claim.

## 3. Direct test on the formerly incompatible flight

A private smoke fixture includes the real ERA5-boundary case from the first
audit. Running only that flight is explicitly marked truncated and is not a
population estimate. The fixture skips the already known input checksum pass
only for this targeted numerical check; full replications do not skip it.

| Check | Measured outcome |
|---|---|
| Default profile | Exit 1, `nominal baseline mismatch`, no output JSON. |
| Corrected-wind profile | Exit 0, explicitly labelled diagnostic; `population_expansion_complete=false`. |
| Stored-wind replay | Exactly 0 kg difference for both ideal and hybrid. |
| Corrected nominal minus frozen | Ideal 0 kg; hybrid +131.5169517964532 kg. |
| Mean-wind changes, absolute | GC 0.07390920253760669 m/s; track 0.0983189111814795 m/s. |
| Total gap effect in this one-flight fixture | 0 percentage points. |
| Component effects in this fixture | Lateral +0.1682643989 pp, vertical -0.1682643989 pp. |

The last two rows are specific to that flight, not a release-wide effect.
The zero ideal fuel change despite a nonzero wind change is compatible with
the historical profile's 20-second, integer-step cruise clock. Small changes
can leave the number of steps unchanged; this discretisation remains part of
the model, not a new physical threshold.

## 4. Full replications

The three existing sensitivity samples are rerun serially with all input
checksums enabled. Each contains 2,549 flights in 534 strata and expands to
1,833,127 flights. Maximum weight is 13,524.6; Kish effective sample size is
341.945. The draws together contain 6,921 distinct flights, not 7,647.
Additional seeds were chosen after inspecting earlier results: this is an
exploratory sampling-stability check, not a preregistration.

**All three new runs completed and passed the corrected-wind contract.** Each
verified the input manifest, processed all 2,549 flights across all nine
scenarios, had zero failures and replayed both frozen fuel baselines exactly
(maximum absolute error 0 kg). Runtime was 485.23, 476.69 and 482.75 seconds,
respectively. No partial run or old aggregate was substituted for a new run.

### Reference change, on the same sampled flights

| Check | Seed 20260901 | Seed 20260902 | Seed 20260903 |
|---|---:|---:|---:|
| Flights with changed mean GC wind | 23 | 18 | 19 |
| Flights with changed mean track wind | 25 | 21 | 24 |
| Maximum ideal fuel change (kg/flight) | 0 | 0 | 0 |
| Maximum hybrid fuel change (kg/flight) | 0 | 0 | 131.516952 |
| Total gap change (pp) | 0 within roundoff | 0 | 0 |
| Lateral gap change (pp) | 0 | 0 | +0.000004106 |
| Vertical gap change (pp) | 0 within roundoff | 0 | -0.000004106 |

The first draw's total/vertical residuals are about `2e-14` pp from floating-point
anchoring/accumulation. In the third draw, the hybrid weighted relative change
is `3.808233639546188e-8`, while the paired lateral/vertical changes are
`+/-4.105571166590494e-6` pp. Real, ideal and gap calibrated tonnes do not change
in any draw. These are **same-sample** effects, not estimates of the full
release-wide boundary correction already documented in `KNOWN-ISSUES.md` §4,
and not an uncertainty bound on the wind field.

The expanded experimental nominal levels are:

| Metric | Seed 20260901 | Seed 20260902 | Seed 20260903 |
|---|---:|---:|---:|
| Total gap (%) | 11.952048 | 12.008595 | 12.641281 |
| Lateral gap (%) | 7.466466 | 7.606884 | 7.807758 |
| Vertical gap (%) | 4.485582 | 4.401711 | 4.833523 |
| Real CO2 (Mt, calibrated) | 23.387469 | 23.454357 | 23.624298 |
| Ideal CO2 (Mt, calibrated) | 20.914511 | 20.967020 | 21.001776 |
| Gap CO2 (Mt, calibrated) | 2.472958 | 2.487337 | 2.622522 |

The percentages use the shared **uncalibrated** ideal denominator, as in the
site decomposition; masses include the type-specific calibration. Dividing
the displayed calibrated tonnes therefore does not reconstruct these percentage
rows. The calibrated percentage is a separate JSON metric.

The exact frozen full-population total gap is 12.091847%. Differences from that
number mainly describe sampling here; they must not be called the wind effect.
An independent recomputation of each frozen sample aggregate, using dataframe
joins and `math.fsum` rather than the runner's accumulator, agrees within
`7e-8` tonnes and `4e-13` percentage points across all three draws.

### Scenario effects around the corrected-wind nominal

Each entry is scenario minus **that draw's nominal**, in percentage points.
All scenario result objects, including their component and tonne metrics,
are numerically identical to the preceding anchored calculations. What has
changed is the explicit reference contract and its verified provenance, not a
post-hoc adjustment of the values. In particular, the old third output remains
rejected; the new third output comes from a full fresh, checked execution.

| Scenario | Seed 20260901 | Seed 20260902 | Seed 20260903 |
|---|---:|---:|---:|
| Cruise +1,000 ft | +1.012244 | +0.945788 | +0.990810 |
| Ground speed instead of IAS-preferred | +1.016148 | +1.325775 | +0.613906 |
| Load factor 0.72 | +0.434006 | +0.374327 | +0.436329 |
| Load factor 0.92 | -0.371821 | -0.385135 | -0.386900 |
| Cruise -1,000 ft | -0.274562 | -0.258583 | -0.249595 |
| Reserve 1,000 kg | +0.209324 | +0.174976 | +0.208572 |
| Reserve 3,000 kg | -0.212317 | -0.163479 | -0.217507 |
| Ground definition a1000t70 | +0.012733 | +0.021926 | +0.004371 |

The GS alternative varies from +0.614 to +1.326 pp across these draws: it is
not yet a stable population sensitivity estimate. The +1,000 ft effect is more
consistent in these three tests, but depends on the chosen step and historical
baseline. Neither range is a probability interval or a prescription.

**Next lab step proposed, not implemented here:** retain rare-stratum coverage
while allocating more observations to the large strata carrying most weight,
then retest the airspeed and altitude scenarios. Quantify numerical sampling
stability separately from physical-model uncertainty. Leave the external
held-out design untouched and do not turn this into January release work.

## 5. Verification and constraints

The automatic suite passes **102/102 tests normally and 102/102 with `-O`**,
including 34 uncertainty tests. Tests exercise both profiles with and without
wind changes; corrupted frozen fuel; unexplained downstream fuel changes;
missing/non-finite wind; missing/failed replay; all five nominal conventions;
release ground-definition mismatch; invalid profile; input verification;
truncated samples; and common exclusion after missing inputs or a single
scenario failure. Numerical tolerances were not widened.

The points reader now filters the selected flight IDs before converting to
pandas. A real-day equivalence test compared values, order and data types:
**4,036 selected rows out of 3,992,103 were identical**. The corresponding
pandas table occupies 145,428 bytes instead of 143,715,840. These are table
sizes, not a claim about total process peak memory or total runtime. Full runs
remain serial on this machine.

The user's scope restrictions deliberately leave the historical optimiser,
20-second profile discretisation, fixed ground shares and current model
structure in place. A deterministic re-optimisation or a fully revised model
could answer a better structural question, but that is not the experiment
authorised here. No January preparation, native-data regeneration, publication
or paid-data access was undertaken. The private held-out validation, its
registration and matcher remain untouched.

Privacy, descriptive language and not claiming unsupported confidence
intervals did not prevent these checks. Nor did the positioning constraints
(tool rather than an authored scientific claim, software/data DOI, no assumed
expert role). They would be counterproductive only if interpreted as a ban on
qualified external methodological review; no such interpretation was used.

The remaining scientific limits are not removed by exact reconstruction:

* scenario magnitudes are diagnostic steps, not probability distributions;
* one-factor effects do not measure interactions and must not be summed into
  an uncertainty interval;
* weights are concentrated and the three draws overlap;
* observed native-track response is approximated by anchored thinned tracks;
* ground shares are fixed under mass/reserve changes;
* a GS-versus-IAS experiment measures a convention, not the actual speed error;
* rejected and upstream-unobserved flights remain outside this experiment;
* external trajectory outcomes are still needed for the separate validation.

## 6. Reproduction and provenance

Use the full corrected-wind command in [UNCERTAINTY.md](UNCERTAINTY.md), with
the same nine scenarios and the three private sample paths below. There is no
`--limit` or `--skip-manifest-verification` in a full run. Outputs are new files
in `/tmp`; earlier outputs are retained unchanged.

Runtime used: Python 3.11.5, OpenAP 2.6.0, NumPy 2.4.6, SciPy 1.17.1,
pandas 3.0.5, PyArrow 25.0.0, xarray 2026.7.0, from `../lab-venv`.

Release-manifest SHA-256:
`db69266d5a4d9ce66f1d1f9bc4533cb31e6fd1b6d8e4fbcc600c53e6a5266a0c`

Unchanged scenario-design SHA-256:
`ab7e5e678959b0178d0620c8d228c9ae85f3b6f641929e65b23d5c706e9ea3f9`

Private samples, seed order 20260901 / 20260902 / 20260903:

* `/tmp/co2gap-uncertainty-sample.json`:
  `020bf2f60f8190ef063d4760fa1c0b5fa71f85b7ba69399f9587f40e868c7957`
* `/tmp/co2gap-uncertainty-sample-seed2.json`:
  `a9d2ea88ef642d72d1d789de385f74319949b7010480c30f189ee62ae133912a`
* `/tmp/co2gap-uncertainty-sample-seed3.json`:
  `7a84e6dad2ad7115d365d0840c894894dc42f9249bf15085a77dffda6c060f27`

The schema-2 outputs record SHA-256 for `lab/uncertainty.py`,
`pipeline/decompose.py`, `pipeline/excess_wind.py`, `pipeline/emissions.py`
and `wind/era5.py`. The runner hash for this experiment is
`72add8c9d24bbb9b81613275343ac76abdedba00c1fe9bbf5eb64a1b810d2253`.

New aggregate files and SHA-256:

* `/tmp/co2gap-corrected-wind-seed1.json`:
  `3887ac3e1aff25f553f2a3f6b64d93c05d4d93b6f5ee8f04d3991a5b6bfdc6c1`
* `/tmp/co2gap-corrected-wind-seed2.json`:
  `77d5dcb48c5752e9f26b2010589088620bffbce5bd550dcc4c8039380754ff51`
* `/tmp/co2gap-corrected-wind-seed3.json`:
  `5e2a7a50c73b90ae5eb06f7e8308a5383adf4b3551f0d1f1110ce0e9df0531de`

The private verifier `/tmp/co2gap_verify_corrected_wind.py` checks source/code
hashes, complete paired populations, zero replay errors, scenario equality to
the prior numerical outputs, and independently summed frozen sample metrics.
The three new outputs all pass. The earlier rejected third output is preserved
byte-for-byte and is not renamed, overwritten or promoted.

## 7. Site and repository isolation

The pre-commit site build, using the README release-profile inputs and an output
directory under `/tmp`, passed the 13 exact headline checks. All eleven requested
artefacts match this branch's `site/`: seven byte-for-byte; `data.html`,
`methodology.html` and `faq.html` differ only in the generated timestamp;
`sitemap.xml` differs only in `lastmod`. The comparator normalises only those
specific fields, not arbitrary dates or text. The same check is repeated after
the commit.

The reference is deliberately the site preserved on **`uncertainty-v1`**.
`master` and `origin/master` remain at `f646ca45e1c742d30096639b191d1b675ab8c778`
and contain later editorial changes not imported here. This branch must not be
deployed wholesale over them. No production code, release data, site prose,
release manifest, headline file, scenario design or held-out registration was
edited in this follow-up. Only lab code, its tests and documentation changed.
