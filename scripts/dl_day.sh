#!/bin/bash
# Download one adsb.lol day dump (3 split-tar parts) into data/raw, resumable.
# Usage: dl_day.sh 2026.07.19
set -euo pipefail

DAY="${1:?usage: dl_day.sh YYYY.MM.DD}"
ROOT="${ADSB_ROOT:-/mnt/wd_elements/adsb-co2}"
TAG="v${DAY}-planes-readsb-prod-0"
DEST="$ROOT/data/raw"
BASE="https://github.com/adsblol/globe_history_2026/releases/download/${TAG}"

mkdir -p "$DEST"
cd "$DEST"
for p in aa ab ac; do
  f="${TAG}.tar.${p}"
  echo "$(date -Is) downloading $f"
  ionice -c3 nice -n15 curl -fL --retry 5 --retry-delay 10 -C - \
    -o "$f" "${BASE}/${TAG}.tar.${p}"
done
echo "$(date -Is) DONE $TAG"
