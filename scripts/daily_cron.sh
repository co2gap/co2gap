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

available() {  # is the dump published on the release? (HEAD one part)
  local day="$1" tag="v${1}-planes-readsb-prod-0"
  local code
  code=$(curl -sIL -o /dev/null -w "%{http_code}" --max-time 30 \
    "https://github.com/adsblol/globe_history_2026/releases/download/${tag}/${tag}.tar.aa")
  [ "$code" = "200" ]
}

processed=0
for off in $(seq 1 "$CATCHUP_DAYS"); do
  [ "$processed" -ge "$MAX_PER_RUN" ] && break
  DAY=$(date -u -d "$off days ago" +%Y.%m.%d 2>/dev/null || date -u -v-"${off}"d +%Y.%m.%d)
  ISO=$(date -u -d "$off days ago" +%Y-%m-%d 2>/dev/null || date -u -v-"${off}"d +%Y-%m-%d)
  if [ -f "$ROOT/data/flights/$ISO/flights.parquet" ]; then
    continue  # already done
  fi
  echo "$(date -Is) target $DAY (missing parquet)"
  if ! available "$DAY"; then
    echo "$(date -Is) dump for $DAY not published yet; will retry next run"
    continue
  fi
  if ! bash "$ROOT/scripts/dl_day.sh" "$DAY"; then
    echo "$(date -Is) download FAILED for $DAY; will retry next run"
    continue
  fi
  echo "$(date -Is) running pipeline for $DAY"
  if WORKERS="$WORKERS" nice -n15 ionice -c3 "$VENV" "$ROOT/pipeline/run_daily.py" --day "$DAY"; then
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
