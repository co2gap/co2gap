#!/usr/bin/env python3
"""Fase A su UN giorno: quanto costa, e quanta distanza vola nel sovrasaturo.

Misura, non stima. Prende le traiettorie vere di un giorno e l'ERA5 t/q dello
STESSO giorno, interpola lungo i waypoint, calcola RHi e la frazione di
DISTANZA percorsa in sovrasaturazione, per i tre rami di correzione decisi in
reports/scie_nox_metodo.md.

⚠️ Questo NON e' la persistenza: manca il criterio di Schmidt-Appleman, che
dipende dal motore (Allegato IIIb). La sovrasaturazione da sola e' un LIMITE
SUPERIORE della condizione da scia. Il costo di calcolo, invece, e' quello
vero: SAC aggiunge una formula puntuale, non un'altra interpolazione.

Serve a decidere Mac contro VPS, cioe' dove avviene il calcolo.

  contrail-venv/bin/python scripts/phasea_probe.py
"""
import os
import sys
import time
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import xarray as xr
from scipy.interpolate import RegularGridInterpolator

ROOT = Path(__file__).resolve().parents[1]
DAY = os.environ.get("PHASEA_DAY", "2026-01-15")
NC = Path(os.environ.get("PHASEA_NC") or (ROOT / f"data/era5_probe/{DAY}_tq.nc"))
PTS = Path(os.environ.get("PHASEA_POINTS")
           or (ROOT / f"data/flights_ecac/{DAY}/points.parquet"))

# I parametri della RSTS (DLR/DWD marzo 2025 §2.4.2), validi per dati ECMWF.
RSTS = dict(rhi_adj=0.9779, rhi_boost_exponent=1.635, clip_upper=1.65)
RAMI = [("ERA5 grezzo", None, {}),
        ("HistogramMatching", "HistogramMatching", {}),
        ("ExponentialBoost PRESCRITTA", "ExponentialBoostHumidityScaling", RSTS)]


def esat_ice_pa(t_k):
    """Murphy & Koop (2005), Pa."""
    return np.exp(9.550426 - 5723.265 / t_k + 3.53068 * np.log(t_k)
                  - 0.00728332 * t_k)


def rhi_from(t, q, p_pa):
    e = q * p_pa / (0.622 + 0.378 * q)
    return 100.0 * e / esat_ice_pa(t)


def haversine_km(lat1, lon1, lat2, lon2):
    r = np.radians
    dlat, dlon = r(lat2 - lat1), r(lon2 - lon1)
    a = (np.sin(dlat / 2) ** 2
         + np.cos(r(lat1)) * np.cos(r(lat2)) * np.sin(dlon / 2) ** 2)
    return 6371.0088 * 2 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def scala(ds, cls_name, kwargs):
    """Correzione di umidita' sulla GRIGLIA, come fa CoCiP.

    Torna DataArray con dimensioni NOMINATE e ordinate allo stesso modo per
    ogni ramo. ⚠️ MetDataset RIORDINA latitudine e livello in senso crescente,
    mentre ERA5 li da' decrescenti: gestire gli assi per posizione qui produce
    campi scambiati con la forma giusta, cioe' nessun errore e numeri falsi.
    """
    if cls_name is None:
        t = ds["t"].rename({"pressure_level": "level", "valid_time": "time"})
        q = ds["q"].rename({"pressure_level": "level", "valid_time": "time"})
    else:
        from pycontrails import MetDataset
        from pycontrails.models import humidity_scaling as hs
        met = ds.rename({"t": "air_temperature", "q": "specific_humidity",
                         "pressure_level": "level", "valid_time": "time"})
        met = MetDataset(met.transpose("longitude", "latitude", "level", "time"))
        cls = getattr(hs, cls_name)
        out = (cls(**kwargs) if kwargs else cls()).eval(met.copy())
        t = out["air_temperature"].data
        q = out["specific_humidity"].data
    ordina = lambda a: (a.sortby("latitude").sortby("level").sortby("time")
                        .transpose("time", "level", "latitude", "longitude"))
    return ordina(t), ordina(q)


