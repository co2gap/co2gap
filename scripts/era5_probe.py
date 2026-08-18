#!/usr/bin/env python3
"""Sonda: temperatura e umidita' specifica ai livelli di crociera, un giorno.

Risponde a tre domande PRIMA di impegnare giorni di scaricamento:
  1. il CDS espone davvero `temperature` e `specific_humidity` su quei livelli?
  2. quanto pesano davvero, dato che l'umidita' comprime peggio del vento?
  3. i dati bastano a derivare la sovrasaturazione rispetto al ghiaccio, che e'
     la condizione che fa PERSISTERE una scia?

Non tocca la produzione e scrive in una cartella sua: i file ERA5 si chiamano
solo YYYY-MM-DD.nc, senza box ne' variabili nel nome, quindi due configurazioni
nella stessa cartella si sovrascriverebbero in silenzio.
"""
import os
import sys
import time
from pathlib import Path

import cdsapi
import numpy as np
import xarray as xr

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = Path(os.environ.get("ERA5_PROBE_DIR") or (ROOT / "data/era5_probe"))
DAY = os.environ.get("ERA5_PROBE_DAY", "2026-01-15")
AREA = [float(x) for x in os.environ.get("ERA5_AREA", "72,-32,27,45").split(",")]
# Piu' fitti dove le scie si formano: la maglia usata per il vento ha solo
# 5 livelli sopra i 400 hPa, e gli strati sovrasaturi sono sottili.
LEVELS = [400, 350, 300, 250, 225, 200, 175, 150]
VARS = ["temperature", "specific_humidity"]


def esat_ice_pa(t_k):
    """Tensione di vapore saturo sul ghiaccio, Murphy & Koop (2005), in Pa."""
    return np.exp(9.550426 - 5723.265 / t_k + 3.53068 * np.log(t_k)
                  - 0.00728332 * t_k)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{DAY}_tq.nc"
    y, m, d = DAY.split("-")
    if not out.exists():
        print(f"richiesta: {VARS}\n  {len(LEVELS)} livelli {LEVELS}\n"
              f"  area {AREA} · giorno {DAY}", flush=True)
        t0 = time.time()
        cdsapi.Client().retrieve("reanalysis-era5-pressure-levels", {
            "product_type": "reanalysis",
            "variable": VARS,
            "pressure_level": [str(x) for x in LEVELS],
            "year": y, "month": m, "day": d,
            "time": [f"{h:02d}:00" for h in range(24)],
            "area": AREA, "grid": [0.25, 0.25],
            "data_format": "netcdf",
        }, str(out))
        print(f"\nscaricato in {time.time() - t0:.0f} s")
    else:
        print(f"gia' presente: {out}")

    ds = xr.open_dataset(out)
    mb = out.stat().st_size / 1e6
    print(f"\nfile      {out.name}  {mb:.1f} MB")
    print(f"variabili {list(ds.data_vars)}")
    print(f"livelli   {ds.pressure_level.values.astype(int).tolist()}")
    print(f"tempi     {ds.sizes.get('valid_time')} · griglia "
          f"{ds.sizes.get('latitude')}x{ds.sizes.get('longitude')}")

    per_var_day = mb / max(len(ds.data_vars), 1)
    print(f"\n{per_var_day:.1f} MB per variabile al giorno su {len(LEVELS)} "
          f"livelli · il vento ne fa 35,1 su 11 livelli")
    for lab, days in (("197 giorni (finestra attuale)", 197),
                      ("365 giorni (finestra di gennaio)", 365)):
        print(f"  t+q su {lab}: {mb * days / 1000:.1f} GB")

    # La prova che conta: si riesce a derivare la sovrasaturazione sul ghiaccio?
    t = ds["t"].sel(pressure_level=250).isel(valid_time=12)
    q = ds["q"].sel(pressure_level=250).isel(valid_time=12)
    p = 250 * 100.0
    e = q * p / (0.622 + 0.378 * q)          # tensione di vapore, Pa
    rhi = 100.0 * e / esat_ice_pa(t)
    frac = float((rhi > 100).mean()) * 100
    print(f"\nverifica fisica a 250 hPa, 12 UTC del {DAY}:")
    print(f"  T mediana         {float(t.median()) - 273.15:+.1f} C")
    print(f"  RHi mediana       {float(rhi.median()):.0f}%")
    print(f"  celle sovrasature {frac:.1f}% del riquadro")
    print("  (attesi pochi punti percentuali fino a ~20%: zero o valori "
          "altissimi indicherebbero un errore di unita' o di formula)")


if __name__ == "__main__":
    sys.exit(main())
