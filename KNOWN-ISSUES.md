# Known issues

Four defects, found along the way, measured rather than estimated, and
deliberately left in the frozen data. Two of them are also stated on the
site — in methodology §11 and in the FAQ — because a reader who trusts a figure
should not have to read the source to learn its limits. This file is the
engineer's copy: what is wrong, how much it moves, and what fixing it requires.

The operational runbook that schedules the work is not in this repository. This
file is, so that a clone carries the defects along with the code.

## 1. The airport resolver used one longitude scale for all of Europe

`pipeline/airports.py` matched a trajectory's endpoints to an airport using
`cos(43°)` — the centre of the original, smaller study area — at every latitude.
It survived the move to the full ECAC area, which reaches 72°N, where the factor
is wrong by 46%: an endpoint 5.5 km east of its airport measured 8.0 km and lost
its ICAO. Corrected on 2026-08-29.

Measured on the 197 published days **before** changing it: 0.28% of origins and
0.53% of destinations move, and 229 airports change their movement count (Palma
−15%, Belfast Aldergrove +16%, Billund +18%). The ranking does not move at all —
same 152 airports, Spearman 1.0000, identical top twenty, median change 0.000
points, 0.162 at the worst and at unchanged rank.

**Consequence for reproduction:** re-running today's code reproduces the
rankings exactly and the movement counts approximately. The frozen window is
left alone rather than recomputed under a rule that changed mid-window.

**Fix, at the next release:** re-resolve every endpoint from the `o_lat`/`o_lon`
stored in `flights.parquet`, so the whole window is matched one way. The raw
dumps are not needed. The decomposition is per-flight physics and does not
depend on the ICAO; only the aggregation has to be redone.

## 2. The quality gate never looks at the speed channel

`pipeline/emissions.py` derives true airspeed as IAS where present and ground
speed otherwise. When **both** are absent there is no third branch and no guard:
TAS is NaN, the step contributes no fuel, and the flight leaves with an absurd
burn instead of being rejected. The gate cannot catch it — `coverage_frac` and
`max_gap_s` measure positional coverage, never speed.

The worst case in the published window is an A320 with 0 of 203 points carrying
IAS and 4 carrying ground speed: 62.7 kg of fuel for 55 minutes, a total gap of
−95.8%. 37 flights sit below −50%.

Removing them shifts the headline by 0.002 of a point and leaves the airport and
route rankings identical. **The defect pushes the gap down, never up:** a flight
that burns too little lowers the aggregate, so it cannot have inflated anything.

`lab/run_phase_split.py` already rejects that flight — it runs on ground speed
and finds none — which is why the phase split holds 1,833,126 rows against the
decomposition's 1,833,127. Two code paths disagree about one flight and the one
that discards it is right.

**Fix, at the next release:** a guard on the fraction of points carrying a
usable speed, and that fraction stored in `flights.parquet` next to
`coverage_frac`, since today it cannot be measured after the fact without
re-reading the points.

## 3. The thresholds are shared with the code but not stamped on the data

The track-quality thresholds now live in one place, `pipeline/track_quality.py`,
read by the gate, by the pipeline and by the sentence published in methodology
§6. The parquet files, however, store the gate **already applied**:
`coverage_frac` and `flown_ge_09gc` were computed with the thresholds of the
day. Change a threshold, rebuild only the site, and the page would state a
criterion the data do not meet.

`lab/site_build.py` checks `GC_MIN_KM` against the shortest published sector and
exits if it fails. That check is one-directional: it proves every row satisfies
the current threshold, not which threshold produced the file. Raising it fires;
lowering it passes in silence.

No equivalent check is possible for the other three. On `FLOWN_MIN_FRAC` it
could not fail at all — in the published window the minimum `flown/gc` ratio is
1.0001, so the 90% rule is not binding here — and a check that cannot fail is
worse than none. `coverage_frac` and the gap threshold are not carried by the
decomposition at all.

**Fix, at the next release:** write the threshold values and a version into the
parquet files (or a manifest beside them) and have the site compare what it read
with what it is about to publish. And rename `flown_ge_09gc`, whose name goes
false the day `FLOWN_MIN_FRAC` stops being 0.9.

## 4. The baseline can never climb above FL360, for any aircraft

`pipeline/excess_wind.py` picks the ceiling of its altitude search like this:

```python
cruise_h_m = ac.get("cruise", {}).get("height", 11000)
ceiling_m  = ac.get("ceiling", cruise_h_m + 1000)
return min(cruise_h_m, ceiling_m - 500) / 0.3048
```

`cruise.height` is OpenAP's *nominal* cruise altitude and is 11 000 m for every
type in the model; `ceiling` is the *service* ceiling, 12 500 m for an A320 and
13 100 m for a 777-300ER. Because no type has a ceiling below 11 500 m, the
`min()` can never take its second argument: **the ceiling branch is dead code**,
and the search stops at 11 000 m — 36 089 ft — for an A320 and an A330 alike.

The search therefore never tests FL370 to FL410, which is where long sectors are
actually flown. Measured on the published window, real cruise above 1 000 km
averages 37 025 ft: **above the ceiling of the search that is supposed to find
the optimum**.

This is not a defect in OpenAP, which reports both numbers correctly. It is this
repository choosing the wrong one.

Measured on 2026-09-01, the day after the first release, over seven types and
four distances: **in 28 cases out of 28 the nominal fuel keeps falling above the
ceiling**, and the minimum always lies outside the range the code can search.
Against the 36 000 ft optimum it does find:

| types | fuel at the true minimum |
|---|---|
| A320, A321, A20N | −0.5% to −2.3% |
| B738 | −1.8% to −3.2% |
| E190 | −2.4% to −3.6% |
| A333, B77W | **−5.0% to −9.5%** |

**Direction of the error, which is the part that matters:** the ideal flight is
burning more than it should, so the difference between the real flight and the
ideal one is *smaller* than it should be. **The published gap is understated, not
inflated.** By how much on the headline is *not* measured — translating these
figures into points of gap means re-running the decomposition over every flight,
which is work for the next release, not a number to guess at now.

**Fix, at the next release — and the first step is not raising the ceiling.**
Across the whole range tested the fuel curve has no minimum inside the physical
envelope: it is still falling at 42 000 ft, above the certified ceiling of most
of these types. A real curve turns: higher up the air is thinner, holding speed
costs more thrust, and the limits bite. Either the model does not capture that
upper-side penalty — in which case simply lifting the cap would overcorrect and
*inflate* the gap — or the minimum sits just above and has to be found. That has
to be settled before anything is changed. The same work needs the constraint the
optimiser lacks entirely: **achievable altitude depends on weight**, and a
loaded aircraft reaches its best level in steps rather than at once.

A second, smaller thing sits in the same function: the search steps 2 000 ft from
16 000, so it can only return even thousands. Below 600 km a 1 000 ft step finds
a better level and saves 1% to 1.5%. Above 1 000 km it changes nothing at all —
both step sizes stop at the same ceiling, which is how the ceiling was found.
