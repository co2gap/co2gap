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
- **No uncertainty is quantified.** Aircraft mass is estimated rather than
  known, and there is no ± on any figure here. The metric is a difference
  between two model runs, so a systematic error cancels and a state-dependent
  one does not — which is why only the tails of the rankings are presented as
  meaning anything.
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

Three defects found before the first release are written down in
[`KNOWN-ISSUES.md`](KNOWN-ISSUES.md), with what each one moves and what fixing
it requires. Two are also stated on the site. None of them changes the published
rankings; one of them cannot have inflated the headline, only lowered it.

## Reproducing

`requirements.txt` records the library versions that produced this release —
OpenAP above all, since it is the fuel model and its version moves the
kilograms. `scripts/setup_venv.sh` installs unpinned, so that file, not the
script, is what a rerun should follow.

Production (per-day accumulation):

```bash
WORKERS=3 python pipeline/run_daily.py --day 2026.07.19
```

Analysis chain — idempotent and resumable, and **no step publishes anything**.
It covers phases 2a/2b only and stops at the decomposition report:

```bash
scripts/run_phase2.sh
```

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
  --days-from $PWD/data/decomposition_ecac
```

```bash
ADSB_ROOT=$PWD \
ADSB_FLIGHTS_DIR=$PWD/data/flights_ecac \
ADSB_DECOMP_DIR=$PWD/data/decomposition_ecac \
ADSB_PHASE_DIR=$PWD/data/decomposition_ecac_phase \
python lab/run_phase_split.py
```

`--out` is required on the first: it has no default, deliberately, because it
writes a directory that the site then treats as authoritative. `--days-from`
takes the exact set of days from the frozen decomposition instead of from
`data/flights_ecac`, which keeps accumulating every night and is already four
days ahead of this release — without it the run adds days the release does not
contain. It processes only those days and **refuses to run** if the output
directory already holds any day outside the set; it does not delete them, since
deciding what to discard is not a compute script's job. Both commands are
resumable, and a day counts as done only if its file opens and has rows — an
interrupted write leaves a temporary, never a truncated result.

Site generation:

```bash
ADSB_ROOT=$PWD \
ADSB_DECOMP_DIR=$PWD/data/decomposition_ecac \
ADSB_PHASE_DIR=$PWD/data/decomposition_ecac_phase \
ADSB_GROUND_DIR=$PWD/data/ground_share_ecac \
ADSB_CALIB=$PWD/data/calibration_ecac.json \
ADSB_AIRPORTS_CSV=$PWD/data/airports_ecac.csv \
ADSB_COVERAGE_JSON=$PWD/data/coverage_ecac.json \
ADSB_SITE_OUT=$PWD/site/index.html \
python lab/site_build.py
```

None of those variables are optional, and two of them fail *silently* rather
than loudly: without `ADSB_DECOMP_DIR` the script reads whichever decomposition
directory it finds by default, and without `ADSB_PHASE_DIR` it does not error at
all — it falls back to older wording that says the gap cannot be located inside
the flight, and drops the whole phase attribution. `ADSB_GROUND_DIR` is the
opposite case and deliberately so: without it the script *exits*, because the
alternative is a gap that silently prices taxiing at a cruise fuel flow. It has
a working default, which is why it is written out above rather than relied on —
a runbook followed at midnight should not depend on a directory being where the
code guesses. The run must print
**1,833,127 flights · 197 days · 23.37 Mt · lat 7.51 · vert 4.59 · KEA +2.26 ·
152 airports · 208 flagged routes**; anything else means a different dataset was
read. `lab/freeze_check.py check` compares the rebuilt pages against a snapshot
of what the site claims and is what caught that fallback in the first place.

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
