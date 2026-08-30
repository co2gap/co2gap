# Final internal check: sampling precision of paired sensitivities

This closes the internal numerical follow-up to
[BALANCED-SENSITIVITY.md](BALANCED-SENSITIVITY.md). It does not prepare January,
revise the frozen release, change the model, or supply a physical CO2 interval.

## Scope and stopping rule, recorded before the instrumented runs

Use the same three balanced samples (seeds 20260901/02/03), all nine scenarios,
and the explicit corrected-wind nominal. Do not redraw, increase the sample,
retune allocation, change scenarios, or relax the reference guards in response
to the new precision estimates. Preserve the previous aggregate results exactly.

The check is complete when the following have been measured and documented:

1. All three complete runs reproduce their previous metrics and reference checks.
2. The variance calculation passes analytic fixtures, exhaustive sampling on a
   small known population, and an independent reconstruction from private moments.
3. Every total/lateral/vertical scenario contrast has a sampling standard error
   and a diagnostic of concentration of its estimated variance across strata.
4. Low precision or unstable variance estimates are reported as limitations,
   not triggers for another automatic round of sampling/model development.

No pass/fail precision threshold, confidence interval, p-value, or distribution
of physical errors is introduced. The remaining synthesis is reporting, not
another technical implementation by default.

## Method

The finite population and each scenario's modelled flight values are fixed.
Randomness here means selecting a different SRS without replacement inside each
of the existing strata. It does not mean a different weather world, aircraft
mass, flight population, receiver network, or period of the year.

For each scenario, expand the uncalibrated observed, ideal and hybrid CO2
totals as `T = sum_h N_h * mean_h(R, I, H)`. The three gap functions are
`100*(R-I)/I`, `100*(H-I)/I`, and `100*(R-H)/I`. The derivatives include the
random ideal denominator; these are ratios of expanded totals, not averages
of per-flight percentages or calibrated-tonne ratios.

Let `g_s` be a gap's gradient evaluated at the scenario's estimated totals.
Within each stratum, calculate the paired linearised difference
`z_i = g_s * x_si - g_0 * x_0i`. The variance estimate is

`V(delta_s) = sum_h N_h*(N_h-n_h)/n_h * sample_variance_h(z)`.

