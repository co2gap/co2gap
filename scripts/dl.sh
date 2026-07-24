#!/bin/bash
set -e
cd /mnt/wd_elements/adsb-co2/data/raw
for p in aa ab ac; do
  echo "$(date) downloading $p"
  ionice -c3 nice -n15 curl -fL --retry 5 -C - -o v2026.07.23-planes-readsb-prod-0.tar.$p     "https://github.com/adsblol/globe_history_2026/releases/download/v2026.07.23-planes-readsb-prod-0/v2026.07.23-planes-readsb-prod-0.tar.$p"
done
echo "$(date) DONE"
