# Sensitivity sampling: same number of flights, less concentrated weights

Laboratory follow-up to [CORRECTED-WIND-SENSITIVITY.md](CORRECTED-WIND-SENSITIVITY.md),
on `uncertainty-v1`. No production change, release regeneration, publication or
January release preparation. The separate 5,000-flight and 2,279-flight held-out
designs and matching protocol remain unchanged.

## 1. Question and decisions made before the new runs

The equal-cap pilot gave the GS-versus-IAS scenario total effects of +1.016,
+1.326 and +0.614 percentage points across three draws. Its 2,549 sampled
flights had a maximum expansion weight of 13,524.6 and a Kish effective sample
size of 341.945. The next question is whether an allocation that represents
large cells more densely gives more stable sensitivity estimates **at the
same computational sample size**.

The fixed budget is the number of sampled flights and flight-scenario
evaluations, not a promise of equal wall-clock runtime on different flights.

The choices were recorded in [sensitivity-sampling-design.json](sensitivity-sampling-design.json)
before drawing and running the new samples. This is explicitly a **post-pilot
exploration**, not confirmatory preregistration. Allocation uses only the
population counts, not measured scenario effects or their pilot variances:

* same 2,549-flight budget and 534 type/distance/coverage cells;
* minimum two flights per cell, or a census if the cell has fewer than two;
* remaining budget proportional to the population remaining after those minima;
* integer largest remainders, with lexicographic cell names breaking ties;
* simple random sampling without replacement within each cell; weight `N_h/n_h`;
* the same three seeds, 20260901 / 20260902 / 20260903, with no redrawing;
* all nine unchanged scenarios, including the nominal, using `corrected-wind`;
* report all results, even if stability is unchanged or worse.

The recorded plan is used to reproduce this experiment. It is not an extra
release gate, and the general sampler remains usable for other declared lab
experiments. The historical equal-cap mode is still the default.

Minimum one would give a higher weight-only ESS (2,231.214) but leave 299 cells
with one sampled flight, many not censuses. Minimum three would lower that ESS
to 1,399.356 at this fixed budget. Minimum two keeps at least two observations
in every non-census cell and leaves 1,501 observations for the proportional
part, after a 1,048-flight floor. This is a transparent compromise, not a proof
of an optimal allocation for these ratio outcomes.

## 2. Allocation and reproducibility measured

| Quantity | Historical equal cap | Balanced allocation |
|---|---:|---:|
| Sample rows per draw | 2,549 | 2,549 |
| Represented cells | 534 | 534 |
| Expanded population | 1,833,127 | 1,833,127 |
| Maximum weight | 13,524.6 | 1,186.368421 |
| Kish effective sample size | 341.944751 | 1,830.647479 |
| Largest sample within a cell | 5 | 57 |
| Distinct flights across three draws | 6,921 | 7,446 |

All three new sample manifests were generated with input verification and
checked against the original population's cell sizes. No cell was lost;
each cell's row count equals its integer allocation and its weights expand
to that cell's population. The first historical sample was regenerated using
the default command and is **byte-for-byte identical** to its original file.

The new design has 29 census cells, including 20 singletons. Sixteen cells
that were previously censuses of three to five flights are now samples; all
remain represented. Preserving rare cells does not preserve all old rows.

New pairwise overlaps are 88 / 84 / 79 flights, with 50 in all three draws.
Old/new overlaps for the same seed are only 161 / 140 / 145. Consequently,
**the old and new designs are not flight-paired experiments**. Scenario
comparisons inside each draw remain paired on the same flights, but the
cross-design comparison includes different sampled flights. Neither the
higher Kish ESS nor a three-draw range is an outcome-specific confidence
interval or a physical-model uncertainty interval.

## 3. Full sensitivity results

All three runs completed: 2,549 flights and all nine scenarios per draw, zero
failures, complete expansion and verified source checksums. Both reference
checks passed for every flight; replay of the stored-wind ideal and hybrid
was **exactly zero kg** in all three runs. A separate join/`math.fsum`
reconstruction of the frozen aggregates agrees within `6e-8` tonnes and
`2e-13` percentage points. Scenario additivity also closes within numerical
tolerance.

Observed wall times were **754.20 / 795.57 / 898.38 seconds**, 40.80 minutes
in total, versus 485.23 / 476.69 / 482.75 seconds in the equal-cap experiment.
These are recorded run times, not a controlled performance benchmark: flights
and machine load differ. The same number of evaluations did not give the same
elapsed cost.

