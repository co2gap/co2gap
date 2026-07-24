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
# The dump is split into a day-dependent number of ~2 GB parts (aa, ab, [ac], ...).
# Download parts until one 404s; the first part missing is a real error.
got=0
for p in aa ab ac ad ae af; do
  f="${TAG}.tar.${p}"
  echo "$(date -Is) downloading $f"
  # -c2 -n7 (best-effort, low prio): yields to containers but is not starved to
  # a crawl the way the idle class (-c3) throttles a large sequential WD write.
  if ionice -c2 -n7 nice -n15 curl -fL --no-progress-meter --retry 5 --retry-delay 10 -C - \
      -o "$f" "${BASE}/${TAG}.tar.${p}"; then
    got=$((got+1))
  else
    rm -f "$f"
    if [ "$got" -eq 0 ]; then
      echo "$(date -Is) ERROR: first part $f not found"; exit 1
    fi
    echo "$(date -Is) $f absent -> $got part(s) total"
    break
  fi
done
echo "$(date -Is) DONE $TAG ($got parts)"
