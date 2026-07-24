#!/bin/bash
set -e
cd /mnt/wd_elements/adsb-co2
python3 -m venv venv
. venv/bin/activate
pip install --upgrade pip wheel setuptools >/tmp/pip.log 2>&1
echo "=== installing core ==="
pip install "numpy" "pandas" "pyarrow" "scipy" "requests" >>/tmp/pip.log 2>&1
echo "=== installing openap ==="
pip install openap >>/tmp/pip.log 2>&1
echo "=== versions ==="
python -c "import numpy,pandas,pyarrow,scipy,openap; print(numpy,numpy.__version__);print(pandas,pandas.__version__);print(\"pyarrow\",pyarrow.__version__);print(\"scipy\",scipy.__version__);print(\"openap\",openap.__version__)"
echo "SETUP_DONE"
