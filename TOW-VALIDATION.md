# External take-off-mass validation

## Outcome

The fixed co2gap mass assumption is **close in fleet-wide mean but too narrow
and structurally uneven**. After standardising the external reference to the
release mix by aircraft type and distance, co2gap initial mass is **1.209
percentage points of MTOW heavier** than the PRC reference. The mean alone is
not the main finding: the release-weighted absolute cell difference is 2.181
points and its root mean square is 2.651 points, so opposite type and distance
effects partly cancel.

This is a laboratory diagnostic. It does not change the September release, the
site or any headline, and it is not a total uncertainty interval.

## Source and chronology

The reference is version 2 of the [EUROCONTROL PRC 2024 take-off-weight
dataset](https://doi.org/10.4121/8cb8484b-dbe7-4750-8b87-a5b1dbc621b4.v2),
published under CC BY 4.0, with the accompanying [Journal of Open Aviation
Science paper](https://doi.org/10.59490/joas.2025.8252). Its complete
`flight_list.csv` contains 527,162 European flights in 2022 and airline-derived
take-off-weight labels. The repository calls them actual airline-provided
weights in its overview and calls the public `tow` column estimated TOW in its
README. They are therefore a strong external reference, not error-free ground
truth.

`tow-validation-design.json` was committed as `56ea854` before the `tow` values
were opened. It fixed the metric, distance bands, 100-row cell floor, 80%
release-coverage gate, robustness checks and non-claims. The analysis used only
the 101,091,663-byte flight list; none of the 306.6 GB trajectory archive was
downloaded.

The official MD5 `2da9db49afcae255eab0828f1e2f8575` matched. The program also
verified the full 14.16 GB co2gap input set against release SHA-256
`caaad496f62183c7fde5fc7c7538f67afdac4344e40331a4774b04787e4db7c0`
and required OpenAP 2.6.0. External and per-flight co2gap rows stayed outside
git; the tracked result is aggregate only and every reported cell contains at
least 100 rows from each source.

## Comparison

Both sources were normalised by the same OpenAP MTOW for their ICAO type. The
external quantity is `tow / MTOW`; the co2gap quantity is
`init_mass_kg / MTOW`. Cells are aircraft type by flown-distance band. Within
every retained cell the PRC mean is computed first, then cell means are weighted
by that cell's share of eligible September-release flights. Thus a difference
in the raw mix of A320s, 737s, short flights and long flights does not determine
the primary result.

The release side reproduces the four published quality predicates and reads
only the 197 manifest days. It yields exactly 1,833,127 eligible flights, which
independently reconciles with the published population. Seventy-seven common
cells retain 1,722,708 release flights and 431,293 PRC flights, covering 93.98%
of the eligible release population and clearing the registered 80% gate.

| registered measure | co2gap | PRC | difference |
|---|---:|---:|---:|
| standardised mean mass / MTOW | 0.8100 | 0.7979 | **+1.209 pp** |
| standardised median | 0.8093 | 0.8054 | +0.390 pp |
| standardised interquartile range | 0.7893–0.8293 | 0.7596–0.8441 | PRC much wider |

The day-block bootstrap, frozen at 2,000 replicates and seed 20260901, gives
+1.103 to +1.325 points (standard error 0.057). That narrow range only measures
precision from resampling the 365 PRC dates while keeping release cells and
weights fixed. It says nothing about airline selection, label error, year,
operator, variant or model error and must not be presented as total uncertainty.

## Where the mean hides structure

The distance pattern is monotonic enough to demand attention:

| distance | release weight | co2gap − PRC, pp MTOW |
|---|---:|---:|
| 150–500 km | 10.9% | **+4.248** |
| 500–1,000 km | 35.6% | **+2.161** |
| 1,000–1,500 km | 26.2% | +0.277 |
| 1,500–2,500 km | 24.3% | −0.279 |
| 2,500–4,000 km | 3.0% | −0.929 |

On short flights the co2gap assumption is materially heavier; beyond roughly
1,500 km the sign reverses. The two sources do not derive flown distance with
the same trajectory-processing chain, so this is evidence for a structured
scenario, not proof of its cause.

Large release-weight type effects also differ in sign: A20N +3.958 points,
B38M +2.990, A320 +1.966, A319 +1.677, B738 −0.851, A21N +0.088 and E190
−0.703. Very large gaps for B788 and B789 short-distance cells carry less than
0.1% release weight each and should not drive a fleet-wide conclusion. The
aggregate result is therefore not a licence to subtract one constant mass from
every flight.

The external funnel excludes 34,464 rows whose seven type codes are outside
the co2gap/OpenAP mapping, 581 below the 150 km scope, two below OpenAP OEW and
849 above OpenAP MTOW. Allowing up to 102% of MTOW admits 180 more rows and
changes the primary result only from +1.20884 to +1.20877 points. Changing the
cell floor to 30 or 300 rows gives +1.228 and +1.242 points respectively, while
release coverage remains above 91%. The sign and useful scale are not being
set by the registered cell threshold or small MTOW exceedances.

## What this resolves—and what it does not

The result supplies the first large external check of co2gap's **combined
initial mass**. It shows that the fixed 0.82 load factor plus fixed 2,000 kg
reserve and iterated trip fuel does not create a large fleet-average mass error
in the common population, but it suppresses real flight-to-flight dispersion
and misses systematic type/distance structure.

TOW cannot identify payload, carried reserve and trip fuel separately. The
statuses of `load_factor` and `reserve_fuel` therefore remain `scenario_only`.
The result also cannot establish population representativeness: PRC contains
the airlines that consented and had complete weight data, represents 6.1% of
EUROCONTROL traffic in 2022, spans a full year rather than January–July, and
does not expose an airline identity that can be aligned with co2gap. Type and
distance post-stratification cannot repair those dimensions.

The next justified implementation is a **combined mass perturbation**, paired
across observed, ideal and hybrid fuel estimates and stratified at least by
type and distance. Its central pattern can come from these measured residuals,
but its amplitude must remain a structural scenario until selection, temporal
transfer and label/variant error are bounded. Converting the PRC dispersion
directly into independent random noise per flight, or treating the day-block
range as a confidence interval for the 12.1% headline, would be incorrect.

The complete aggregate output is in `tow-validation-result.json`; the runner
and its guards are in `lab/tow_validation.py` and `tests/test_tow_validation.py`.
