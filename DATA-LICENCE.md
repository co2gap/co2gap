# Data licensing, in practice

The source code is MIT (see `LICENSE`). The data is not, and the distinction
matters enough to write down — the ODbL obligations attach to *databases*, not
to the code that reads them.

## What we publish, and under what terms

| Artefact | What it is under ODbL | Obligation |
|---|---|---|
| **The website** (`site/`) — charts, rankings, prose | **Produced Work** | Attribute adsb.lol, name the ODbL licence of the source database. Share-alike does **not** apply. |
| **The source code** (`pipeline/`, `lab/`, `ingest/`, …) | Not a database | MIT. No ODbL obligation — it contains no adsb.lol data. |
| **Per-flight parquet** (`data/flights*/`, `data/decomposition*/`) | Derivative Database | **Never published.** Excluded by `.gitignore`. Per-flight rows do not leave the lab, for GDPR reasons independent of licensing. |
| **Aggregated per-route / per-airport tables**, if ever released as a downloadable file | Derivative Database | Would have to be released **under ODbL**. This is the one that trips people up. |

The practical consequence: **publishing the site is unproblematic; publishing a
downloadable dataset is a licensing decision**, because share-alike would then
bind it. That is deliberate and was factored into the source choice — adsb.lol
(ODbL) permits commercial use, unlike the non-commercial alternatives.

## Required attribution

The site carries, and must keep carrying:

> Flight trajectories: © adsb.lol contributors, [Open Database Licence (ODbL) v1.0](https://opendatacommons.org/licenses/odbl/1-0/).
> Wind: ERA5, Copernicus Climate Change Service (C3S).
> Airports: OurAirports (CC0). Performance model: [OpenAP](https://openap.dev), TU Delft.

Removing any of these breaks the licence, not merely good manners.

## Privacy floor, which is stricter than any licence

No row is published that aggregates **fewer than 10 flights**, and nothing is
published per flight, per aircraft registration or per operator. This is a
project rule, not a licence requirement: it is what keeps an aggregate
observatory from becoming a tool for tracking individual movements.
