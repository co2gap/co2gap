# co2gap

**An open pipeline that measures CO₂ and flight inefficiency in Europe from
ADS-B trajectories.**

For every flight it computes the CO₂ actually emitted and compares it with an
*ideal* flight: same aircraft type, direct great-circle route, the most
fuel-efficient altitude and speed profile for that distance, and **the same real
wind**. The difference splits into two additive parts — a **lateral** component
(having flown more kilometres) and a **vertical** one (having flown the same
route on a less efficient altitude and speed profile).

Everything is aggregated by route and airport. Nothing is published per flight,
per aircraft or per operator.

Site: **[co2gap.org](https://co2gap.org)** · Methodology: `site/methodology.html`

---

## What it measures — and what it does not

**It measures the distance from a theoretical optimum. It does not measure
recoverable fuel.** The ideal great-circle flight at a perfect profile is a limit
no real flight can reach: separation, route structure, constrained airspace and
arrival queues put it out of reach. Published estimates of what is actually
recoverable are far smaller — EUROCONTROL puts roughly 39 kg per flight on
continuous climb and descent procedures, against roughly 520 kg of total gap
measured here.

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
| CO₂ emitted | **25.44 Mt** |
| Gap against the theoretical optimum | **4.57 Mt** (22.1%) |
| — lateral (routing) | 7.51% |
| — vertical (profile) | 14.55% |
| Routes with n≥10 | 5,520 (2,788 ranked at n≥100) |
| Airports with n≥2,000 movements | 152 |

Of the 14.0 points of vertical gap, **5.5 remain for a flight going direct
through an empty night sky** — a floor that is not inefficiency but the baseline
staying out of reach. Only the remaining 8.5 move with traffic, routing and
profile.

## External validation

The lateral component is directly comparable to EUROCONTROL's horizontal
en-route flight efficiency indicator (KEA). Computed on the same definition —
excluding the terminal areas within 40 NM of each airport — this pipeline
obtains **+2.26%** against the roughly 3% EUROCONTROL publishes. That agreement
is the main external check on the method, and it was not tuned for.

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
            site_build.py    static site generation
```

Emissions come from **[OpenAP](https://openap.dev)** (TU Delft). Aircraft mass is
estimated iteratively; true airspeed is derived from reported IAS, so it is
independent of wind — the wind enters the comparison only through flight
*duration*, which is why giving the ideal baseline the same along-track wind
makes it cancel between the two.

Per-type correction factors are anchored to the **ICAO Carbon Emissions
Calculator methodology v13.1**, Appendix C. They are needed because OpenAP ships
calibrated fuel models for 14 typecodes and falls back to a generic model for
everything else; types with a calibrated model land within ~5% of the ICAO
reference with no correction at all, across more than 270,000 flights. This is
documented in an open question to the OpenAP maintainers rather than worked
around silently.

## Known limitations

- The baseline is a **theoretical optimum**, not an achievable target (above).
- Individual positions at the top of the airport ranking are **not resolvable**:
  ten places can sit within 3.1 points in a given month. The defensible claim is
  that a group of congested hubs sits above the norm, not an ordering within it.
- **Closed airspace** makes some routes structurally longer — Kaliningrad,
  Belarus, Ukraine. 208 ranked routes have a direct path through closed
  airspace; they are flagged individually, and the detour is not recoverable
  while those closures hold.
- The vertical component does **not** separate cruise from climb and descent.
- Four days are missing from the source (2026-05-05/06/07 and 2026-06-11).
- No oceanic coverage: the scope is deliberately ECAC, where ADS-B coverage is
  dense and an external benchmark (KEA) exists.

## Data sources and licensing

**Code: Apache-2.0** (see `LICENSE` and `NOTICE`). **Data: not Apache-2.0** —
see `DATA-LICENCE.md`, which matters more than it sounds: the published site is
a *Produced Work* under ODbL and needs only attribution, but redistributing an
aggregated dataset would make it a Derivative Database and bind it to
share-alike. Site text, tables and figures are additionally offered under
**CC BY 4.0**.

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
per flight, per aircraft registration or per operator. This is a project rule
rather than a licence requirement: it is what keeps an aggregate observatory from
becoming a tool for tracking individual movements.

## Reproducing

Production (per-day accumulation):

```bash
WORKERS=3 python pipeline/run_daily.py --day 2026.07.19
```

Analysis chain — idempotent and resumable, and **no step publishes anything**:

```bash
scripts/run_phase2.sh
```

Site generation:

```bash
ADSB_DECOMP_DIR=$PWD/data/decomposition_ecac \
ADSB_CALIB=$PWD/data/calibration_ecac.json \
ADSB_AIRPORTS_CSV=$PWD/data/airports_ecac.csv \
ADSB_SITE_OUT=$PWD/site/index.html \
python lab/site_build.py
```

Per-flight intermediate data stays out of this repository by design.

## About this project, plainly

I am not an aviation professional, an air traffic controller or a climate
scientist. I keep an ADS-B receiver at home, and this started as a personal
project because the subject matters to me.

**The pipeline was built with AI assistance (Claude).** The method and the code
are open precisely so that people who do know the field can check them — and if
you find an error, that is the point of publishing it this way. Corrections and
right-of-reply responses are published next to the figure they concern.

Contact: **hello@co2gap.org**
