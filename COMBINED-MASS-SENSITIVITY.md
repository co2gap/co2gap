# Combined-mass structural sensitivity

## Outcome

The external type/distance mass pattern changes the co2gap total gap by
**+0.223 percentage points** in the registered primary run. The two unchanged
replications give **+0.227** and **+0.163** points. Almost all of the change is
vertical: the three vertical effects are +0.245, +0.248 and +0.183 points,
while the lateral effect is small and stable at -0.022 to -0.021 points.

This is a structural sensitivity, not a correction or probability interval.
It leaves the September release, public site and headline unchanged.

## What was transferred

The source is the completed external TOW comparison in
[`TOW-VALIDATION.md`](TOW-VALIDATION.md). Its 77 retained aircraft-type by
flown-distance cells cover exactly 93.9765% of eligible release flights. For a
supported cell the scenario adds

`(PRC cell mean TOW/MTOW - co2gap cell mean initial mass/MTOW) * OpenAP MTOW`

to the combined non-trip mass term. It then iterates trip fuel again. The same
cell adjustment is applied to the observed trajectory, great-circle ideal and
flown-distance hybrid. A cell outside the external support gets zero
perturbation and remains in every scenario; no type, family or distance
extrapolation is allowed.

This deliberately does **not** turn the residual into a corrected load factor.
One take-off weight observes payload, reserve and trip fuel together and cannot
identify their separate distributions.

The propagation design was committed as `98a694e` before any CO2 scenario
outcome was calculated. It records the chronology honestly: the external TOW
cell residuals were already known, but their effect on co2gap was not. The
implementation was then frozen as `a8c16dc` before the three full registered
runs.

## Results

Every run used one of the three existing balanced samples: 2,549 flights,
534 sampling strata and expansion to 1,833,127 release flights. Every source
manifest was verified, the draw was regenerated from the release frame and
both scenarios succeeded for all flights.

| seed | requested mass, pp MTOW, all flights | realised observed mass, pp MTOW | total gap, pp | lateral, pp | vertical, pp | conditional SE total, pp |
|---:|---:|---:|---:|---:|---:|---:|
| 20260901 primary | -1.145 | -1.146 | **+0.223** | -0.022 | +0.245 | 0.025 |
| 20260902 | -1.110 | -1.112 | **+0.227** | -0.021 | +0.248 | 0.033 |
| 20260903 | -1.130 | -1.134 | **+0.163** | -0.021 | +0.183 | 0.025 |

The simple three-run mean, reported only as a descriptive replication summary,
is +0.204 points. The observed range +0.163 to +0.227 is not a confidence
interval or a physical uncertainty bound. Each conditional standard error
comes from the already registered stratified finite-population calculation and
does not include model, transfer or external-label uncertainty.

The existing fixed-mass model is heavier than the PRC reference by 1.209 points
within the external common cells. The three samples reconstruct a supported-
cell shift of -1.182 to -1.219 points. Once unsupported cells are explicitly
left nominal, the all-flight requested shift becomes -1.110 to -1.145 points.
The realised observed shifts are -1.112 to -1.146 points; re-iterated trip fuel
therefore changes the requested perturbation only slightly.

The MTOW cap is not driving the result. Depending on trajectory and sample, it
affects 20 to 24 sampled cases and only about 0.010% to 0.013% of expanded
weight. No scenario fell below OEW and no flight was removed.

## Why lower mass raises the gap ratio

In the primary run the structural pattern changes the sample-expanded calibrated
totals as follows:

| quantity | nominal, tonnes | scenario, tonnes | change, tonnes |
|---|---:|---:|---:|
| observed CO2 | 23,395,043 | 23,373,420 | -21,624 |
| ideal CO2 | 20,859,969 | 20,796,035 | -63,934 |
| excess CO2 | 2,535,074 | 2,577,385 | **+42,310** |

Lower mass reduces both sides, but the synthetic ideal falls more than the
anchored observed trajectory in this harness. The denominator therefore falls
and the difference grows. Across the other two samples observed CO2 changes by
-16.8 to -32.9 thousand tonnes, ideal by about -61.0 to -61.4 thousand tonnes
and the excess by +28.5 to +44.2 thousand tonnes.

This difference is precisely why an absolute-fuel mass error cannot simply be
copied into the inefficiency percentage. Much of the shared mass dependence
cancels; the remainder is structured and mostly vertical.

