#!/bin/bash
# Fase 2a/2b single-command driver.
#
# The fase-2b rerun over the whole year must be ONE COMMAND, not a session:
# everything below is idempotent and resumable, so this is also the right
# thing to re-run today while the Pi backfill and the ERA5 queue are still
# filling in days. It processes whatever is ready and skips what is done.
#
#   scripts/run_phase2.sh              # full chain
#   scripts/run_phase2.sh --no-sync    # skip pulling parquet from the Pi
#   scripts/run_phase2.sh --no-era5    # skip the ERA5 fetch (already running)
#
# Deliberately NOT included: any deploy or publication step. Publishing is
# the user's explicit decision, never a side effect of a rerun.
set -uo pipefail

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

do_sync=1; do_era5=1
for a in "$@"; do
  case "$a" in
    --no-sync) do_sync=0 ;;
    --no-era5) do_era5=0 ;;
    *) echo "unknown option: $a"; exit 2 ;;
  esac
done

cd "$ROOT" || exit 1
step() { echo; echo "=== $* ==="; }

if [ "$do_sync" = 1 ]; then
  step "1/5 sync parquet from the Pi (parquet only, never raw dumps)"
  bash "$ROOT/../sync_parquet.sh" || echo "  sync failed — continuing with local days"
fi

if [ "$do_era5" = 1 ]; then
  step "2/5 ERA5 backfill ${FROM_DAY} → ${TO_DAY}"
  "$PY" scripts/era5_backfill.py "$FROM_DAY" "$TO_DAY"
fi

step "3/5 per-type calibration anchored to ICAO ICEC"
"$PY" lab/anchor_refs.py
"$PY" lab/calibrate.py

step "4/5 lateral/vertical decomposition (all days with parquet + ERA5)"
"$PY" lab/run_decompose.py

step "5/5 decomposition report"
"$PY" lab/decompose_report.py

echo
echo "=== done. Nothing was published — deploy stays an explicit decision. ==="
