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

## 6. Within-gate selection stress

```bash
python lab/uncertainty.py selection-stress \
  --release-manifest "$PWD/release-manifest.json" \
  --flights-dir /path/to/data/flights_ecac \
  --decomposition-dir /path/to/data/decomposition_ecac \
  --ground-dir /path/to/data/ground_share_ecac \
  --calibration "$PWD/data/calibration_ecac.json" \
  --out /tmp/co2gap-selection-stress.json
```

This stress test only removes observations from the trustworthy side of the
gate. It never decomposes or imputes a rejected flight. Its nested scenarios
raise temporal coverage to 90%, 95% and 99%; cap the longest gap at 900, 600,
300 and 120 seconds; combine the moderate criteria; and remove the one, two,
four and ten lowest-retention days.

Every subset is reported twice. The raw result answers what the stricter
population says. The poststratified result restores the nominal distribution of
ideal CO2 across aircraft type and great-circle distance band, reducing the
part of the change caused merely by a different fleet or distance mix. Support,
maximum weight and Kish effective sample size are emitted with every result.

On the frozen release, requiring at least 95% coverage moves the total gap by
**+0.344 percentage points after poststratification**, of which +0.325 is
vertical. Limiting the longest gap to 300 seconds moves it by **+0.375 points**,
again almost entirely vertical. Removing the two worst-retention days moves it
by only **-0.001 points** after standardisation. The gradient therefore lives
inside track quality rather than in the aggregate weight of 24–25 March.

The 99%/120-second tails require maximum weights above 41 and are deliberately
reported as unstable stress cases. Even the moderate gradient is not a bound:
poststratification does not make quality random within a type/distance cell, and
it cannot establish the missing outcome on the rejected side.

## 7. Pre-registered independent selection sample

The durable pre-gate tables still contain the release-local keys, time window,
endpoint coordinates and type of flights rejected by the four quality checks.
They do not contain the raw dump or a second measurement of the trajectory.
The next defensible step is therefore split in two: draw the sample now, before
seeing a second-source outcome, and estimate only after an independent source
returns it.

```bash
python lab/uncertainty.py selection-validation-sample \
  --release-manifest "$PWD/release-manifest.json" \
  --flights-dir /path/to/data/flights_ecac \
  --decomposition-dir /path/to/data/decomposition_ecac \
  --per-stratum 3 \
  --target-sample 5000 \
  --top-types 12 \
  --seed 20260901 \
  --out /tmp/co2gap-selection-validation-sample.json \
  --match-out /tmp/co2gap-selection-validation-match-list.json
```

The design forms cells from the exact four-bit failure mask, distance band,
coverage band and the twelve most common aircraft types plus `OTHER`. It takes
at least three observations from each cell, then allocates the balance in
proportion to the remaining population. Simple random sampling without
replacement happens within every cell. The frozen frame contains 2,115,824
flights; 5,000 are selected across 843 cells, including 3,061 passing controls
and 1,939 rejected flights. The weights close exactly on the frame, the maximum
is 701.23 and their Kish effective sample size is 3,537.75.

`selection-validation-design.json` is the public, aggregate pre-registration.
It contains no flight row, time or endpoint, but freezes the parameters, exact
failure-mask partition and SHA-256 of both private outputs. The command verifies
those hashes on every regeneration and fails on a different draw.

The two outputs are deliberately different. The design file contains private
release keys, failure mask and weight. The matching list withholds all of those
and exposes only an opaque sample id, day, type, timestamps and endpoint
coordinates. Those fields can still identify a flight, so both files stay
outside git and only aggregate results may be published.

The independent source must return real, ideal and hybrid CO2 components under
the contract in `lab/selection-validation-outcomes.schema.json`, declare that
it is independent of adsb.lol, use no primary trajectory and confirm that the
matcher did not receive gate status. These declarations are recorded, not
proved, by code. A suitable source could be an independently collected
trajectory or an ANSP/operator record; another transformation of the same
adsb.lol trace would not satisfy the contract.