### 3a. Reference change, kept separate from sampling and scenarios

| Corrected nominal minus frozen on the same sample | 20260901 | 20260902 | 20260903 |
|---|---:|---:|---:|
| Flights with changed mean GC wind | 18 | 24 | 12 |
| Flights with changed mean track wind | 18 | 25 | 15 |
| Maximum ideal CO2 change (kg/flight) | 43.960047 | 97.382693 | 0 |
| Maximum hybrid CO2 change (kg/flight) | 71.364584 | 97.589690 | 0 |
| Total gap change (pp) | -0.000304525 | -0.000261426 | 0 |
| Lateral gap change (pp) | -0.000083832 | +0.000254235 | 0 |
| Vertical gap change (pp) | -0.000220693 | -0.000515661 | 0 |
| Ideal CO2 change (calibrated tonnes) | +51.740129 | +44.471430 | 0 |
| Gap CO2 change (calibrated tonnes) | -51.740129 | -44.471430 | 0 |

Observed CO2 is unchanged. The third run has changed winds but no changed
fuel baseline at the model's existing discretisation. None of these weighted
sample deltas is the effect on the entire release.

The new seed-20260901 reference change affects the ideal as well as the hybrid.
Its lateral and vertical changes are therefore not equal and opposite. That
symmetry was necessary in the earlier hybrid-only case, with observed and ideal
CO2 fixed; it is not a general requirement when the ideal denominator changes.

### 3b. Sample levels versus the exact observed population

| Metric | 20260901 | 20260902 | 20260903 |
|---|---:|---:|---:|
| Frozen same-sample total gap (%) | 12.281055 | 12.065665 | 12.195406 |
| Error versus exact frozen total (pp) | +0.189209 | -0.026181 | +0.103560 |
| Corrected nominal total gap (%) | 12.280751 | 12.065404 | 12.195406 |
| Corrected nominal lateral gap (%) | 7.713692 | 7.593252 | 7.409232 |
| Corrected nominal vertical gap (%) | 4.567059 | 4.472152 | 4.786174 |
| Corrected nominal real CO2 (Mt, calibrated) | 23.395043 | 23.337270 | 23.412772 |
| Corrected nominal ideal CO2 (Mt, calibrated) | 20.859969 | 20.850498 | 20.899774 |
| Corrected nominal gap CO2 (Mt, calibrated) | 2.535074 | 2.486772 | 2.512998 |

The exact frozen population total remains **12.091846635657904%**. Sampling
error above compares the *frozen* estimate with the frozen population, so it
does not mix in the wind reference change. The frozen total's three-draw span
falls from 0.689233 to 0.215390 pp; lateral falls from 0.341288 to 0.304543 and
vertical from 0.431816 to 0.313507 pp. Weight closure is still not exact recovery
of the headline. These three errors are observations, not a confidence interval.

Percentages use the uncalibrated ideal denominator, as in the site's split;
tonnes include calibration by aircraft type. Do not reconstruct these
percentages by dividing the calibrated tonne columns.

### 3c. All scenario total effects, in percentage points from each nominal

The spans are maximum minus minimum across the three draws, not confidence
limits. The nominal has a zero scenario delta by definition.

| Scenario | 20260901 | 20260902 | 20260903 | New span | Equal-cap span |
|---|---:|---:|---:|---:|---:|
| Nominal | 0 | 0 | 0 | 0 | 0 |
| GS instead of IAS-preferred | +1.152206 | +1.023600 | +1.006450 | 0.145756 | 0.711869 |
| Cruise +1,000 ft | +0.963743 | +0.947095 | +0.977459 | 0.030365 | 0.066456 |
| Cruise -1,000 ft | -0.245112 | -0.235903 | -0.250177 | 0.014274 | 0.024966 |
| Load factor 0.72 | +0.505838 | +0.452212 | +0.466049 | 0.053626 | 0.062002 |
| Load factor 0.92 | -0.372081 | -0.346332 | -0.374603 | 0.028271 | 0.015079 |
| Reserve 1,000 kg | +0.253070 | +0.221845 | +0.234258 | 0.031226 | 0.034348 |
| Reserve 3,000 kg | -0.163164 | -0.158810 | -0.167004 | 0.008194 | 0.054028 |
| Ground definition a1000t70 | +0.008287 | +0.012515 | +0.012745 | 0.004459 | 0.017556 |

