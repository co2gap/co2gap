# co2gap

**An open pipeline that measures the CO₂ of European flights against a
fuel-optimal ideal, from ADS-B trajectories.**

For every flight it computes the CO₂ actually emitted and compares it with an
*ideal* flight: same aircraft type, direct great-circle route, the most
fuel-efficient altitude and speed profile for that distance, and **the same real
wind**. The difference splits into two additive parts — a **lateral** component
(having flown more kilometres) and a **vertical** one (having flown the same
route on a less efficient altitude and speed profile).

Everything is aggregated by route and airport. Nothing is published per flight,
per aircraft or per operator.

Site: **[co2gap.org](https://co2gap.org)** — findings, [context](https://co2gap.org/context.html),
[data](https://co2gap.org/data.html), [methodology](https://co2gap.org/methodology.html),
[FAQ and where the method is weak](https://co2gap.org/faq.html),
[release history](https://co2gap.org/releases.html) and
[replies and corrections](https://co2gap.org/replies.html).
Releases are twice a year, at the end of January and the end of July, each
covering twelve months; the next is **31 January 2027**.

---

## What it measures — and what it does not

**It measures the distance from a theoretical optimum. It does not measure
recoverable fuel.** The ideal great-circle flight at a perfect profile is a limit
no real flight can reach: separation, route structure, constrained airspace and
arrival queues put it out of reach. Published estimates of what is actually
recoverable are far smaller — EUROCONTROL puts roughly 39 kg per flight on
continuous climb and descent procedures, recoverable from current practice,
against roughly 163 kg of vertical gap measured here. Those two figures are not
rival estimates of one quantity: theirs is measured against what aircraft do
today and is recoverable by a known procedure, ours against a theoretical
optimum that no flight can fly. The total gap is roughly 431 kg per flight;
the 163 kg is its vertical component, which is the part continuous climb and
descent procedures address.

The distinction matters enough that the site states it above the fold, and this
repository would be misused if that framing were dropped.

The data also cannot see *why* a profile was flown: noise abatement rules,
sequencing constraints, capacity limits and terrain are invisible to ADS-B. The
output describes **what flights fly**, never what an airport, an airline or a
controller could have done differently.

## Results over the published period

ECAC area, 2026-01-01 → 2026-07-20, **197 days, 1,833,127 flights**:

| | |
|---|---|
| CO₂ emitted **in flight** | **23.4 Mt** |
| Gap against the theoretical optimum | **2.50 Mt** (12.1%) |
| — lateral (routing) | 7.5% |
| — vertical (profile) | 4.6% |
| Routes with n≥10 | 5,483 (2,787 ranked at n≥100) |
| Airports with n≥2,000 movements | 152 |

Of the median flight's 5.1 points of vertical gap, **2.2 remain for a flight
going direct through an empty night sky** (3,120 such flights). We read that as
the baseline staying out of reach rather than inefficiency — a reading, not a
second measurement, since nothing here separates the two. Only 2.9 points move
with traffic, routing and profile, and even that subtraction compares two
groups of different length: the floor is measured above 1,000 km, and at equal
distance the margin is about 0.9 points. The rest is the distance mix.

## External validation

The lateral component is directly comparable to EUROCONTROL's horizontal
en-route flight efficiency indicator (KEA). Computed on the same definition —
excluding the terminal areas within 40 NM of each airport — this pipeline
obtains **+2.26%** against the roughly 3% EUROCONTROL publishes. That agreement
is the main external check on the method, and it was not tuned for.

Splitting the same vertical gap by the part of the path it was burnt on gives a
second, independent reading. Across the 28 airports whose departures deviate by
at least two points, a median of **33%** of that deviation was produced within
40 NM of the airport itself and 32% of it in the climb; for arrivals (52
airports) it is **70%** within 40 NM and **88%** in the descent. EUROCONTROL's own
figures point the same way: they put the fuel recoverable by continuous descent
at around ten times that recoverable by continuous climb. **That is a
location, not a cause**: it says where the fuel was burnt, not whether the
profile was chosen by the operator or imposed by the traffic, and nothing here
distinguishes the two.

Two further checks: the wind correction holds identically across seasons
(directional spread 5.1 / 5.2 / 4.6 in January, February and July), and the
route ranking is stable month over month (Spearman ρ median 0.87, worst pair
0.79 across all 21 month pairs).

## How it works

```
ingest/     source.py        data-source abstraction (adsb.lol daily dumps)
pipeline/   trajectories.py  per-flight trajectory segmentation and cleaning
            flightproc.py    thinned trajectory + quality metrics
            emissions.py     fuel/CO2 via OpenAP (vectorised integrator)
            excess_wind.py   wind-aware great-circle baseline
            decompose.py     lateral / vertical decomposition
            run_daily.py     production orchestrator (multiprocessing)
wind/       era5.py          ERA5 download (CDS) + 4-D wind field
lab/        calibrate.py     per-type correction factors
            anchor_refs.py   ICAO reference cruise fuel flows
            gate.py          wind-correction validation gate
            release_data.py  authoritative ground-corrected release loader
            stability.py     month-over-month rank stability
            run_phase_split.py  vertical excess by phase of flight and position
            phase_attrib.py  per-airport attribution of that split
            context_page.py  the only figures on the site not from the pipeline
            site_build.py    static site generation
            freeze_check.py  guards what the site claims against a snapshot
```

Emissions come from **[OpenAP](https://openap.dev)** (TU Delft). Aircraft mass is
estimated iteratively; true airspeed is derived from reported IAS, so it is
independent of wind — the wind enters the comparison only through flight
*duration*, which is why giving the ideal baseline the same along-track wind
makes it cancel between the two.

Per-type correction factors are anchored to the **ICAO Carbon Emissions
Calculator methodology v13.1**, Appendix C. They are needed because OpenAP ships
its own fuel models for 14 typecodes and falls back to a generic model for
everything else — and it is the generic branch, not the engine, that produces
the error worth correcting.

The types OpenAP models natively need no correction here: A320, A321, B738 and
A319, **the majority of flights in the published period**, land within ~5% of the
ICAO reference on their own. That is the check that means anything, because on a
corrected type the comparison is tautological.

⚠️ The methodology page calls those four **uncalibrated**, which is the opposite
word for the same thing: there it means *no correction applied by this project*,
here it means *OpenAP has a model for them*. Same types, both statements true.

This is documented in an open question to the OpenAP maintainers rather than
worked around silently.

## Known limitations

- The baseline is a **theoretical optimum**, not an achievable target (above).
- Individual positions at the top of the airport ranking are **not resolvable**:
  ten places can sit within 3.1 points in a given month. The defensible claim is
  that a group of congested hubs sits above the norm, not an ordering within it.
- **Closed airspace** makes some routes structurally longer — Kaliningrad,
  Belarus, Ukraine. 208 ranked routes have a direct path through closed
  airspace; they are flagged individually, and the detour is not recoverable
  while those closures hold.
- The vertical component does not distinguish the *cause* of a profile. It is
  split by **where along the path** the gap was burnt (see *External validation*
  above), which is a location and not a cause.
- **Four days are missing** inside the period, all absent at the source
  (2026-05-05, 05-06, 05-07 and 2026-06-11). The window ends on 20 July because
  the four days that follow have flight data but not yet the wind data the
  comparison needs.
- No oceanic coverage: the scope is deliberately ECAC, where ADS-B coverage is
  dense and an external benchmark (KEA) exists.
- **The cruise baseline is too generous.** Measured over the cruise alone the
  gap comes out slightly negative: real aircraft burn marginally less than the
  profile we call optimal, because that baseline cruises about 1,000 ft below
  what aircraft actually reach on the longest sectors. It is a defect in the
  reference, not a result about aviation, and correcting it would make the
  headline figure *larger*. It is stated before the next release rather than
  explained after it.
- **No probabilistic uncertainty is quantified yet.** Aircraft mass is
  estimated rather than known, and there is no evidence-backed ± on any figure
  here. The experimental uncertainty programme below now measures temporal
  composition and paired finite differences, but deliberately does not relabel
  them as confidence intervals. The metric is a difference between two model
  runs, so a systematic error cancels and a state-dependent one does not —
  which is why only the tails of the rankings are presented as meaning
  anything.
- CO₂ only: **these figures contain no contrails or NOx**, and they are a large share
  of aviation's warming effect. A profile that avoids contrail formation can
  look worse by these figures and be better for the climate.

## Data sources and licensing

**Code: Apache-2.0** (see `LICENSE` and `NOTICE`). **Data: not Apache-2.0** —
see `DATA-LICENCE.md`, which matters more than it sounds: the published site is
a *Produced Work* under ODbL and needs only attribution, but redistributing an
aggregated dataset would make it a Derivative Database and bind it to
share-alike. Site **text and charts** are additionally offered under
**CC BY 4.0** — that grant covers this project's own expression, not the
underlying data. The tables are not included: they are figures derived from an
ODbL database, and extracting them as a dataset makes a Derivative Database.
Methodology §12 is the reference text.

The name *co2gap* and the domain are not covered by the code licence: Apache-2.0
§6 grants no trademark rights. Reuse the code freely; do not present a derived
service as this one.

Independence, right of reply and the rules that would govern any paid work are
in `INDEPENDENCE.md`.

- Trajectories: **© adsb.lol contributors**, [ODbL v1.0](https://opendatacommons.org/licenses/odbl/1-0/)
- Wind: **ERA5**, Copernicus Climate Change Service (C3S)
- Airports: **OurAirports** (CC0)
- Performance model: **[OpenAP](https://openap.dev)**, TU Delft
- Reference fuel: **ICAO** Carbon Emissions Calculator methodology v13.1

## Privacy

No published row aggregates fewer than **10 flights**, and nothing is published
per flight, per aircraft registration or per operator. **37 routes whose traffic
is majority business aviation are excluded** from every table and chart: on such
a route a row can describe one or two aircraft — one operator or one owner —
even while clearing the floor of 10 flights, and since aircraft identity is
deliberately not stored, that cannot be ruled out by counting. Those flights
stay in the European totals, where they identify nobody. This is a project rule
rather than a licence requirement: it is what keeps an aggregate observatory from
becoming a tool for tracking individual movements.

## Known issues

Known defects and open methodological risks are written down in
[`KNOWN-ISSUES.md`](KNOWN-ISSUES.md), with what each one moves and what fixing
it requires. The entries distinguish measured release effects, future risks and
uncertainty that remains unbounded; they must not be pooled into one claim about
the published rankings or headline.

## Uncertainty programme

The first uncertainty layer is documented in [`UNCERTAINTY.md`](UNCERTAINTY.md).
It keeps exact release values, temporal-composition diagnostics and paired model
sensitivities separate; none is labelled a confidence interval. The observed
track and every counterfactual are perturbed with the same parameters, and
per-flight sensitivity rows remain inside the lab. Validate the machine-readable
register and diagnostic scenarios with:

```bash
python lab/uncertainty.py validate
```

The full commands intentionally write their sample and aggregate results to
`/tmp`. They do not alter a frozen release or the public site.

The release-gate denominator and attrition can be audited independently of the
fuel sensitivities:

```bash
python lab/uncertainty.py selection \
  --release-manifest "$PWD/release-manifest.json" \
  --flights-dir /path/to/data/flights_ecac \
  --decomposition-dir /path/to/data/decomposition_ecac \
  --out /tmp/co2gap-selection-audit.json
```

This rebuilds the four gate predicates from the durable pre-gate flight tables
and requires their passing keyset to equal the release decomposition exactly.
Its denominator begins after complete-flight reconstruction, aircraft-model
support and successful first-pass fuel modelling; it does not call that subset
all ECAC traffic. The retained shares measure coverage, not the direction or
size of bias in the 12.1% headline: rejected flights have no trustworthy
decomposition. New daily ingestion records the earlier attrition stages as
an aggregate, internally closed funnel in the parquet source contract. The
September inputs predate that funnel, so their upstream attrition cannot be
recovered retrospectively.

The observable side can then be stressed without assigning an outcome to any
rejected flight:

```bash
python lab/uncertainty.py selection-stress \
  --release-manifest "$PWD/release-manifest.json" \
  --flights-dir /path/to/data/flights_ecac \
  --decomposition-dir /path/to/data/decomposition_ecac \
  --ground-dir /path/to/data/ground_share_ecac \
  --calibration "$PWD/data/calibration_ecac.json" \
  --out /tmp/co2gap-selection-stress.json
```

It applies nested stricter coverage and maximum-gap filters to the frozen
population, removes the lowest-retention days, and reports both raw subsets and
a poststratification to the nominal aircraft-type/distance ideal-CO2 mix. The
result measures a within-gate quality gradient. It is explicitly not an
imputation, correction or bound for flights outside the gate.

The next selection step is prepared, but cannot manufacture its missing input:

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

The first private file holds gate status and sampling weights. The second is a
blinded list for an independent trajectory source: timestamps, aircraft type
and endpoint coordinates, but no release flight id, gate result, quality band
or resolved airport. On the frozen release the design selects 5,000 flights in
843 cells, expands exactly to 2,115,824 pre-gate flights, has a maximum weight
of 701.23 and a Kish effective size of 3,537.75. The aggregate registration in
`selection-validation-design.json` freezes those parameters, partitions and
the SHA-256 of both private files; regeneration fails if either digest changes.

Independent results must follow
`lab/selection-validation-outcomes.schema.json`. Analyse them with:

```bash
python lab/uncertainty.py selection-validation \
  --sample /tmp/co2gap-selection-validation-sample.json \
  --match-list /tmp/co2gap-selection-validation-match-list.json \
  --outcomes /private/path/independent-outcomes.json \
  --out /tmp/co2gap-selection-validation-result.json
```

The runner writes a blocked diagnostic and exits non-zero on partial response
rather than modelling a second selection process. A measured row must also have
one unambiguous match, report temporal
and endpoint offsets, and pass the second source's documented trajectory-quality
rule. A complete result carries a design-based sampling interval for the
independent proxy; it is still not a general uncertainty interval or automatic
correction of the published headline.

A free one-day external audit has also been completed with OpenSky scientific
dataset 11. The matching code never receives gate status or primary track
quality, all per-flight material stays in `/tmp`, and the aggregate design and
result are tracked in `opensky-day-audit-design.json` and
`opensky-day-audit-result.json`. It finds useful failure-mask heterogeneity but
does not close the problem: matching succeeds for 86.17% of gate passes versus
74.65% of rejects, and on 7,615 like-for-like airborne controls the OpenSky
ground-speed proxy is 4.61 percentage points below the frozen primary gap. The
observed -1.51-point rejected-minus-passed contrast is therefore diagnostic,
not a headline correction or bound. Full commands, the failed raw-state pilot
and its explicitly post-pilot amendment are documented in
[`UNCERTAINTY.md`](UNCERTAINTY.md).

Those mask contrasts now drive a separately frozen sensitivity ladder, without
imputing a rejected flight:

```bash
python lab/uncertainty.py selection-sensitivity \
  --selection-audit /tmp/co2gap-selection-audit.json \
  --out /tmp/co2gap-selection-sensitivity.json
```

Half, one and twice the observed mask contrast produce adverse total shifts of
respectively **±0.184, ±0.369 and ±0.737 percentage points** around the frozen
headline. The signed central transfer is only -0.014 points because 96.3% of
opposite mask contributions cancel; it is not evidence of negligible bias.
Coverage-only and unresolved-endpoint failures supply 94.4% of the central
stress magnitude and are therefore the two priorities for a held-out external
extract. `selection-sensitivity-design.json` and the output both forbid calling
these stresses a correction, confidence interval or bound.

## Reproducing

The two machines have separate direct-dependency locks:
`requirements-pi.lock` for daily acquisition and fuel computation, and
`requirements-lab.lock` for decomposition, ERA5 and site generation. OpenAP is
pinned in both because its version moves the kilograms; the lab lock also names
`xarray`, `cdsapi` and the `netCDF4` backend required by the documented weather
chain. `requirements.txt` remains a compatibility alias for the lab lock.
`scripts/check_environment.py pi|lab` verifies both the recorded Python version
and every direct pin before a rerun. The Pi record is necessarily only Python
3.11: its patch version was not captured with the first release.

The fast regression suite is independent of the frozen data and network:

```bash
python -m unittest discover -s tests -v
```

It runs in CI and exercises download failure semantics, truncated and partial
artefacts, release keysets and perimeters, ERA5 coverage, required phase and
coverage inputs, exact headlines, and whole-generation site promotion.

Production (per-day accumulation):

```bash
WORKERS=3 python pipeline/run_daily.py --day 2026.07.19
```

Each newly promoted day contracts an aggregate selection funnel from dump
members through regional traces, exclusive complete-flight rejection reasons,
aircraft support, fuel-model success and the quality-gate failure combinations.
It stores no rejected trace or flight identifier. Historical days without this
new optional contract block remain readable rather than being quarantined for
lacking information that was never collected.

The source downloader obtains the complete asset list and declared byte sizes
from the official GitHub release API before transferring anything. A confirmed
release-tag 404 is the only condition recorded as “not published”; API errors,
timeouts, incomplete lists and size mismatches are failures and remain
retryable. Both the sequential and parallel downloaders verify every declared
part, rather than guessing the end of a split archive from the first missing
suffix.

Analysis chain — idempotent and resumable, and **no step publishes anything**.
The two modes are deliberately different commands: `--update` consumes every
ready day in the accumulating caches; `--release-manifest` selects and verifies
the immutable published population. Omitting the mode is an error.

```bash
scripts/run_phase2.sh --update
```

To reproduce the frozen release instead:

```bash
scripts/run_phase2.sh --release-manifest $PWD/release-manifest.json
```

`release-manifest.json` records its exact 197 days, the separate historical
201-day calibration population, geographic and ERA5 grids, ground definition,
OpenAP and quality-gate versions, and SHA-256 checksums of every input and frozen
artefact. Verify it independently with
`python scripts/verify_release_manifest.py --include-artifacts release-manifest.json`.
Extra days may coexist in an accumulating *input cache*, but no release output
directory may contain a missing or extra day.

The 201-day calibration population is historical provenance, not the rule for a
new release: it records the four extra days that entered the frozen September
factors. Corrected calibration reads `manifest.days`, verifies that exact input
set, and future manifests make calibration and analysis populations identical.

An ERA5 file counts as ready only when it contains the exact 24 UTC timestamps
of its named date, all configured pressure levels, only the requested wind
variables, and the complete configured area at the configured resolution. A
readable partial day or a NetCDF from another box is retried, never interpolated.

Every multi-day stage returns a non-zero status if any requested day fails or
is missing. Exploratory accumulation may opt into `--allow-partial`; release
mode rejects that flag. A failed Pi sync is fatal unless an update run explicitly
uses `--allow-stale-inputs`, which is also forbidden for a release.

New daily parquet outputs are self-validating. Durable flight/point pairs record
their shared counts and keyset, the downloader's complete asset manifest, the
number and fraction of declared dump bytes actually consumed, schemas and
configuration. The pair is written under a sibling staging directory and is
promoted only after the tar reaches normal completion and at least 90% of its
declared bytes were read. A failed run exits non-zero and leaves no final day.
An older promoted day that fails the current contract is moved intact to the
sibling `flights*.quarantine` directory and reported, never deleted or
overwritten automatically. Derived decomposition, phase and ground files
additionally record full SHA-256 fingerprints of their upstream inputs. Resume
checks validate those contracts instead of treating any readable non-empty file
as complete. The September artefacts predate the footer contract and are
accepted only through the full checksums in their manifest.

Before a phase parquet is promoted, the rebuilt hybrid must reproduce its
decomposition input exactly and the six phase/position buckets must close to
within `1e-9` percentage points. Both gates run on every day and a failure
leaves no final output file.

Release headlines have one loading path: `lab/release_data.py` joins the exact
decomposition and ground keysets, removes ground movement, recomputes the three
excess columns and only then applies calibration to absolute masses. The site,
`decompose_report.py` and `phase_report.py` all use it; the reports cannot fall
back to the retired gate-to-gate headline when a ground artefact is absent.

Two more stages have to run before the site can be built. Without the first,
`site_build.py` exits; without the second it stays silent and drops the phase
attribution altogether, which is the worse failure of the two. They are two
separate commands and each carries its own environment — variables written in
front of one `python` do not survive to the next, and the defaults they would
fall back to are the old, smaller study area:

```bash
ADSB_ROOT=$PWD python lab/ground_share.py \
  --src $PWD/data/flights_ecac \
  --out $PWD/data/ground_share_ecac \
  --release-manifest $PWD/release-manifest.json
```

```bash
ADSB_ROOT=$PWD \
ADSB_FLIGHTS_DIR=$PWD/data/flights_ecac \
ADSB_DECOMP_DIR=$PWD/data/decomposition_ecac \
ADSB_PHASE_DIR=$PWD/data/decomposition_ecac_phase \
python lab/run_phase_split.py --release-manifest $PWD/release-manifest.json
```

`--out` is required on the first: it has no default, deliberately, because it
writes a directory that the site then treats as authoritative. The manifest
takes the exact set of days instead of deriving it from `data/flights_ecac`,
which keeps accumulating every night and is already four days ahead of this
release. It processes only those days and **refuses to run** if the output
directory holds any day outside the set or if its final checksum differs; it
does not delete anything. Both commands are
resumable, and a day counts as done only if its file opens and has rows — an
interrupted write leaves a temporary, never a truncated result.

Site generation:

```bash
ADSB_DECOMP_DIR=$PWD/data/decomposition_ecac \
ADSB_PHASE_DIR=$PWD/data/decomposition_ecac_phase \
ADSB_GROUND_DIR=$PWD/data/ground_share_ecac \
ADSB_CALIB=$PWD/data/calibration_ecac.json \
ADSB_AIRPORTS_CSV=$PWD/data/airports_ecac.csv \
ADSB_COVERAGE_JSON=$PWD/data/coverage_ecac.json \
ADSB_RELEASE_MANIFEST=$PWD/release-manifest.json \
ADSB_RELEASE_HEADLINES=$PWD/release-headlines.json \
ADSB_SITE_OUT=$PWD/site/index.html \
python lab/site_build.py --profile release
```

The `release` profile makes all nine variables above mandatory before staging
starts. It verifies the manifest, the exact decomposition/phase/ground day sets
and checksums, calibration, airports and coverage; an absent phase split or even
an import error in its attribution code is fatal. The older wording that omits
phase attribution exists only under the explicit `--profile exploratory`, which
announces on stderr that release guarantees and the frozen-headline gate are
disabled and reports the reason for every fallback. An exploratory build should
always set `ADSB_SITE_OUT` to a disposable directory. The release run must print
**1,833,127 flights · 197 days · 23.37 Mt · lat 7.51 · vert 4.59 · KEA +2.26 ·
152 airports · 208 flagged routes**; anything else means a different dataset was
read. Before rendering, `lab/headline_check.py` compares thirteen unrounded
values and exact counts against `release-headlines.json`; it is a numeric gate,
separate from the editorial one. `lab/freeze_check.py check` compares the
rebuilt pages against a snapshot of what the site claims and is what caught
that fallback in the first place.

The generator builds a complete site in a sibling staging directory, validates
the exact generated/static file set, and only then promotes the whole directory.
A late failure therefore leaves the previous generation untouched, while files
that a new generation no longer produces cannot survive as stale pages.

Per-flight intermediate data stays out of this repository by design.

## About this project, plainly

I am not an aviation professional, an air traffic controller or a climate
scientist. I keep an ADS-B receiver at home, and this started as a personal
project because the subject matters to me.

**The method, the modelling and the code were built with AI assistance (Claude);
the constraints are mine** — what the figures cover, when they change, and what
this project declines to claim. The method and the code are open precisely so
that people who do know the field can check them — and if you find an error,
that is the point of publishing it this way. Corrections and
right-of-reply responses are published next to the figure they concern.

Contact: **hello@co2gap.org**
