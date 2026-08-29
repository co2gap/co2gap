#!/bin/bash
# Nightly accumulation job for the ADS-B CO2 observatory (runs on the Pi at 02:00).
#
# For each of the last CATCHUP_DAYS days that has no parquet yet, download the
# adsb.lol dump (if published) and run the daily pipeline, oldest-missing first,
# capped at MAX_PER_RUN days per night. This gives automatic retry of days that
# were not yet published (or failed) without unbounded work. Raw dumps older
# than RAW_RETENTION_DAYS are rotated; the parquet output is kept forever.
#
# Single-instance via flock. All output tee'd to a dated log.
set -uo pipefail

ROOT="${ADSB_ROOT:-/mnt/wd_elements/adsb-co2}"
VENV="$ROOT/venv/bin/python"
# Output directory must follow the box being accumulated. Hardcoding
# data/flights here while run_daily.py writes to data/flights_ecac would make
# the "already done" test look in the wrong directory and reprocess every day
# forever.
FLIGHTS_DIR="${ADSB_FLIGHTS_DIR:-$ROOT/data/flights}"
WORKERS="${WORKERS:-3}"
CATCHUP_DAYS="${CATCHUP_DAYS:-5}"
MAX_PER_RUN="${MAX_PER_RUN:-2}"
RAW_RETENTION_DAYS="${RAW_RETENTION_DAYS:-2}"

LOGDIR="$ROOT/logs"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/cron_$(date +%Y%m%d).log"
exec >>"$LOG" 2>&1

echo "==== $(date -Is) daily_cron start (workers=$WORKERS) ===="

# single instance
exec 9>"$ROOT/.cron.lock"
if ! flock -n 9; then
  echo "$(date -Is) another run holds the lock; exiting"
  exit 0
fi

available() {  # 0=manifest valid, 44=release absent, other=lookup failure
  local day="$1" tag="v${1}-planes-readsb-prod-0"
  "$VENV" "$ROOT/scripts/release_assets.py" "$tag" >/dev/null
}

processed=0
for off in $(seq 1 "$CATCHUP_DAYS"); do
  [ "$processed" -ge "$MAX_PER_RUN" ] && break
  DAY=$(date -u -d "$off days ago" +%Y.%m.%d 2>/dev/null || date -u -v-"${off}"d +%Y.%m.%d)
  ISO=$(date -u -d "$off days ago" +%Y-%m-%d 2>/dev/null || date -u -v-"${off}"d +%Y-%m-%d)
  # A day counts as done only if its parquet READS. Testing existence alone
  # marks a run killed mid-write (footer-less parquet) as complete and the day
  # is never retried again — exactly how 2026-03-27 was lost.
  if "$VENV" -c "import sys; sys.path.insert(0,sys.argv[1]+'/pipeline'); from store import validate_day_pair; validate_day_pair(sys.argv[2])" \
       "$ROOT" "$FLIGHTS_DIR/$ISO" 2>/dev/null; then
    continue  # already done
  fi
  echo "$(date -Is) target $DAY (missing parquet)"
  if available "$DAY"; then
    :
  else
    rc=$?
    if [ "$rc" -eq 44 ]; then
      echo "$(date -Is) dump for $DAY not published yet; will retry next run"
    else
      echo "$(date -Is) asset lookup FAILED for $DAY (rc=$rc); will retry next run"
    fi
    continue
  fi
  if ! bash "$ROOT/scripts/dl_day.sh" "$DAY"; then
    echo "$(date -Is) download FAILED for $DAY; will retry next run"
    continue
  fi
  echo "$(date -Is) running pipeline for $DAY"
  if WORKERS="$WORKERS" nice -n15 ionice -c2 -n7 "$VENV" "$ROOT/pipeline/run_daily.py" --day "$DAY"; then
    echo "$(date -Is) pipeline OK for $DAY"
    processed=$((processed+1))
  else
    echo "$(date -Is) pipeline FAILED for $DAY; raw kept for retry"
  fi
done

# rotate raw dumps older than RAW_RETENTION_DAYS (parquet kept forever)
find "$ROOT/data/raw" -maxdepth 1 -name '*.tar.a?' -mtime +"$RAW_RETENTION_DAYS" -print -delete \
  2>/dev/null | sed "s/^/$(date -Is) rotated: /"

echo "==== $(date -Is) daily_cron done (processed=$processed) ===="
