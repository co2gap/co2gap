#!/bin/bash
# Download one adsb.lol day dump (3 split-tar parts) into data/raw, resumable.
# Usage: dl_day.sh 2026.07.19
set -euo pipefail

DAY="${1:?usage: dl_day.sh YYYY.MM.DD}"
ROOT="${ADSB_ROOT:-/mnt/wd_elements/adsb-co2}"
TAG="v${DAY}-planes-readsb-prod-0"
DEST="$ROOT/data/raw"
ASSET_PY="${ADSB_ASSET_PY:-$ROOT/venv/bin/python}"

mkdir -p "$DEST"
cd "$DEST"
asset_list="$DEST/.${TAG}.assets.$$"
trap 'rm -f "$asset_list"' EXIT
if "$ASSET_PY" "$ROOT/scripts/release_assets.py" "$TAG" >"$asset_list"; then
  :
else
  rc=$?
  [ "$rc" -eq 44 ] && echo "$(date -Is) ERROR: release $TAG not found"
  [ "$rc" -eq 44 ] || echo "$(date -Is) ERROR: cannot obtain asset manifest for $TAG"
  exit "$rc"
fi

got=0
while IFS=$'\t' read -r f expected url; do
  [ -n "$f" ] || continue
  actual=$([ -f "$f" ] && wc -c <"$f" | tr -d ' ' || echo 0)
  if [ "$actual" = "$expected" ]; then
    echo "$(date -Is) verified existing $f ($expected bytes)"
    got=$((got+1)); continue
  fi
  [ "$actual" -le "$expected" ] || rm -f "$f"
  echo "$(date -Is) downloading $f ($expected bytes)"
  # -c2 -n7 (best-effort, low prio): yields to containers but is not starved to
  # a crawl the way the idle class (-c3) throttles a large sequential WD write.
  ionice -c2 -n7 nice -n15 curl -fL --no-progress-meter --retry 5 --retry-delay 10 -C - \
      -o "$f" "$url" || { echo "$(date -Is) ERROR: download failed for $f"; exit 1; }
  actual=$(wc -c <"$f" | tr -d ' ')
  [ "$actual" = "$expected" ] || {
    echo "$(date -Is) ERROR: $f has $actual bytes, expected $expected"; rm -f "$f"; exit 1;
  }
  got=$((got+1))
done <"$asset_list"
[ "$got" -gt 0 ] || { echo "$(date -Is) ERROR: empty asset manifest"; exit 1; }
echo "$(date -Is) DONE $TAG ($got parts)"
