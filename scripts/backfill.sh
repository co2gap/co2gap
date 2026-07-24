#!/bin/bash
# Year-to-date backfill: rebuild per-flight parquet for every missing day, from
# the most recent day backwards to a start date. Parquet only — excess CO2 is
# recomputed later on the Mac in one batch (see README, two-machine split).
#
# Usage:  backfill.sh [FROM_DAY] [TO_DAY]        (ISO dates, walked backwards)
#         backfill.sh 2026-07-21 2026-01-01      (default)
#
# Design notes that matter:
#
#  * ONE DAY AT A TIME, whole cycle: download -> pipeline -> delete that day's
#    raw. Transient disk stays ~4 GB (one day's dump), never more.
#
#  * COEXISTENCE WITH THE 02:00 CRON is a hard requirement. Two mechanisms:
#      1. the same lock file, taken PER DAY, and taken with a WAIT
#         (flock -w) rather than -n. The cron keeps its non-blocking flock, so
#         the priority is asymmetric: the cron never queues behind us, we queue
#         behind the cron. If the cron fires while we sit between two days, it
#         takes the lock and we wait for it.
#      2. a quiet window: we never START a day between QUIET_FROM and QUIET_TO.
#         A day costs <=25 min worst case, so starting no later than 01:30
#         guarantees the lock is released well before 02:00.
#
#  * RESTART SAFETY. A day counts as done only if its flights.parquet is
#    READABLE (valid footer, >0 rows) — a pipeline killed mid-write leaves a
#    directory and a headerless file, which must not be mistaken for success.
#    On (re)start of a day we drop any leftover raw for that day and fetch
#    fresh, so a truncated part from a power cut can never be resumed into a
#    corrupt tar. Anything already valid is skipped, so the script is
#    idempotent and can be interrupted at any point.
set -uo pipefail

ROOT="${ADSB_ROOT:-/mnt/wd_elements/adsb-co2}"
VENV="$ROOT/venv/bin/python"
WORKERS="${WORKERS:-4}"
FROM_DAY="${1:-2026-07-21}"
TO_DAY="${2:-2026-01-01}"

LOCKFILE="$ROOT/.cron.lock"
LOG="$ROOT/logs/backfill.log"
VERBOSE="$ROOT/logs/backfill_verbose.log"   # full pipeline output, kept out of the one-line-per-day log
MISSING="$ROOT/data/backfill_missing.txt"
LOCK_WAIT_S=7200          # yield to the cron for up to 2 h
SLEEP_BETWEEN_S=20        # netiquette towards the GitHub release host
QUIET_FROM="0130"         # do not start a new day inside this window …
QUIET_TO="0330"           # … so the 02:00 cron always finds the lock free

mkdir -p "$ROOT/logs" "$ROOT/data"
touch "$MISSING"

log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

# A day is done only if the parquet is actually readable.
is_valid_day() {
    local iso="$1" f="$ROOT/data/flights/$1/flights.parquet"
    [ -f "$f" ] || return 1
    "$VENV" - "$f" <<'PY' 2>/dev/null
import sys
import pyarrow.parquet as pq
try:
    sys.exit(0 if pq.read_metadata(sys.argv[1]).num_rows > 0 else 1)
except Exception:
    sys.exit(1)
PY
}