Seven of eight nonnominal **total** spans are smaller. The primary GS contrast
is still material, around +1.0 to +1.15 pp, but its observed span is **79.5%
smaller**. Load factor 0.92 is worse: its total span rises from 0.015079 to
0.028271 pp. All signs are preserved in these draws. This does not establish
variance reduction in repeated sampling, nor does it validate either airspeed
convention against actual flight burn.

### 3d. Components: narrower totals can hide wider lateral variation

| Scenario | New lateral span | Equal-cap lateral span | New vertical span | Equal-cap vertical span |
|---|---:|---:|---:|---:|
| GS instead of IAS-preferred | 0 | 0 | 0.145756 | 0.711869 |
| Cruise +1,000 ft | 0.026589 | 0.004041 | 0.009334 | 0.065411 |
| Cruise -1,000 ft | 0.009199 | 0.003801 | 0.023473 | 0.026898 |
| Load factor 0.72 | 0.000488 | 0.001407 | 0.053774 | 0.063409 |
| Load factor 0.92 | 0.000539 | 0.001426 | 0.027911 | 0.016485 |
| Reserve 1,000 kg | 0.000263 | 0.000818 | 0.030978 | 0.034097 |
| Reserve 3,000 kg | 0.000254 | 0.000837 | 0.007941 | 0.054532 |
| Ground definition a1000t70 | 0 | 0 | 0.004459 | 0.017556 |

Both altitude scenarios have **wider lateral spans**, despite narrower total
spans. The total alone would therefore overstate the improvement. The GS and
ground-definition effects remain entirely vertical in this model experiment;
their unchanged lateral term is structural, not evidence of independent
component precision.

**Conclusion:** the design is much less weight-concentrated and these draws
support better screening stability for the principal GS and total-altitude
contrasts, but it is not uniformly superior. Keep both experiments and do not
retune the allocation after these outcomes. The historical sampler remains the
default; the balanced mode is an explicit lab option.

## 4. Verification, boundaries and remaining work

The suite passes **110 tests normally and with Python `-O`**, including 42
uncertainty tests. New regressions cover exact quotas and ties, census/floor
boundaries, 100 varied allocation problems, impossible budgets, invalid or
duplicate cells, row-order invariance, cell expansion weights, nonnumeric and
non-finite stratification values (including nullable missing values), explicit
CLI combinations, and correctly labelled sample metadata. An integration
regression checks that both reference profiles propagate the allocation label
without changing the metrics for the same rows. The old sample format is
retained in equal-cap mode.

Three invalid CLI combinations were also run end-to-end: equal mode with a
total target, balanced mode without a target, and balanced mode with the old
per-cell cap flag. Each exited 1 before reading input files and wrote no JSON.
With the real input checksums enabled, requests for 1,047 and 1,833,128 rows
also exited 1 and wrote no JSON: the measured feasible interval is 1,048 to
1,833,127. Infeasible targets are rejected, not silently clipped to the
population or minimum. A balanced minimum is never recorded as an equal
per-cell cap.

This changes sampling, not the emissions model. The stored cruise altitude,
ground fractions, track anchor, calibration and explicit corrected-wind
reference remain as in the preceding experiment. Both reference replay checks
remain mandatory. Changed reference and scenario effects must still be read
separately. Model validity, receiver selection bias and actual airspeed error
are not established by stabilising a numerical experiment.

The recorded hashes of `decompose.py`, `excess_wind.py`, `emissions.py` and
`wind/era5.py` match the preceding experiment. Only the sampler and allocation
metadata change in the runner; the paired sensitivity calculation is unchanged.

The allocation is not Neyman/variance-optimal, fuel-size proportional, or fitted
to make a particular sensitivity small. Better allocations or internal
design-based variance estimates could be investigated later; privacy does not
prevent computing aggregate variance diagnostics. They should not be presented
as uncertainty about the physical truth of the emissions estimates.

The user's constraints did not prevent the checks reported here. They kept
the experiment off production and out of January release preparation. No
historical optimiser replacement, full-model revision, external data request
or paid access was necessary for this sampling comparison.