def main():
    for f in (NC, PTS):
        if not f.exists():
            sys.exit(f"manca {f}")

    t0 = time.perf_counter()
    ds = xr.open_dataset(NC).load()
    t_load_met = time.perf_counter() - t0

    t0 = time.perf_counter()
    tab = pq.read_table(PTS, columns=["flight_id", "t", "lat", "lon", "alt_ft"])
    fid = tab["flight_id"].to_numpy(zero_copy_only=False)
    ts = tab["t"].to_numpy(zero_copy_only=False).astype("datetime64[s]").astype(np.int64)
    lat = tab["lat"].to_numpy(zero_copy_only=False).astype(np.float64)
    lon = tab["lon"].to_numpy(zero_copy_only=False).astype(np.float64)
    alt = tab["alt_ft"].to_numpy(zero_copy_only=False).astype(np.float64)
    t_load_pts = time.perf_counter() - t0
    n = len(lat)

    # --- distanza per waypoint: meta' del segmento prima + meta' dopo -------
    t0 = time.perf_counter()
    seg = np.zeros(n)
    same = fid[1:] == fid[:-1]
    d = haversine_km(lat[:-1], lon[:-1], lat[1:], lon[1:])
    d = np.where(same, d, 0.0)
    seg[:-1] += d / 2.0
    seg[1:] += d / 2.0
    dist_tot = seg.sum()
    t_dist = time.perf_counter() - t0

    # --- quota -> pressione (ISA), come fa pycontrails ---------------------
    from pycontrails.physics.units import ft_to_pl
    p_hpa = ft_to_pl(alt)

    lv = ds.pressure_level.values.astype(float)
    in_band = (p_hpa <= lv.max()) & (p_hpa >= lv.min())

    pts = np.column_stack([ts[in_band], p_hpa[in_band],
                           lat[in_band], lon[in_band]])
    print(f"giorno {DAY}")
    print(f"  waypoint            {n:,}")
    print(f"  entro 150-400 hPa   {in_band.sum():,} "
          f"({in_band.mean()*100:.1f}%)")
    print(f"  distanza totale     {dist_tot:,.0f} km")
    print(f"  caricamento         meteo {t_load_met:.1f}s · punti "
          f"{t_load_pts:.1f}s · distanze {t_dist:.1f}s")

    t_rif = None
    for etichetta, cls_name, kwargs in RAMI:
        t0 = time.perf_counter()
        ta, qa = scala(ds, cls_name, kwargs)      # DataArray (time, level, lat, lon)

        # 🔒 invariante: nessuna correzione di umidita' tocca la temperatura.
        # E' il controllo che smaschera un riordino di assi sbagliato, che
        # altrimenti passa in silenzio con la forma giusta.
        if t_rif is None:
            t_rif = ta.values
        elif not np.allclose(t_rif, ta.values, equal_nan=True):
            sys.exit(f"{etichetta}: la temperatura e' cambiata -> assi disallineati")

        grid = (ta.time.values.astype("datetime64[s]").astype(np.int64),
                ta.level.values.astype(float),
                ta.latitude.values.astype(float),
                ta.longitude.values.astype(float))
        it = RegularGridInterpolator(grid, ta.values, bounds_error=False,
                                     fill_value=np.nan)
        iq = RegularGridInterpolator(grid, qa.values, bounds_error=False,
                                     fill_value=np.nan)
        tv, qv = it(pts), iq(pts)
        rhi = rhi_from(tv, qv, pts[:, 1] * 100.0)
        dt = time.perf_counter() - t0

        w = seg[in_band]
        ok = np.isfinite(rhi)
        iss = (rhi > 100) & ok
        frac_dist = w[iss].sum() / dist_tot * 100.0
        print(f"\n{etichetta:30s} {dt:5.1f}s")
        print(f"  frazione di DISTANZA sovrasatura   {frac_dist:5.2f}%")
        print(f"  waypoint sovrasaturi               "
              f"{iss.sum()/max(ok.sum(),1)*100:5.2f}%  (non pesati)")

    print(f"\n⚠️ senza Schmidt-Appleman: limite SUPERIORE, non la persistenza.")


if __name__ == "__main__":
    sys.exit(main())