The same construction gives level precision and corrected-minus-frozen
reference precision. The three-component covariance matrix is retained:
component standard errors do not add, even though the point estimates do.
The finite-population multiplier is the usual stratified-SRS total variance
formula. [Penn State STAT 506, stratified sampling](https://online.stat.psu.edu/stat506/Lesson06)
documents it; [survey's ratio-estimation documentation](https://r-survey.r-forge.r-project.org/survey/html/svyratio.html)
also distinguishes combined ratios from separate within-stratum ratios and
supports covariance matrices for ratios. The paired contrast here applies the
gradient to both scenarios jointly.

This is a **first-order Taylor approximation** for nonlinear ratios, not an
exact finite-sample variance, a bias correction or a coverage guarantee.
The experiment remains exploratory and post-pilot; adding standard errors does
not retroactively make its three draws a confirmatory study.
Small non-census cells with two observations can have unstable estimated
variances. A census, including a singleton census, contributes zero sampling
variance. A non-census singleton is rejected, never treated as zero variance.

The frozen release statistic uses **all 1,833,127 observed passing flights**.
The errors estimated here concern an estimator using 2,549 sampled flights,
not the already known full-population statistic. They must not be attached to
the published 12.1% as a `+/-`. Having an observed-population census removes
this source of sampling error, not selection bias or physical model error.

The runner centres flight values in memory and subtracts paired influence
values before squaring. This avoids subtracting two large variances and losing
the covariance that can cancel in a contrast. Values are discarded before
returning; only overall estimates and covariance matrices enter the aggregate.

## Contracts and private audit

`--sampling-precision` is optional. Without it the existing output contract and
metrics remain unchanged. With it, inputs and sample provenance must be
verified, no `--limit` is allowed, and the declared sample is regenerated from
the verified frame. Matching weights alone do not authenticate a sample design.
Every requested flight must succeed in every scenario; partial runs cannot
produce a precision estimate.

`--precision-audit-out` optionally writes **private** within-stratum means and
centred cross-products, bound to input and implementation hashes. These moments
have no flight keys but can reveal small-cell outcomes: they must remain outside
the repository and must not be published. Existing audit files are not overwritten,
and the audit path cannot also be the aggregate-output path. The aggregate
records the audit SHA-256, not its contents.

The three completed moment files were restricted to mode **0600** after
generation, verified explicitly. For reproduction the documented command sets
`umask 077` before creating new output files. The ordinary JSON writer follows
the process umask; labelling a file private is not itself access control.

## Results

All three full runs completed on 30–31 August 2026: **2,549 paired flights,
nine scenarios, 534 strata and 1,833,127 expanded population rows** in each.
There were no common-population failures or truncations. The draws regenerated
exactly from the verified frame. All previous aggregate metrics and contract
fields are identical to the preceding experiment; only implementation hashes
and the new precision object differ. Both stored-wind baseline replays retain
their exact zero-kg reconstruction error.

Measured wall times were **757.11 / 745.83 / 623.77 seconds** (35.45 minutes in
total). These are observations, not a controlled performance comparison.

The independently implemented matrix calculation agrees with all level,
contrast and reference covariance matrices: maximum absolute discrepancy
**1.483e-15 pp²**; maximum discrepancy in the largest-stratum variance fraction
**2.239e-10**. Every new estimate was also checked against the corresponding
original uncalibrated metric within `1e-12 pp`, not against calibrated tonnes.

### What the check establishes

* The total ground-speed diagnostic is **+1.006 to +1.152 pp**, with sampling
  SE **0.0879–0.0952 pp**. Its relative SE is **7.81–9.45% of that contrast**,
  not a percentage error on physical emissions.
* The total altitude contrasts have SE about **0.010 pp**: the -1,000 ft
  scenario shifts the total by -0.250 to -0.236 pp; +1,000 ft shifts it by
  +0.947 to +0.977 pp. Component precision is reported separately below.
* The ground-definition contrast is much smaller, +0.0083 to +0.0127 pp.
  Its SE is 0.00208–0.00229 pp, **17.25–25.15% of the estimated contrast**.
  Its extra decimal places should not be interpreted as accuracy.
* The tiny corrected-minus-frozen reference change is poorly characterised
  by these samples. In each of the first two draws, **more than 99.998% of
  its estimated total variance comes from one stratum**, and SE is roughly
  the size of the estimated total change. The third sample has no fuel-baseline
  change at the existing model discretisation. This does not establish that
  the reference change vanishes on the unsampled population.
* The design has **29 census strata** and **283 non-census strata with only
  two observations**, out of 505 non-census strata. Estimated variances can
  therefore be unstable. For example, one stratum supplies 38.74% of total
  contrast variance for reserve +1,000 kg in the first draw, versus 13.03%
  in the second and 34.47% in the third. This is a variance diagnostic,
  not that stratum's share of emissions or of the point estimate.

These are descriptive measurements, not newly chosen precision gates. They
close the requested internal check without tuning allocation or scenarios.

### All total contrasts

Each cell is **effect (sampling SE)**; both numbers are percentage points.
Six decimal places are retained for reproducibility, not physical accuracy.
The nominal-minus-itself contrast and SE are exactly zero and are omitted.

| Scenario | 20260901 | 20260902 | 20260903 |
|---|---:|---:|---:|
| Load factor -0.10 | +0.505838 (0.030714) | +0.452212 (0.034709) | +0.466049 (0.031657) |
| Load factor +0.10 | -0.372081 (0.022087) | -0.346332 (0.023034) | -0.374603 (0.023973) |
| Reserve -1,000 kg | +0.253070 (0.019060) | +0.221845 (0.015673) | +0.234258 (0.023867) |
| Reserve +1,000 kg | -0.163164 (0.014446) | -0.158810 (0.013370) | -0.167004 (0.017789) |
| Cruise altitude -1,000 ft | -0.245112 (0.009980) | -0.235903 (0.009968) | -0.250177 (0.009930) |
| Cruise altitude +1,000 ft | +0.963743 (0.010234) | +0.947095 (0.009566) | +0.977459 (0.010223) |
| Observed ground-speed diagnostic | +1.152206 (0.089935) | +1.023600 (0.087900) | +1.006450 (0.095158) |
| Ground definition a1000t70 | +0.008287 (0.002084) | +0.012515 (0.002159) | +0.012745 (0.002293) |

### All lateral contrasts

| Scenario | 20260901 | 20260902 | 20260903 |
|---|---:|---:|---:|
| Load factor -0.10 | -0.007995 (0.000490) | -0.007847 (0.000434) | -0.007507 (0.000440) |
| Load factor +0.10 | +0.009196 (0.000499) | +0.009016 (0.000439) | +0.008657 (0.000446) |
| Reserve -1,000 kg | -0.006110 (0.000255) | -0.006358 (0.000216) | -0.006095 (0.000234) |
| Reserve +1,000 kg | +0.006491 (0.000258) | +0.006715 (0.000219) | +0.006462 (0.000236) |
| Cruise altitude -1,000 ft | +0.087207 (0.006872) | +0.082770 (0.007179) | +0.091969 (0.006579) |
| Cruise altitude +1,000 ft | -0.072158 (0.007145) | -0.094365 (0.007234) | -0.067776 (0.007213) |
| Observed ground-speed diagnostic | +0.000000 (0.000000) | +0.000000 (0.000000) | +0.000000 (0.000000) |
| Ground definition a1000t70 | +0.000000 (0.000000) | +0.000000 (0.000000) | +0.000000 (0.000000) |

### All vertical contrasts

| Scenario | 20260901 | 20260902 | 20260903 |
|---|---:|---:|---:|
| Load factor -0.10 | +0.513833 (0.030725) | +0.460059 (0.034719) | +0.473556 (0.031677) |
| Load factor +0.10 | -0.381277 (0.022092) | -0.355349 (0.023049) | -0.383260 (0.023983) |
| Reserve -1,000 kg | +0.259181 (0.019074) | +0.228202 (0.015683) | +0.240353 (0.023881) |
| Reserve +1,000 kg | -0.169655 (0.014448) | -0.165525 (0.013374) | -0.173466 (0.017800) |
| Cruise altitude -1,000 ft | -0.332318 (0.009315) | -0.318673 (0.009015) | -0.342146 (0.009107) |
| Cruise altitude +1,000 ft | +1.035901 (0.009131) | +1.041459 (0.008600) | +1.045235 (0.009192) |
| Observed ground-speed diagnostic | +1.152206 (0.089935) | +1.023600 (0.087900) | +1.006450 (0.095158) |
| Ground definition a1000t70 | +0.008287 (0.002084) | +0.012515 (0.002159) | +0.012745 (0.002293) |

Ground-speed and ground-definition changes leave the ideal and hybrid
baselines unchanged. Their lateral contrast and SE are therefore structurally
zero; their total and vertical contrasts have the same SE. This is not a
general rule for altitude, load or reserve scenarios. Component SEs cannot
be added: the overall JSON preserves the full covariance matrices.

### Relative precision and concentration, total contrasts

Each cell is **SE / absolute effect (%) ; largest-stratum share of estimated
variance (%)**. The slash below separates these two diagnostics, not a
division of one percentage by another.

| Scenario | 20260901 | 20260902 | 20260903 |
|---|---:|---:|---:|
| Load factor -0.10 | 6.07% / 16.91% | 7.68% / 34.19% | 6.79% / 18.34% |
| Load factor +0.10 | 5.94% / 16.33% | 6.65% / 30.88% | 6.40% / 19.18% |
| Reserve -1,000 kg | 7.53% / 22.41% | 7.06% / 10.67% | 10.19% / 31.84% |
| Reserve +1,000 kg | 8.85% / 38.74% | 8.42% / 13.03% | 10.65% / 34.47% |
| Cruise altitude -1,000 ft | 4.07% / 9.98% | 4.23% / 10.42% | 3.97% / 11.25% |
| Cruise altitude +1,000 ft | 1.06% / 9.15% | 1.01% / 6.86% | 1.05% / 9.69% |
| Observed ground-speed diagnostic | 7.81% / 5.88% | 8.59% / 11.53% | 9.45% / 13.92% |
| Ground definition a1000t70 | 25.15% / 30.63% | 17.25% / 14.63% | 17.99% / 21.28% |

### Frozen sample levels and their known sampling discrepancies

Here the first number is a **level in percent**, while the parenthesised SE
is in **percentage points**. These are sample estimates of a known observed
population, not replacements for its exact full-population values.

| Metric | 20260901 | 20260902 | 20260903 |
|---|---:|---:|---:|
| Total | +12.281055 (0.165953) | +12.065665 (0.170717) | +12.195406 (0.164556) |
| Lateral | +7.713775 (0.111273) | +7.592998 (0.103433) | +7.409232 (0.099495) |
| Vertical | +4.567280 (0.104072) | +4.472668 (0.116109) | +4.786174 (0.113754) |

The following cells show **sample minus exact population (pp) / that
discrepancy divided by SE**. The full-population reference remains
12.091846635657904% total, 7.505740825238055% lateral and 4.586105810419848%
vertical. Three realised discrepancies are not a validation of interval
coverage or of the physical model.

| Metric | 20260901 | 20260902 | 20260903 |
|---|---:|---:|---:|
| Total | +0.189209 / +1.140 | -0.026181 / -0.153 | +0.103560 / +0.629 |
| Lateral | +0.208035 / +1.870 | +0.087257 / +0.844 | -0.096509 / -0.970 |
| Vertical | -0.018826 / -0.181 | -0.113438 / -0.977 | +0.200068 / +1.759 |

### Reference change, separate from scenario sensitivity

Each cell is again **effect (sampling SE)** in percentage points, for
corrected nominal minus frozen on the same sampled flights.

| Metric | 20260901 | 20260902 | 20260903 |
|---|---:|---:|---:|
| Total | -0.000305 (0.000304) | -0.000261 (0.000261) | +0.000000 (0.000000) |
| Lateral | -0.000084 (0.000370) | +0.000254 (0.000272) | +0.000000 (0.000000) |
| Vertical | -0.000221 (0.000227) | -0.000516 (0.000365) | +0.000000 (0.000000) |

The third draw's total and vertical SEs are approximately `2e-16 pp`,
numerical roundoff, and its lateral SE is exactly zero. The displayed zeros
must not be promoted to evidence of exact population-level reference
invariance. Relative SE is undefined when the estimated contrast is zero;
the JSON records `null` instead of dividing by zero.

### Closing decision

The bounded internal statistical task is **complete**. The main scenario
effects are accompanied by measured conditional sampling precision; fragile
small or rare effects are explicitly labelled. This report supplies the closing
synthesis rather than opening another technical step. No new sampling,
allocation tuning or model revision is required by this result.

Physical accuracy, historical upstream selection and independent source
validation are still unresolved; they were never the endpoint of this check.
Wingbits/OpenSky evidence remains a separate workstream. No public interval
around 12.1%, publication, or January release work is authorised here.

## Verification and interpretation boundaries

The full regression suite passes **124/124 tests**, both normally and with
Python assertions disabled (`-O`). The new tests include:

* An analytic paired contrast with variance **112.5 pp²**; incorrectly treating
  nominal and scenario as independent would give **812.5 pp²**. This exercises
  both the finite-population correction and covariance cancellation.
* Exhaustive enumeration of all **18** possible samples from two small strata:
  the average estimated variance equals the actual sampling variance when the
  denominator is constant. This does not assert exactness for nonlinear ratios.
* Finite-difference checks of the ratio Jacobian, including the ideal denominator;
  an independent paired-influence calculation with a changing denominator; and
  invariance when all fuel values are rescaled by `1e-6` or `1e6`.
* Zero contrasts for identical scenarios, zero sampling variance for censuses,
  covariance additivity, and rejection of incomplete pairs, non-census
  singletons, invalid weights/counts/values, malformed scenario sets and
  altered declared draws.
* Both reference profiles preserve existing aggregates with the optional
  calculation enabled; private-audit hashes close and overwrites are refused.

Four actual CLI combinations also exit **1**, before reading the intentionally
absent inputs and without creating an output: precision with skipped
verification, precision with a limit, an audit without precision, and a shared
audit/aggregate path. The design/register validation command exits **0**.

These checks establish arithmetic, pairing and contract behaviour. They do not
validate physical fuel values, the representativeness of ADS-B coverage, a
probability distribution for scenario choices, or coverage of a confidence
interval. Nor does a small estimated sampling error prove that the model is
correct: an entire census can have zero sampling error and systematic error.

### Effect of the user's constraints

The constraints did **not** prevent this statistical check. Calculating private
within-cell moments is compatible with publishing only aggregate diagnostics;
read-only production data and scratch outputs suffice. No paid/provider data,
release rebuild of the model, new allocation or January engineering work was
needed. Freezing the experiment prevents outcome-driven tuning, not estimation
of its precision. There is therefore no constraint that needs relaxing to close
this step.

External validation, upstream selection and evidence for model-error
distributions remain separate evidence gaps. They must not be presented as
things this conditional standard error has solved.

## Reproduction

Use the complete [conditional sampling precision command](UNCERTAINTY.md#conditional-sampling-precision),
with the same balanced sample and `--reference-profile corrected-wind`.

Run seeds 2 and 3 serially in the same way, using new output paths. All source
data, held-out registrations, matching rules, scenarios and release headlines
remain unchanged. No provider access, paid data or external model validation
is required for this conditional sampling calculation.

The scope/stopping-rule document was recorded before the first instrumented
run with SHA-256
`6deead666fee6063b0d014e43f4e0aa3a2223e4ff6bce7f66b45f266c8a6921a`.
That is the **pre-results** version, not a checksum of this subsequently
completed report. The existing exploratory sampling plan and all three sample
hashes are documented in [BALANCED-SENSITIVITY.md](BALANCED-SENSITIVITY.md).

Implementation hashes held fixed throughout the instrumented experiment:

* `lab/uncertainty.py`:
  `b0d10bb1d6c0df7d4184de471d6ad783b237e193d3aa012c01bd3e1379b548a7`.
* `lab/sampling_precision.py`:
  `0c7aef3cd3fbdbdae031e80353843c5712dab8b5d2c814e7ce5ac5d552c77a6a`.

The private orchestration script is `/tmp/co2gap_run_sampling_precision.py`.
The private verifier is `/tmp/co2gap_verify_sampling_precision.py`; it does
not import the new estimator or sensitivity runner. It first constructs the
joint covariance matrix of expanded raw totals from the private moments, then
applies separately derived ratio Jacobians. This differs from projecting each
centred flight first in the runner. It checks the full covariance matrices,
not only their diagonals, and verifies provenance and previous output fields.

Aggregate result files (overall diagnostics only):

* `/tmp/co2gap-sampling-precision-seed1.json`:
  `4b57b08a211e42c091c918a56eda6647119ed42d93896d14e2723c9a2988ea2c`.
* `/tmp/co2gap-sampling-precision-seed2.json`:
  `5145cd5f8c3f01dd13b4a671f970533d03b935a002297852d575110ff5eab89e`.
* `/tmp/co2gap-sampling-precision-seed3.json`:
  `28ab395e31fd2cd25a117663d64b20ddf8ba734eda715c66fc923403c1055528`.

Private moment files — **do not publish**:

* `/tmp/co2gap-sampling-moments-seed1.json`:
  `07cca117cf2185d72bf4086c08ff6bba34f3187159ab927c809062b33ced15b9`.
* `/tmp/co2gap-sampling-moments-seed2.json`:
  `da30ed6684c53c30e39e6ecd51fc741b305ee6f66b32ef4c45b6c079d0fd716e`.
* `/tmp/co2gap-sampling-moments-seed3.json`:
  `03eca38aefb315380af53d727f7d7bab1f1a33dde3bfcec622da3e615a2b5aa2`.

The report tables are generated and checked against the aggregate files using
`/tmp/co2gap_sampling_precision_tables.py`. The scratch files are not durable
archival storage; their checksums, source identities and reproduction commands
are recorded here so their loss does not imply a different experiment.

## Release isolation

The pre-commit build in `/tmp/co2gap-precision-site-before-s05UGo/site` used the
README release-profile command with output redirected to `/tmp`. It verified
all **13 exact frozen headlines** and matched all **11** requested artefacts
against the **branch's** `site/`: seven byte-for-byte; `data.html`,
`methodology.html` and `faq.html` after normalising only the generated timestamp;
`sitemap.xml` after normalising only `lastmod`.

The same command is repeated after the local commit, with its receipt recorded
in the delivery and project memory. The wrapper is
`/tmp/co2gap_precision_site_check.sh before` or `after`; the comparator is
`/tmp/co2gap_sensitivity_site_compare.py`.

This is not a comparison against the later editorial state of `master` or a
live deployment. `master` and `origin/master` remain `f646ca4`; do not deploy
this branch over those changes. No production data, site prose, release inputs,
headlines, held-out protocol, matching code or physical model was changed.
