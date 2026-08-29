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
ASSET_PY="${ADSB_ASSET_PY:-$ROOT/venv/bin/python}"

mkdir -p "$DEST"
cd "$DEST" || exit 1

asset_list="$DEST/.${TAG}.assets.$$"
trap 'rm -f "$asset_list"' EXIT
if "$ASSET_PY" "$ROOT/scripts/release_assets.py" "$TAG" >"$asset_list"; then
    :
else
    rc=$?
    [ "$rc" -eq 44 ] && echo "$(date -Is) ERRORE: release $TAG inesistente"
    [ "$rc" -eq 44 ] || echo "$(date -Is) ERRORE: manifesto asset non disponibile"
    exit "$rc"
fi
NAMES=(); SIZES=(); URLS=()
while IFS=$'\t' read -r name size url; do
    [ -n "$name" ] || continue
    NAMES+=("$name"); SIZES+=("$size"); URLS+=("$url")
done <"$asset_list"
[ ${#NAMES[@]} -gt 0 ] || { echo "$(date -Is) ERRORE: manifesto vuoto"; exit 1; }

# Fetch every declared part at once. Stale files are removed first so a truncated
#    part from an interrupted run is never resumed into a corrupt tar.
echo "$(date -Is) scarico ${#NAMES[@]} parti in parallelo: $TAG"
pids=()
for idx in "${!NAMES[@]}"; do
    name="${NAMES[$idx]}"
    rm -f "$name"
    curl -fL --no-progress-meter --retry 5 --retry-delay 10 \
         -o "$name" "${URLS[$idx]}" &
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

for idx in "${!NAMES[@]}"; do
    actual=$(wc -c <"${NAMES[$idx]}" | tr -d ' ')
    if [ "$actual" != "${SIZES[$idx]}" ]; then
        echo "$(date -Is) ERRORE: ${NAMES[$idx]} ha $actual byte, attesi ${SIZES[$idx]}"
        rm -f "${NAMES[@]}"
        exit 1
    fi
done

echo "$(date -Is) DONE $TAG (${#NAMES[@]} parti in parallelo, dimensioni verificate)"
