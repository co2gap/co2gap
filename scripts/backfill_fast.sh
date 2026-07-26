#!/bin/bash
# Backfill variant for a dedicated cloud box (no other tenants on the machine).
#
# Differences from scripts/backfill.sh, which stays exactly as it is because it
# is tuned for the Pi and already proven there:
#
#   * NO cron coexistence and NO quiet window. On the Pi the nightly job must
#     never be starved; here nothing else runs, so both are pure overhead.
#   * DOWNLOAD PREFETCH. Measured on the target box the download (~131 s for a
#     4.3 GB dump at ~33 MB/s) is LONGER than the pipeline (~90-110 s with 8
#     fast cores), so a strictly sequential download->process loop would spend
#     more than half its time waiting on GitHub. We therefore fetch day N+1
#     while day N is being processed: the wall clock per day collapses to
#     max(download, pipeline) instead of their sum (~45% saved).
#
# What is deliberately KEPT from the Pi version, because this session proved
# each one matters:
#   * a day counts as done only if its parquet is READABLE (valid footer,
#     rows > 0) — a pipeline killed mid-write leaves a headerless file that a
#     mere existence check would mistake for success;
#   * stale raw parts for a day are deleted before (re)downloading, so a
#     truncated file from an interrupted run can never be resumed into a
#     corrupt tar;
#   * a single-instance lock, so a second launch refuses instead of silently
#     racing the first;
#   * one line per day in the log, verbose pipeline output kept separate.
#
# Usage:  ADSB_ROOT=/opt/adsb-co2 WORKERS=8 backfill_fast.sh 2026-03-26 2026-01-01
set -uo pipefail

ROOT="${ADSB_ROOT:-/opt/adsb-co2}"
VENV="$ROOT/venv/bin/python"
WORKERS="${WORKERS:-8}"
FROM_DAY="${1:?uso: backfill_fast.sh FROM_DAY TO_DAY (ISO, si percorre a ritroso)}"
TO_DAY="${2:?uso: backfill_fast.sh FROM_DAY TO_DAY (ISO, si percorre a ritroso)}"

LOG="$ROOT/logs/backfill.log"
VERBOSE="$ROOT/logs/backfill_verbose.log"
MISSING="$ROOT/data/backfill_missing.txt"
GLOBAL_LOCK="$ROOT/.backfill.single.lock"

mkdir -p "$ROOT/logs" "$ROOT/data/raw" "$ROOT/data/flights"
touch "$MISSING"

log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

# ---- single instance --------------------------------------------------------
# Held on fd 8 for the whole process; the kernel releases it on exit, so a crash
# cannot leave it stuck the way a pid-file would. 8>> and not 8>: '>' truncates
# on open, before flock is attempted, so a second instance would wipe the
# holder's pid before being refused.
command -v flock >/dev/null || { log "flock non disponibile"; exit 3; }
exec 8>>"$GLOBAL_LOCK"
if ! flock -n 8; then
    log "un altro backfill e' gia' in esecuzione (PID $(cat "$GLOBAL_LOCK" 2>/dev/null || echo '?')) -> esco"
    exit 0
fi
echo $$ > "$GLOBAL_LOCK"

raw_glob() { echo "$ROOT/data/raw/v${1//-/.}-planes-readsb-prod-0.tar."*; }

drop_raw() { rm -f "$ROOT/data/raw/v${1//-/.}-planes-readsb-prod-0.tar."*; }