## Interpretation

The fixed 0.82 load factor plus 2,000 kg reserve is not a catastrophic source
of error in the fleet-wide *gap*. Replacing its cell means with the external
pattern moves a roughly 12% gap by about two-tenths of a percentage point in
these three samples. That is material enough to keep as a named structural
scenario, but too small and too weakly transferable to justify rewriting the
public headline.

The result also sharpens the model-development question. A richer mass model
could improve absolute fuel estimates and recover the observed dispersion, but
the benefit for the real-versus-ideal gap is much smaller because the same mass
model is used on both sides. Future development should therefore be judged
separately for absolute CO2 and for the inefficiency ratio.

## Limits that remain

* PRC describes selected participating airlines in 2022, not the full 2026
  ECAC population. Type and distance do not control airline, cabin, variant,
  season, route or operating policy.
* The two sources derive flown distance through different processing chains.
* Unsupported cells, 6.02% of the exact release population, stay nominal. This
  is explicit missing support, not evidence that their mass error is zero.
* The stored nominal ground-fuel share is held fixed. Its dependence on mass is
  not recomputed in this harness.
* The corrected-wind nominal is experimental. It is not the frozen public
  release.
* OpenAP model error, engine/variant mapping, cruise baseline error and upstream
  flight selection remain separate sources.
* Conditional sampling SEs do not cover external-label error or transfer from
  2022 participating airlines to 2026.

Consequently `load_factor` and `reserve_fuel` remain `scenario_only`. The new
combined-mass pattern is also `scenario_only`; it is not a distribution.

## Guards exercised

The implementation adds a laboratory-only MTOW-fraction hook whose default is
zero. The zero-hook result is exactly equal to the old call. Positive and
negative tests verify signed mass and fuel responses in both vector and scalar
integrators, while the decomposition test verifies the same fraction reaches
both baselines.

The design validator checks source hashes, cell arithmetic, the 100-row floor,
77-cell identity, exact coverage, scenario scales, non-extrapolation policy,
output non-claims and the three registered sample hashes. Tests deliberately
changed the source hash, output policy, unsupported-cell policy, scenario scale
and sample population; each was rejected. A byte-different but semantically
identical sample also exited non-zero before creating output, and the wrong
reference profile did the same.

The full suite passes **142/142** normally and **142/142** under `python -O`.
An independent verifier reconstructed the support and requested mass directly
from the private samples and release decomposition, reconstructed all runner
totals from the private within-stratum moments and checked hashes, modes and
total = lateral + vertical. Its SHA-256 is
`6d492ee954c6ad1627d351df9d99e6a55614608a82989aa68302190a3108a081`.

All six full-run files in `/tmp` have mode 0600. The three moment files can
expose small-cell outcomes and must never be published. Their hashes and the
aggregate hashes are preserved in
[`combined-mass-sensitivity-result.json`](combined-mass-sensitivity-result.json).

## Reproduction

Run under an environment with the exact lab lock and a restrictive umask. Use
each of the three registered sample files in turn; do not redraw them:

```bash
umask 077
python lab/uncertainty.py sensitivity \
  --release-manifest release-manifest.json \
  --flights-dir /path/to/data/flights_ecac \
  --decomposition-dir /path/to/data/decomposition_ecac \
  --ground-dir /path/to/data/ground_share_ecac \
  --era5-dir /path/to/data/era5_ecac \
  --calibration data/calibration_ecac.json \
  --sample /tmp/co2gap-balanced-sample-seed1.json \
  --scenarios combined-mass-sensitivity-design.json \
  --reference-profile corrected-wind \
  --sampling-precision \
  --precision-audit-out /tmp/co2gap-combined-mass-moments-seed1.json \
  --out /tmp/co2gap-combined-mass-seed1.json
```

Repeat with the registered seed-2 and seed-3 paths. A complete run may not use
`--limit` or `--skip-manifest-verification`.

## Stopping decision

This phase is complete. Do not tune the 77 cells, smooth the residuals, add an
arbitrary half-scale scenario or convert the three runs into a probability
range. The next evidence-bearing physical-model candidate is the PRC/ACARS fuel
dataset, whose OpenAP-assisted unit filtering must be treated as a circularity
caveat. The independent flight-selection programme remains separately blocked
on Wingbits or OpenSky access.
