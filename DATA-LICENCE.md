# Data licensing, in practice

The source code is Apache-2.0 (see `LICENSE` and `NOTICE`). The data is not, and
the distinction matters enough to write down — the ODbL obligations attach to
*databases*, not to the code that reads them.

## What we publish, and under what terms

| Artefact | What it is under ODbL | Obligation |
|---|---|---|
| **The website** (`site/`) — charts, rankings, prose | **Produced Work** | Attribute adsb.lol and name the ODbL licence of the source database. Share-alike does **not** apply to a Produced Work. **Text and charts** are additionally offered under **CC BY 4.0** — this project's own expression, not the underlying data. **Not the tables**: offering data derived from an ODbL database under CC BY would purport to grant what is not ours to grant. |
| **The source code** (`pipeline/`, `lab/`, `ingest/`, …) | Not a database | **Apache-2.0**. No ODbL obligation — it contains no adsb.lol data. |
| **Per-flight parquet** (`data/flights*/`, `data/decomposition*/`) | Derivative Database | **Never published.** Excluded by `.gitignore`. Per-flight rows do not leave the lab, for GDPR reasons independent of licensing. |
| **Aggregated per-route / per-airport tables**, if ever released as a downloadable file | Derivative Database | Would have to be released **under ODbL**, share-alike included. This is the one that trips people up. |

The practical consequence: **publishing the site is unproblematic; publishing a
downloadable dataset is a licensing decision**, because share-alike would then
bind it. That is deliberate and was factored into the source choice — adsb.lol
(ODbL) permits commercial use, unlike the non-commercial alternatives.

⚠️ **Do not release an aggregated dataset under a permissive licence for
convenience.** Share-alike on a Derivative Database is the only term that
obliges a downstream reuser to give something back, and it cannot be reinstated
once a release has gone out without it.

## Why Apache-2.0, and not MIT, GPL or AGPL

- **Apache-2.0 over MIT** — identical permissions, but it adds an explicit
  patent grant, an explicit *refusal* of trademark rights (§6), and a `NOTICE`
  mechanism for attribution. For a project whose only currency is being credited
  and being verifiable, that is strictly better at no cost.
- **Not GPL or AGPL** — neither prevents the realistic case, which is someone
  running this pipeline privately and selling reports produced from its output.
  That involves no distribution and no network interaction with this code, so
  copyleft never triggers. What does bite is ODbL share-alike on the data,
  above. AGPL would additionally exclude organisations that ban it outright,
  which in this field is a cost rather than a neutral choice.
- **The asymmetry to remember** — a licence can always be relaxed later; it can
  never be tightened on a release already published.

## Required attribution

The site carries, and must keep carrying:

> Flight trajectories: © adsb.lol contributors, [Open Database Licence (ODbL) v1.0](https://opendatacommons.org/licenses/odbl/1-0/).
> Wind: ERA5, Copernicus Climate Change Service (C3S).
> Airports: OurAirports (CC0). Performance model: [OpenAP](https://openap.dev), TU Delft.

⚠️ **Not all four are licence obligations, and the distinction is worth keeping
straight.** Attribution to **adsb.lol** is required by the ODbL, and
acknowledgement of **Copernicus/ERA5** by its own terms: removing either breaks a
licence. **OurAirports is CC0** — a public-domain dedication that waives
attribution — and **OpenAP** is credited as the tool that produced the figures,
not because LGPL-3.0 compels a line on a web page. Those two are carried because
crediting what you rely on is right, not because anyone could sue over it.
Claiming otherwise would overstate the obligation, which is its own kind of
error.

## Privacy floor, which is stricter than any licence

No row is published that aggregates **fewer than 10 flights**, and nothing is
published per flight, per aircraft registration or per operator. This is a
project rule, not a licence requirement: it is what keeps an aggregate
observatory from becoming a tool for tracking individual movements.

## Related

Independence, right of reply and the rules that would govern any paid work are
in `INDEPENDENCE.md`. They are policy rather than licensing, but anyone reusing
these figures should know they exist.
