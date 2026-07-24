#!/bin/bash
# Backfill specific days to parquet: download, process, delete that day's raw.
# Keeps only one day of raw on disk at a time. Parquet is kept forever.
# Usage: backfill.sh 2026.07.13 2026.07.14 ... (dump-tag dates)
set -uo pipefail

ROOT="${ADSB_ROOT:-/mnt/wd_elements/adsb-co2}"
VENV="$ROOT/venv/bin/python"
WORKERS="${WORKERS:-3}"

for DAY in "$@"; do
  ISO=$(echo "$DAY" | tr '.' '-')
  if [ -f "$ROOT/data/flights/$ISO/flights.parquet" ]; then
    echo "$(date +%FT%T) $DAY already done, skipping"
    continue
  fi
  echo "$(date +%FT%T) ==== backfill $DAY ===="
  if ! bash "$ROOT/scripts/dl_day.sh" "$DAY"; then
    echo "$(date +%FT%T) download failed for $DAY, skipping"
    continue
  fi
  if WORKERS="$WORKERS" nice -n15 ionice -c3 "$VENV" "$ROOT/pipeline/run_daily.py" --day "$DAY"; then
    echo "$(date +%FT%T) $DAY OK; deleting raw"
    rm -f "$ROOT/data/raw/v${DAY}-planes-readsb-prod-0.tar."*
  else
    echo "$(date +%FT%T) pipeline failed for $DAY; raw kept"
  fi
done
echo "$(date +%FT%T) backfill complete"