is_valid_day() {
    local f="$ROOT/data/flights/$1/flights.parquet"
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

published() {
    local tag="v${1//-/.}-planes-readsb-prod-0"
    [ "$(curl -sIL -o /dev/null -w '%{http_code}' --max-time 30 \
        "https://github.com/adsblol/globe_history_2026/releases/download/${tag}/${tag}.tar.aa")" = "200" ]
}

# ---- prefetch ---------------------------------------------------------------
# DL_PID/DL_DAY must be set by a function called in the CURRENT shell, not in a
# $(...) subshell: `wait` only works on direct children of this shell.
DL_PID=""
DL_DAY=""

start_download() {
    local iso="$1"
    drop_raw "$iso"
    bash "$ROOT/scripts/dl_day_fast.sh" "${iso//-/.}" >>"$VERBOSE" 2>&1 &
    DL_PID=$!
    DL_DAY="$iso"
}

# ---- build the work list up front -------------------------------------------
# Cheap (a local parquet check per day) and it lets the prefetch know which day
# comes next without re-deriving it mid-loop.
DAYS=()
ISO="$FROM_DAY"
n_already=0
while [[ "$ISO" > "$TO_DAY" || "$ISO" == "$TO_DAY" ]]; do
    if is_valid_day "$ISO"; then
        n_already=$((n_already+1))
    elif grep -qx "$ISO" "$MISSING"; then
        n_already=$((n_already+1))
    else
        DAYS+=("$ISO")
    fi
    ISO=$(date -u -d "$ISO -1 day" +%Y-%m-%d)
done

t_start=$(date +%s)
n_ok=0; n_fail=0; n_missing=0
log "==== BACKFILL FAST $FROM_DAY -> $TO_DAY : ${#DAYS[@]} giorni da fare, \
$n_already gia' presenti (workers=$WORKERS, prefetch attivo) ===="
[ ${#DAYS[@]} -eq 0 ] && { log "niente da fare"; exit 0; }

for idx in "${!DAYS[@]}"; do
    iso="${DAYS[$idx]}"
    day_t0=$(date +%s)

    # The download for this day was started at the end of the previous
    # iteration. Only the first day has to start (and wait for) its own.
    if [ "$DL_DAY" != "$iso" ]; then
        start_download "$iso"
    fi
    wait "$DL_PID"; dl_rc=$?
    DL_PID=""; DL_DAY=""

    if [ "$dl_rc" -ne 0 ]; then
        # dl_day.sh exits non-zero both when the release does not exist and on a
        # genuine network failure. Classify, so a missing day is recorded once
        # and never retried in a loop, while a transient error stays retryable.
        if published "$iso"; then
            log "$iso  FALLITO (download), $(( $(date +%s) - day_t0 ))s"
            n_fail=$((n_fail+1))
        else
            echo "$iso" >> "$MISSING"
            log "$iso  MANCANTE (nessuna release su adsb.lol)"
            n_missing=$((n_missing+1))
        fi
        drop_raw "$iso"
        # still prefetch the next one before moving on
        next="${DAYS[$((idx+1))]:-}"
        [ -n "$next" ] && start_download "$next"
        continue
    fi

    # Overlap: fetch the next day while this one is being modelled.
    next="${DAYS[$((idx+1))]:-}"
    [ -n "$next" ] && start_download "$next"

    if WORKERS="$WORKERS" "$VENV" "$ROOT/pipeline/run_daily.py" --day "${iso//-/.}" \
            >>"$VERBOSE" 2>&1 && is_valid_day "$iso"; then
        nf=$("$VENV" -c "import pyarrow.parquet as pq;print(pq.read_metadata('$ROOT/data/flights/$iso/flights.parquet').num_rows)" 2>/dev/null)
        log "$iso  OK    voli=${nf:-?}  $(( $(date +%s) - day_t0 ))s"
        n_ok=$((n_ok+1))
    else
        log "$iso  FALLITO (pipeline), $(( $(date +%s) - day_t0 ))s"
        rm -rf "$ROOT/data/flights/$iso"
        n_fail=$((n_fail+1))
    fi
    drop_raw "$iso"

    done_n=$((n_ok+n_fail+n_missing))
    if [ $((done_n % 30)) -eq 0 ]; then
        el=$(( ($(date +%s) - t_start) / 60 ))
        left=$(( ${#DAYS[@]} - done_n ))
        eta=$(( left * (($(date +%s) - t_start)) / done_n / 60 ))
        log "---- $done_n/${#DAYS[@]} fatti ($n_ok ok, $n_fail falliti, \
$n_missing mancanti) in ${el} min · ~${eta} min rimanenti ----"
    fi
done

# a prefetch may still be running if the last day failed early
[ -n "$DL_PID" ] && { wait "$DL_PID" 2>/dev/null; drop_raw "$DL_DAY"; }

el=$(( ($(date +%s) - t_start) / 60 ))
log "==== FINE: $n_ok ok · $n_fail falliti · $n_missing mancanti · ${el} min ===="
[ "$n_missing" -gt 0 ] && log "giorni senza release: vedi $MISSING"
exit 0