Every measured row must resolve to exactly one candidate and report departure
and arrival time offsets, endpoint distances and coverage of the independent
trajectory. It must pass the source's documented quality rule. These fields do
not prove a correct match, but prevent an ambiguous or visibly incomplete
second trace from silently becoming the answer.

```bash
python lab/uncertainty.py selection-validation \
  --sample /tmp/co2gap-selection-validation-sample.json \
  --match-list /tmp/co2gap-selection-validation-match-list.json \
  --outcomes /private/path/independent-outcomes.json \
  --out /tmp/co2gap-selection-validation-result.json
```

Every sampled id must be returned, including explicit `not_found`, `unusable`
or `source_error` states. Unless all 5,000 are measured, estimation is blocked:
the diagnostic is written but the command exits non-zero. Response weighting
would merely replace the original selection problem with a second unvalidated
model. With complete outcomes the estimator expands each
cell to its known population size and reports full-pre-gate minus gate-passing
ratios with finite-population, design-based standard errors. That interval is
conditional on the proxy and sample design. It contains neither proxy error nor
model uncertainty, does not cover upstream ingestion exclusions and cannot by
itself be attached to the public 12.1% figure.

## 8. Free OpenSky one-day audit

OpenSky's scientific dataset 11 contains a complete Trino-table snapshot for
1 March 2026, a day inside the frozen release. It is enough for a useful
external diagnostic without buying an API plan, but not for the complete
5,000-flight validation above: it covers one day, may share underlying
community receivers with adsb.lol and supplies ground speed rather than IAS.

`opensky-day-audit-design.json` freezes the 10,290-flight pre-gate census, the
four-bit failure partition, a matching whitelist that excludes gate status and
primary quality, mutual-nearest matching thresholds and source-only quality
rules. Private frames, source identifiers and state vectors remain in `/tmp`.
Alongside the public design, only the aggregate
`opensky-day-audit-result.json` is tracked.

The original registration is preserved at commit `75dfae4`. Its literal
sub-second maximum-segment-speed guard fired on sporadic coordinate contaminants
in OpenSky state snapshots and left only 172 outcomes. That was a guardian
failure, not a selection result. The public design records the result hash and
the post-pilot amendment: after deduplicating equal position times, source
states are represented by medians in fixed 60-second Unix bins. Sensitivity at
10, 20, 30 and 60 seconds was inspected before the amendment, so the amended
analysis is explicitly exploratory and cannot inherit the original
pre-registration label.

The complete amended run found 8,726 unique matches: 86.17% of primary-gate
passes but only 74.65% of rejected flights. Source quality and modelling then
left 7,615 passing controls and 829 rejected outcomes, respectively 83.99% and
67.78% of their original groups. No missing outcome is imputed.

Conditional on both matching and source quality, the OpenSky proxy gap is
8.17% for primary-gate passes and 6.65% for rejected flights, a rejected-minus-
passed contrast of -1.51 percentage points. The aggregate hides opposite
failure-mask results: primary coverage failures show 11.96%, while unresolved-
endpoint failures show 5.94%. The latter group is much larger and drives the
combined contrast.

That contrast is not a correction. On the same 7,615 controls, the frozen
airborne primary calculation is 12.77%, versus 8.17% for the OpenSky
takeoff-to-landing ground-speed proxy: -4.61 points, split into -1.62 lateral
and -2.98 vertical. The source proxy therefore has a large measurement/model
offset even after comparing like with like. Together with differential
matching and possible receiver overlap, this leaves the full-release headline
bias unbounded. The result is nevertheless informative: it demonstrates
failure-mask heterogeneity, proves that a free external-day workflow is
practical, and specifies what a second held-out day or Wingbits extract must
improve.

The reproducible stages are separate so every private boundary is inspectable:

