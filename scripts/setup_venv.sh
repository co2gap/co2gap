#!/bin/bash
set -e
cd /mnt/wd_elements/adsb-co2
python3 -m venv venv
. venv/bin/activate
pip install --upgrade pip wheel setuptools >/tmp/pip.log 2>&1
echo "=== installing core ==="
pip install -r requirements-pi.lock >>/tmp/pip.log 2>&1
echo "=== versions ==="
python scripts/check_environment.py pi
echo "SETUP_DONE"
