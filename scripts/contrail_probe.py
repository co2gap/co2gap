#!/usr/bin/env python3
"""Quanto sposta la correzione di umidita' la frequenza delle regioni sovrasature?

La sonda ERA5 ha stabilito che dai campi grezzi si ricava RHi. Ma ERA5 ha un
bias secco noto in alta troposfera: sottostima proprio le regioni sovrasature,
che sono la condizione di persistenza di una scia. La letteratura lo corregge,
e pycontrails porta quelle correzioni.

Questa sonda misura la differenza fra prima e dopo su un giorno solo, per
sapere se e' un ritocco o un fattore — prima di impegnare ore di scaricamento.

Gira nel venv separato: contrail-venv/bin/python scripts/contrail_probe.py
"""
import os
import sys
from pathlib import Path

import numpy as np
import xarray as xr

ROOT = Path(__file__).resolve().parents[1]
NC = Path(os.environ.get("ERA5_PROBE_NC")
          or (ROOT / "data/era5_probe/2026-01-15_tq.nc"))
LEVEL = int(os.environ.get("PROBE_LEVEL", "250"))


def esat_ice_pa(t_k):
    """Murphy & Koop (2005), Pa. La stessa formula della sonda ERA5."""
    return np.exp(9.550426 - 5723.265 / t_k + 3.53068 * np.log(t_k)
                  - 0.00728332 * t_k)


def rhi_from(t, q, p_pa):
    e = q * p_pa / (0.622 + 0.378 * q)
    return 100.0 * e / esat_ice_pa(t)


def main():
    if not NC.exists():
        sys.exit(f"manca {NC}: lancia prima scripts/era5_probe.py")
    ds = xr.open_dataset(NC)

    # --- riferimento: RHi dai campi grezzi, calcolato da noi ----------------
    t = ds["t"].sel(pressure_level=LEVEL)
    q = ds["q"].sel(pressure_level=LEVEL)
    raw = rhi_from(t, q, LEVEL * 100.0)
    print(f"file      {NC.name} · livello {LEVEL} hPa · "
          f"{ds.sizes['valid_time']} ore")
    print(f"\nERA5 grezzo   RHi mediana {float(raw.median()):5.1f}%   "
          f"celle sovrasature {float((raw > 100).mean())*100:5.2f}%")

    # --- che correzioni offre pycontrails? ---------------------------------
    try:
        from pycontrails.models import humidity_scaling as hs
    except Exception as exc:                     # pragma: no cover
        sys.exit(f"pycontrails non importabile: {exc}")

    names = [n for n in dir(hs)
             if n.endswith("HumidityScaling") or "Scaling" in n or "Matching" in n]
    print(f"\ncorrezioni disponibili in pycontrails:\n  " + "\n  ".join(sorted(names)))

    from pycontrails import MetDataset

    # pycontrails vuole i nomi CF e il livello in hPa chiamato "level".
    met = ds.rename({"t": "air_temperature", "q": "specific_humidity",
                     "pressure_level": "level", "valid_time": "time"})
    met = MetDataset(met.transpose("longitude", "latitude", "level", "time"))

    for cls_name in ("ExponentialBoostHumidityScaling",
                     "HistogramMatching",
                     "ConstantHumidityScaling"):
        cls = getattr(hs, cls_name, None)
        if cls is None:
            continue
        try:
            model = cls()
            out = model.eval(met.copy())
            qc = out["specific_humidity"].data.sel(level=LEVEL)
            tc = out["air_temperature"].data.sel(level=LEVEL)
            corr = rhi_from(tc, qc, LEVEL * 100.0)
            print(f"\n{cls_name:34s} RHi mediana {float(corr.median()):5.1f}%   "
                  f"celle sovrasature {float((corr > 100).mean())*100:5.2f}%")
        except Exception as exc:
            print(f"\n{cls_name:34s} non applicabile qui: "
                  f"{type(exc).__name__}: {str(exc)[:120]}")


if __name__ == "__main__":
    sys.exit(main())
