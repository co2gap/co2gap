#!/bin/bash
# Phase 2a/2b decomposition chain — NOT the whole pipeline to the site.
#
# The fase-2b rerun over the whole year must be ONE COMMAND, not a session:
# everything below is idempotent and resumable, so this is also the right
# thing to re-run today while the Pi backfill and the ERA5 queue are still
# filling in days. It processes whatever is ready and skips what is done.
#
#   scripts/run_phase2.sh --update     # accumulating inputs; not a release
#   scripts/run_phase2.sh --release-manifest release-manifest.json
#                                      # exact immutable release population
#
# ⚠️ IT STOPS AT THE DECOMPOSITION REPORT, and the site needs two more stages
# that this script does NOT run. It used to call itself "full chain", which was
# a promise it never kept:
#
#   lab/ground_share.py      -> data/ground_share_ecac/        (site_build EXITS without it)
#   lab/run_phase_split.py   -> data/decomposition_ecac_phase/ (site_build goes SILENT without it:
#                                                               it drops the whole phase attribution
#                                                               and falls back to older wording)
#
# Run those two after this one, then the site build in README "Reproducing".
# Widening this script to cover them is on the list for the January release,
# together with a completeness check at the end; renaming it honestly is what
# could be done safely three days before publication.
#
# Deliberately NOT included: any deploy or publication step. Publishing is
# the user's explicit decision, never a side effect of a rerun.
# set -e per davvero: senza, un errore di ERA5, calibrazione o decomposizione
# non fermava la catena, l'ultimo echo restituiva zero e stampava "done". Un
# comando offerto come "catena completa" che riesce dopo aver fallito e' peggio
# di nessun comando. L'unico passo che puo' fallire senza fermare tutto e' il
# sync dal Pi, ed e' guardato dal suo || esplicito.
set -euo pipefail

ROOT="${ADSB_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PY="${ADSB_PY:-$ROOT/../lab-venv/bin/python}"

# La scatola sta nell'ambiente, non nel codice — ma un runbook che dice "una sola
# riga" deve nominarla, altrimenti i default portano su data/flights e
# data/decomposition, cioe' il vecchio box EU-Sud da 64 giorni, e la catena
# gira fino in fondo senza un errore producendo aggregati di un altro dataset.
# Si esportano solo se non sono gia' impostate, cosi' chi ne vuole un'altra la
# passa davanti al comando.
export ADSB_ROOT="$ROOT"
export ADSB_FLIGHTS_DIR="${ADSB_FLIGHTS_DIR:-$ROOT/data/flights_ecac}"
export ADSB_DECOMP_DIR="${ADSB_DECOMP_DIR:-$ROOT/data/decomposition_ecac}"
export ERA5_DIR="${ERA5_DIR:-$ROOT/data/era5_ecac}"
export ADSB_AIRPORTS_CSV="${ADSB_AIRPORTS_CSV:-$ROOT/data/airports_ecac.csv}"
export ADSB_CALIB="${ADSB_CALIB:-$ROOT/data/calibration_ecac.json}"
export ERA5_AREA="${ERA5_AREA:-72,-32,27,45}"
FROM_DAY="${FROM_DAY:-2026-01-01}"
TO_DAY="${TO_DAY:-2026-07-23}"

do_sync=1; do_era5=1; mode=""; manifest=""; allow_partial=0; allow_stale=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --update)
      [ -z "$mode" ] || { echo "choose only one pipeline mode"; exit 2; }
      mode="update"; shift ;;
    --release-manifest)
      [ "$#" -ge 2 ] || { echo "--release-manifest needs a path"; exit 2; }
      [ -z "$mode" ] || { echo "choose only one pipeline mode"; exit 2; }
      mode="release"; manifest="$2"; do_sync=0; shift 2 ;;
    --no-sync) do_sync=0; shift ;;
    --no-era5) do_era5=0; shift ;;
    --allow-partial) allow_partial=1; shift ;;
    --allow-stale-inputs) allow_stale=1; shift ;;
    *) echo "unknown option: $1"; exit 2 ;;
  esac
done
[ -n "$mode" ] || {
  echo "choose explicitly: --update OR --release-manifest PATH"; exit 2;
}
if [ "$mode" = "release" ]; then
  [ "$allow_partial" = 0 ] || { echo "--allow-partial is forbidden for a release"; exit 2; }
  [ "$allow_stale" = 0 ] || { echo "--allow-stale-inputs is forbidden for a release"; exit 2; }
  manifest="$(cd "$(dirname "$manifest")" && pwd)/$(basename "$manifest")"
  [ -f "$manifest" ] || { echo "release manifest not found: $manifest"; exit 2; }
  export ADSB_RELEASE_MANIFEST="$manifest"
fi

cd "$ROOT" || exit 1
step() { echo; echo "=== $* ==="; }

if [ "$do_sync" = 1 ]; then
  step "1/5 sync parquet from the Pi (parquet only, never raw dumps)"
  if ! bash "$ROOT/../sync_parquet.sh"; then
    if [ "$allow_stale" = 1 ]; then
      echo "  sync failed — continuing because --allow-stale-inputs was explicit"
    else
      echo "  sync failed — refusing stale inputs"; exit 1
    fi
  fi
fi

if [ "$do_era5" = 1 ]; then
  if [ "$mode" = "release" ]; then
    step "2/5 ERA5 for immutable release manifest"
    "$PY" scripts/era5_backfill.py --release-manifest "$manifest"
  else
    step "2/5 ERA5 update ${FROM_DAY} → ${TO_DAY}"
    if [ "$allow_partial" = 1 ]; then
      "$PY" scripts/era5_backfill.py "$FROM_DAY" "$TO_DAY" --allow-partial
    else
      "$PY" scripts/era5_backfill.py "$FROM_DAY" "$TO_DAY"
    fi
  fi
fi

if [ "$mode" = "release" ]; then
  step "3/5 verify frozen release inputs and calibration"
  "$PY" scripts/verify_release_manifest.py "$manifest"
else
  step "3/5 update per-type calibration anchored to ICAO ICEC"
  "$PY" lab/anchor_refs.py
  "$PY" lab/calibrate.py
fi

if [ "$mode" = "release" ]; then
  step "4/5 lateral/vertical decomposition (manifest days only)"
  "$PY" lab/run_decompose.py --release-manifest "$manifest"
else
  step "4/5 lateral/vertical decomposition (all ready days)"
  if [ "$allow_partial" = 1 ]; then
    "$PY" lab/run_decompose.py --allow-partial
  else
    "$PY" lab/run_decompose.py
  fi
fi

step "5/5 decomposition report"
"$PY" lab/decompose_report.py

echo
echo "=== 2a/2b done. NOT the whole chain: ground_share.py and run_phase_split.py"
echo "    still have to run before the site can be built. Nothing was published."