published() {   # is the dump released? (HEAD the first part)
    local tag="v${1}-planes-readsb-prod-0"
    [ "$(curl -sIL -o /dev/null -w '%{http_code}' --max-time 30 \
        "https://github.com/adsblol/globe_history_2026/releases/download/${tag}/${tag}.tar.aa")" = "200" ]
}

wait_out_quiet_window() {
    while :; do
        local now; now=$(date +%H%M)
        # window does not cross midnight, plain string compare is fine
        if [[ "$now" > "$QUIET_FROM" && "$now" < "$QUIET_TO" ]]; then
            log "quiet window ($QUIET_FROM-$QUIET_TO), pausa: il cron delle 02:00 ha precedenza"
            sleep 300
        else
            return
        fi
    done
}

# ---------------------------------------------------------------- main loop --
n_ok=0; n_skip=0; n_missing=0; n_fail=0; n_done=0
t_start=$(date +%s)

log "==== BACKFILL START $FROM_DAY -> $TO_DAY (workers=$WORKERS) ===="

ISO="$FROM_DAY"
while [[ "$ISO" > "$TO_DAY" || "$ISO" == "$TO_DAY" ]]; do
    DAY="${ISO//-/.}"

    if is_valid_day "$ISO"; then
        n_skip=$((n_skip+1))
        ISO=$(date -u -d "$ISO -1 day" +%Y-%m-%d); continue
    fi
    if grep -qx "$ISO" "$MISSING"; then
        # already known absent from the release page; do not probe again in a
        # loop (delete the line in data/backfill_missing.txt to retry)
        n_skip=$((n_skip+1))
        ISO=$(date -u -d "$ISO -1 day" +%Y-%m-%d); continue
    fi

    wait_out_quiet_window

    if ! published "$DAY"; then
        echo "$ISO" >> "$MISSING"
        log "$ISO  MANCANTE (nessuna release su adsb.lol)"
        n_missing=$((n_missing+1))
        ISO=$(date -u -d "$ISO -1 day" +%Y-%m-%d)
        sleep 5; continue
    fi

    day_t0=$(date +%s)
    (
        # Wait for the lock: the cron (flock -n) must never queue behind us.
        flock -w "$LOCK_WAIT_S" 9 || { echo "LOCK_TIMEOUT"; exit 91; }

        # Re-check under the lock: the cron may have just built this very day
        # (its catch-up window overlaps the top of our range).
        if is_valid_day "$ISO"; then echo "RACE_DONE"; exit 92; fi

        # never resume a possibly-truncated part from an interrupted run
        rm -f "$ROOT/data/raw/v${DAY}-planes-readsb-prod-0.tar."*

        bash "$ROOT/scripts/dl_day.sh" "$DAY" >>"$VERBOSE" 2>&1 || exit 93

        WORKERS="$WORKERS" nice -n15 ionice -c3 \
            "$VENV" "$ROOT/pipeline/run_daily.py" --day "$DAY" >>"$VERBOSE" 2>&1 || exit 94

        rm -f "$ROOT/data/raw/v${DAY}-planes-readsb-prod-0.tar."*
    ) 9>"$LOCKFILE"
    rc=$?
    day_dt=$(( $(date +%s) - day_t0 ))

    case $rc in
      0)
        if is_valid_day "$ISO"; then
            nf=$("$VENV" -c "import pyarrow.parquet as pq;print(pq.read_metadata('$ROOT/data/flights/$ISO/flights.parquet').num_rows)" 2>/dev/null)
            log "$ISO  OK    voli=${nf:-?}  ${day_dt}s"
            n_ok=$((n_ok+1)); n_done=$((n_done+1))
        else
            log "$ISO  FALLITO (parquet non valido dopo il run), ${day_dt}s"
            rm -rf "$ROOT/data/flights/$ISO"
            n_fail=$((n_fail+1)); n_done=$((n_done+1))
        fi ;;
      91) log "$ISO  RIMANDATO (lock occupato oltre ${LOCK_WAIT_S}s)"; n_fail=$((n_fail+1)) ;;
      92) log "$ISO  già fatto dal cron mentre attendevo il lock"; n_skip=$((n_skip+1)) ;;
      93) log "$ISO  FALLITO (download), ${day_dt}s"
          rm -f "$ROOT/data/raw/v${DAY}-planes-readsb-prod-0.tar."*
          n_fail=$((n_fail+1)); n_done=$((n_done+1)) ;;
      94) log "$ISO  FALLITO (pipeline), ${day_dt}s"
          rm -rf "$ROOT/data/flights/$ISO"
          rm -f "$ROOT/data/raw/v${DAY}-planes-readsb-prod-0.tar."*
          n_fail=$((n_fail+1)); n_done=$((n_done+1)) ;;
      *)  log "$ISO  FALLITO (rc=$rc), ${day_dt}s"; n_fail=$((n_fail+1)); n_done=$((n_done+1)) ;;
    esac

    if [ "$n_done" -gt 0 ] && [ $((n_done % 30)) -eq 0 ]; then
        el=$(( ($(date +%s) - t_start) / 60 ))
        log "---- riepilogo: $n_done giorni processati ($n_ok ok, $n_fail falliti, \
$n_missing mancanti, $n_skip saltati) in ${el} min ----"
    fi

    ISO=$(date -u -d "$ISO -1 day" +%Y-%m-%d)
    sleep "$SLEEP_BETWEEN_S"
done

el=$(( ($(date +%s) - t_start) / 60 ))
log "==== BACKFILL FINE: $n_ok ok · $n_fail falliti · $n_missing mancanti · \
$n_skip saltati · ${el} min totali ===="
[ "$n_missing" -gt 0 ] && log "giorni senza release: vedi $MISSING"
exit 0