```bash
python lab/opensky_day_audit.py validate
python lab/opensky_day_audit.py prepare \
  --flights-dir /path/to/data/flights_ecac \
  --decomposition-dir /path/to/data/decomposition_ecac \
  --ground-dir /path/to/data/ground_share_ecac \
  --match-out /tmp/co2gap-opensky-primary.json \
  --private-out /tmp/co2gap-opensky-private.json
python lab/opensky_day_audit.py match \
  --primary /tmp/co2gap-opensky-primary.json \
  --source-flight /tmp/opensky-flight-part-1.parquet \
  --source-flight /tmp/opensky-flight-part-2.parquet \
  --out /tmp/co2gap-opensky-matches.json
python lab/opensky_day_audit.py extract \
  --matches /tmp/co2gap-opensky-matches.json \
  --s3-anonymous-prefix data-samples/trino-tables/state_vectors \
  --out /tmp/co2gap-opensky-state-vectors.parquet
python lab/opensky_day_audit.py partition \
  --matches /tmp/co2gap-opensky-matches.json \
  --state-vectors /tmp/co2gap-opensky-state-vectors.parquet \
  --out-dir /tmp/co2gap-opensky-state-buckets
python lab/opensky_day_audit.py audit \
  --private /tmp/co2gap-opensky-private.json \
  --matches /tmp/co2gap-opensky-matches.json \
  --state-vectors /tmp/co2gap-opensky-state-buckets \
  --era5-dir /path/to/data/era5_ecac \
  --out /tmp/co2gap-opensky-audit-result.json
```

## 9. Failure-mask selection sensitivity

The OpenSky day does not estimate the missing release outcomes, but its
failure-mask contrasts can be used as a transparent stress scale. The design
was frozen in `selection-sensitivity-design.json` before the implementation.
It transfers only each mask's difference from the OpenSky passing controls;
the -4.61-point OpenSky-versus-primary level offset is never transferred.

First rebuild the verified aggregate selection audit exactly as in section 5,
then run:

```bash
python lab/uncertainty.py selection-sensitivity \
  --selection-audit /tmp/co2gap-selection-audit.json \
  --out /tmp/co2gap-selection-sensitivity.json
```

The calculation weights failure masks by their first-pass gate-to-gate CO2
exposure, not by flight count. That is still only an exposure proxy: rejected
flights have neither a trustworthy airborne ideal denominator nor a valid
decomposition. Masks with an observed OpenSky proxy use their difference from
the `0000` controls; compound masks inherit the closest observed mechanism;
the small unsupported short-sector/flown-distance masks receive an explicit
zero contrast. Every mapping and its reason is machine-readable in the design.

Three declared amplitudes use half, once and twice the observed one-day mask
contrast. At each amplitude the runner reports the signed OpenSky direction and
two coherent adverse directions in which every whole lateral/vertical vector
lowers or raises the total gap:

| stress | signed total shift | adverse total shift |
|---|---:|---:|
| prudente (0.5x) | -0.007 pp | -0.184 .. +0.184 pp |
| centrale (1x) | -0.014 pp | -0.369 .. +0.369 pp |
| severo (2x) | -0.027 pp | -0.737 .. +0.737 pp |

These are sensitivity shifts around the frozen 12.09185% passed-flight
headline, not endpoints of an uncertainty interval. The central signed result
is especially easy to misuse: **96.3% of the gross mask contributions cancel**.
Coverage-only failures push in the opposite direction to unresolved-endpoint
failures. A small net shift therefore does not demonstrate small selection
uncertainty.

The central adverse contribution ranks the next external validation work.
Coverage-only mask `0100` supplies 47.9% of its total magnitude, unresolved
endpoints `1000` another 46.5%, and their overlap `1100` 5.4%. Together the
first two account for 94.4%, so a held-out Wingbits or second-source extract
should prioritise those two groups rather than sample all rejected masks
uniformly. This ranking is the permitted decision use of the diagnostic; the
release headline remains unchanged and unbounded.

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
