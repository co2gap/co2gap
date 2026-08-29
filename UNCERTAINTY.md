# CO2 uncertainty programme

This is an experimental lab layer. It does not change a frozen release and it
does not put an error bar on the public site. Its first job is to keep unlike
objects unlike:

1. exact values for the flights that passed the release gate;
2. temporal-composition diagnostics;
3. finite-difference model sensitivities;
4. probability intervals, only after their input distributions have evidence.

The observed trajectory and its ideal/hybrid baselines are always perturbed as
one coupled calculation. Sampling their parameters independently would destroy
the shared error that can cancel in the gap and would overstate its uncertainty.

## Scope of v1

The estimands are the calibrated airborne real, ideal and gap tonnes, followed
by the uncalibrated ratio-of-sums total, lateral and vertical gaps used by the
site. They describe quality-gated flights in the named release. They are not an
estimate of all ECAC traffic unless the later selection-bias phase supports that
generalisation.

Taxi/APU, well-to-wheel emissions, contrails and NOx are outside this budget.
They are different quantities, not uncertainty on airborne tank-to-wake CO2.

`uncertainty-register.json` is the inventory of observations, parameters,
model choices, selection effects and temporal variability. A source may be:

* `quantified`: an existing range has been measured;
* `coverage_measured`: the observed retention is exact but its effect on the
  target estimand remains unbounded;
* `scenario_only`: executable alternatives exist but are not probabilities;
* `needs_evidence`: no numerical range is defensible yet;
* `out_of_scope`: explicitly excluded from this estimand.

The validator refuses to call a source quantified or coverage-measured when it
has no range.

## 1. Validate the design inputs

```bash
python lab/uncertainty.py validate
```

`uncertainty-scenarios.json` contains finite-difference steps for screening.
Values such as load factor 0.72/0.92 or a 1,000 ft baseline offset are test
steps, not lower/upper confidence bounds. The file is machine-marked
`diagnostic_only`, and the validator rejects any other status.

## 2. Exact release and temporal diagnostics

All generated uncertainty artefacts go to `/tmp`; per-flight rows never enter
git.

```bash
python lab/uncertainty.py release-summary \
  --release-manifest "$PWD/release-manifest.json" \
  --decomposition-dir /path/to/data/decomposition_ecac \
  --ground-dir /path/to/data/ground_share_ecac \
  --calibration "$PWD/data/calibration_ecac.json" \
  --iterations 2000 \
  --out /tmp/co2gap-uncertainty-release.json
```

This command verifies the release artefacts, reproduces the exact point
estimates, resamples whole days and reports leave-one-month-out and ground
definition ranges. Day resampling describes temporal composition. It is not
model uncertainty and not a confidence interval for the already observed
population.

## 3. Reproducible stratified sample

```bash
python lab/uncertainty.py sample \
  --release-manifest "$PWD/release-manifest.json" \
  --flights-dir /path/to/data/flights_ecac \
  --decomposition-dir /path/to/data/decomposition_ecac \
  --per-stratum 5 \
  --seed 20260901 \
  --out /tmp/co2gap-uncertainty-sample.json
```

Strata are aircraft type, distance band and track-coverage band. Equal
allocation keeps rare/difficult cells in the test; inverse sampling weights
expand the sample back to the release population. The manifest contains only
day and release-local flight id, but that is still per-flight data and remains
private. Expansion recovers the population size, not the exact headline: the
weighted nominal must be compared with `release-summary`, and scenario deltas
are the primary screening result. The output records the maximum weight and
Kish effective sample size so an apparently large row count cannot hide a
concentrated design.

## 4. Paired sensitivity

```bash
python lab/uncertainty.py sensitivity \
  --release-manifest "$PWD/release-manifest.json" \
  --flights-dir /path/to/data/flights_ecac \
  --decomposition-dir /path/to/data/decomposition_ecac \
  --ground-dir /path/to/data/ground_share_ecac \
  --era5-dir /path/to/data/era5_ecac \
  --calibration "$PWD/data/calibration_ecac.json" \
  --sample /tmp/co2gap-uncertainty-sample.json \
  --out /tmp/co2gap-uncertainty-sensitivity.json
```

For each flight and scenario the runner:

1. reconstructs the observed stored track;
2. recomputes observed fuel with the scenario's mass/airspeed parameters;
3. anchors nominal stored-track fuel to the frozen native-track CO2;
4. recomputes ideal and hybrid baselines with the same load, reserve and
   baseline-altitude scenario;
5. removes the selected stored ground-fuel share;
6. applies the same type calibration to real, ideal and hybrid;
7. discards the per-flight result and retains only weighted aggregates.

The current runner holds each definition's ground share fixed while mass and
reserve vary. Recomputing the share under every parameter draw is a named v2
task, not a hidden claim of v1.

`--limit N` exists only for a complete smoke run. Its output is marked as a
truncated sample and its population estimate is explicitly invalid.

## 5. Selection audit

```bash
python lab/uncertainty.py selection \
  --release-manifest "$PWD/release-manifest.json" \
  --flights-dir /path/to/data/flights_ecac \
  --decomposition-dir /path/to/data/decomposition_ecac \
  --out /tmp/co2gap-selection-audit.json
```

The command reads the durable flight tables before the analysis gate, rebuilds
all four predicates and requires the passing `(day, flight_id)` keyset to equal
the frozen decomposition. It emits only aggregate activity: flight count,
great-circle kilometres, flown kilometres and first-pass gate-to-gate CO2. The
last measure is an exposure proxy, not the published airborne inventory.

Independent failure totals deliberately overlap. A 4-bit failure mask provides
the exact partition, while the displayed cascade uses a declared order and is
diagnostic rather than causal. Day, aircraft-type, distance, reception-coverage
and maximum-gap groups retain the project's minimum of ten flights.

The historical denominator starts only after regional filtering, complete-leg
reconstruction, OpenAP type support and a successful first-pass fuel estimate.
Those upstream exclusions were not stored for the September release and cannot
be reconstructed honestly. Future daily runs add an aggregate selection funnel
to both parquet source contracts, with closed partitions checked before a day
is promoted. It contains no trace, aircraft or flight identifier.

This closes the count, distance and exposure accounting inside the observable
gate. It does **not** bound the headline bias: applying the full decomposition
to a flight rejected precisely because its track is unreliable would turn the
quality failure into a model input. Any correction requires an independent or
validated proxy for the missing outcome.

## Gates before a public interval

A public probabilistic interval remains blocked until:

* every dominant sensitivity has an evidence-backed distribution or remains a
  separately named structural scenario;
* global, aircraft-family and per-flight correlations are represented;
* the effect of quality-gate selection on the target estimands is bounded or
  corrected using independent evidence;
* selection before the durable pre-gate population is measured or bounded;
* interval coverage is checked against independent or held-out evidence;
* the cruise baseline issue is resolved or its structural range is published;
* an independent reviewer examines the register, propagation and language.

Until then the correct outputs are a point estimate, a sensitivity table, a
structural scenario range and a coverage statement.