**Suggested next lab step, not implemented here:** estimate the sampling error
of each paired scenario contrast using internal within-stratum sufficient
statistics, finite-population corrections and the covariance of nominal and
scenario calculations. Cover total, lateral and vertical separately. This
would measure simulation/sampling precision conditional on the existing model,
not the physical uncertainty of CO2. The current aggregate files discard the
needed within-cell variances and covariances, so this requires an instrumented
rerun; they cannot be recovered from Kish ESS or three observed ranges.
Publish only overall diagnostics, not small-cell or per-flight outcomes.
This is preferable to changing the allocation again on the evidence of these
same three draws. External validation still awaits the separate provider work.

### Release isolation check

The pre-commit release-profile build in
`/tmp/co2gap-balanced-site-before-Xz35h4/site` verified all 13 exact frozen
headline values. All eleven requested artefacts match the **branch's** `site/`:
seven byte-for-byte, three HTML pages after normalising only the generated
timestamp, and sitemap after normalising only `lastmod`. This is not a claim
of equality to later editorial changes on `master` or to a freshly inspected
live deployment. Do not deploy this branch over those changes.

The same build-and-compare command is repeated after the local commit; its
receipt belongs in the delivery and project memory. No site, production data,
release manifest/headline, held-out registration or matching implementation
is part of this change.

## 5. Reproduction and provenance

The complete balanced sample command is in
[UNCERTAINTY.md](UNCERTAINTY.md#optional-balanced-allocation). Run the existing
full sensitivity command on each resulting private sample, with
`--reference-profile corrected-wind`, all nine original scenarios and all input
checksums enabled. Neither `--limit` nor `--skip-manifest-verification` is used
for a full run. Run the replications serially on this machine.

Sampling plan SHA-256 (recorded before the new runs):
`5196899b01f5cc5adcced27cb7752e1e39621cf109a7fcaaea49257527f0bb97`

Runner SHA-256:
`0a817be320f2e19941c018f6ed53296da7399de4ee2487e97119afb3f882e5f0`

Release manifest and scenarios remain respectively:
`db69266d5a4d9ce66f1d1f9bc4533cb31e6fd1b6d8e4fbcc600c53e6a5266a0c`
and `ab7e5e678959b0178d0620c8d228c9ae85f3b6f641929e65b23d5c706e9ea3f9`.

New private sample files and SHA-256:

* `/tmp/co2gap-balanced-sample-seed1.json`:
  `5e918a8e421431856ea06d7b921855d84efcde1f1e871bda3c59043b035d0a96`
* `/tmp/co2gap-balanced-sample-seed2.json`:
  `fcb3fe218f75b968989db993b403ccb2351634e5bfbc8cc6d0c45da6b5633c34`
* `/tmp/co2gap-balanced-sample-seed3.json`:
  `10d2f9789170cbafa86615e7586273a6a03a5bf3ac1b09f290d7d1755dbea2f4`

New aggregate sensitivity files and SHA-256:

* `/tmp/co2gap-balanced-sensitivity-seed1.json`:
  `01ee438f8512c1f980b0aee05d24589a204a08a5ad13b22db417b38cc70fe53a`
* `/tmp/co2gap-balanced-sensitivity-seed2.json`:
  `3b31d9cd8dd74838fa861a7670c829375b2bf561844818f1764be0a5344087cc`
* `/tmp/co2gap-balanced-sensitivity-seed3.json`:
  `8179a5bdb2508fc79f98256888f63e6ce611da51c204cb07283e4e861009eaa7`

Historical default regeneration: `/tmp/co2gap-balanced-legacy-recheck.json`,
SHA-256 `020bf2f60f8190ef063d4760fa1c0b5fa71f85b7ba69399f9587f40e868c7957`.
This equals the original seed-20260901 sample byte-for-byte.

The private orchestration script `/tmp/co2gap_run_balanced_sensitivity.py`
reads the recorded plan, generates/verifies the samples and runs all scenarios.
The private verifier `/tmp/co2gap_verify_balanced_sensitivity.py` checks the
output provenance, paired population, reference deltas and a separate
dataframe-join/`math.fsum` reconstruction of each frozen sample aggregate.
Neither script writes production data or publishes per-flight identifiers.

Site check wrapper: `/tmp/co2gap_balanced_site_check.sh before` or `after`;
it uses the README's release-profile inputs and
`/tmp/co2gap_sensitivity_site_compare.py` for the eleven-file comparison.
Runtime: Python 3.11.5, OpenAP 2.6.0, NumPy 2.4.6, SciPy 1.17.1,
pandas 3.0.5, PyArrow 25.0.0 and xarray 2026.7.0 in `../lab-venv`.
