#!/bin/bash
# Parallel-part downloader, for a cloud box with real bandwidth.
#
# Why this exists next to dl_day.sh instead of replacing it: GitHub throttles
# each connection to ~28-33 MB/s but scales almost linearly with concurrency.
# Measured on the target box against a release asset:
#     1 connection   28 MB/s
#     2 connections  62 MB/s
#     4 connections 105 MB/s
# The dump is already split into 2-3 parts, so fetching the parts concurrently
# — instead of one after the other — cuts the download to a third WITHOUT range
# trickery and without opening more sockets than the file naturally has. On the
# Pi this would be pointless (the 20 MB/s there was the home line, not GitHub)
# and would fight the WD for I/O, so dl_day.sh stays sequential and untouched.
#
# Usage: dl_day_fast.sh 2026.03.24
set -uo pipefail

DAY="${1:?uso: dl_day_fast.sh YYYY.MM.DD}"
ROOT="${ADSB_ROOT:-/opt/adsb-co2}"
TAG="v${DAY}-planes-readsb-prod-0"
DEST="$ROOT/data/raw"
BASE="https://github.com/adsblol/globe_history_2026/releases/download/${TAG}"

mkdir -p "$DEST"
cd "$DEST" || exit 1

# 1) Discover how many parts this day has. The count varies per day (2 or 3),
#    and assuming a fixed 3 silently skipped every 2-part day in an earlier
#    version of this pipeline. A HEAD is ~0.2 s, so probing in order is cheap.
PARTS=()
for p in aa ab ac ad ae af; do
    code=$(curl -sIL -o /dev/null -w '%{http_code}' --max-time 30 "${BASE}/${TAG}.tar.${p}")
    [ "$code" = "200" ] || break
    PARTS+=("$p")
done

if [ ${#PARTS[@]} -eq 0 ]; then
    echo "$(date -Is) ERRORE: nessuna parte trovata per $TAG (release inesistente?)"
    exit 1
fi

# 2) Fetch every part at once. Stale files are removed first so a truncated
#    part from an interrupted run is never resumed into a corrupt tar.
echo "$(date -Is) scarico ${#PARTS[@]} parti in parallelo: $TAG"
pids=()
for p in "${PARTS[@]}"; do
    rm -f "${TAG}.tar.${p}"
    curl -fL --no-progress-meter --retry 5 --retry-delay 10 \
         -o "${TAG}.tar.${p}" "${BASE}/${TAG}.tar.${p}" &
    pids+=($!)
done

rc=0
for pid in "${pids[@]}"; do
    wait "$pid" || rc=1
done

if [ "$rc" -ne 0 ]; then
    echo "$(date -Is) ERRORE: download fallito per $TAG, rimuovo i parziali"
    rm -f "${TAG}.tar."*
    exit 1
fi

echo "$(date -Is) DONE $TAG (${#PARTS[@]} parti in parallelo)"
