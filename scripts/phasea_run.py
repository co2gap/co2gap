#!/usr/bin/env python3
"""Fase A — condizioni da scia lungo le traiettorie reali, un giorno alla volta.

Decisioni di metodo: reports/scie_nox_metodo.md. In sintesi:
  * tre rami di correzione dell'umidita', ramo di riferimento = i parametri
    PRESCRITTI dalla RSTS (mai i default di pycontrails);
  * FORMAZIONE (Schmidt-Appleman) e PERSISTENZA (SAC and RHi>=100) misurate
    separate: la persistenza e' la quantita' primaria, la formazione un limite
    superiore diagnostico;
  * peso sulla DISTANZA, mai sul tempo, e denominatore = distanza totale del volo;
  * motore dall'Allegato IIIb (refs/), eta' da Poll-Schumann;
  * sotto i 400 hPa la temperatura e' quella dell'atmosfera standard: li' nessuna
    scia si forma, e lo scarico copre solo la banda delle scie.

⚠️ Assi gestiti per NOME e invariante sulla temperatura: MetDataset riordina
latitudine e livello in senso crescente mentre ERA5 li da' decrescenti, e la
forma resta identica -> campi scambiati senza alcun errore.

  contrail-venv/bin/python scripts/phasea_run.py [YYYY-MM-DD ...]
"""
from __future__ import annotations

import csv
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import xarray as xr
from scipy.interpolate import RegularGridInterpolator

ROOT = Path(__file__).resolve().parents[1]
TQ_DIR = Path(os.environ.get("ERA5_TQ_DIR") or (ROOT / "data/era5_tq_ecac"))
FLIGHTS_DIR = Path(os.environ.get("ADSB_FLIGHTS_DIR") or (ROOT / "data/flights_ecac"))
OUT_DIR = Path(os.environ.get("PHASEA_OUT") or (ROOT / "data/contrail_phasea"))
ENGINES = ROOT / "refs/annex_iiib_default_engines.csv"

# RSTS (DLR/DWD marzo 2025) §2.4.2, valida per dati ECMWF -> i nostri.
RSTS_HUM = dict(rhi_adj=0.9779, rhi_boost_exponent=1.635, clip_upper=1.65)
# RSTS Tab. 3: valori di default del carburante. NON i default di pycontrails
# (43,13 MJ/kg e EI_H2O 1,23), che divergono di ~1,7% su G una volta combinati.
Q_FUEL = 42.8e6          # J/kg, potere calorifico netto
EI_H2O = 9.0 * 0.1379    # kg/kg, da 13,79% di idrogeno in massa

RAMI = (("grezzo", None, {}),
        ("histogram", "HistogramMatching", {}),
        ("prescritta", "ExponentialBoostHumidityScaling", RSTS_HUM))

P0, A0, KAPPA = 101325.0, 340.294, 1.4


def esat_ice_pa(t_k):
    """Murphy & Koop (2005), Pa."""
    return np.exp(9.550426 - 5723.265 / t_k + 3.53068 * np.log(t_k)
                  - 0.00728332 * t_k)


def esat_liq_pa(t_k):
    """Murphy & Koop (2005) sull'acqua sopraffusa, Pa."""
    return np.exp(54.842763 - 6763.22 / t_k - 4.210 * np.log(t_k)
                  + 0.000367 * t_k
                  + np.tanh(0.0415 * (t_k - 218.8))
                  * (53.878 - 1331.22 / t_k - 9.44523 * np.log(t_k)
                     + 0.014025 * t_k))


def rh_from(t, q, p_pa, sat):
    e = q * p_pa / (0.622 + 0.378 * q)
    return e / sat(t)


def isa_temperature(alt_ft):
    """Atmosfera standard: usata SOLO sotto la banda delle scie."""
    h = np.asarray(alt_ft, dtype=float) * 0.3048
    return np.where(h < 11000.0, 288.15 - 0.0065 * h, 216.65)


def cas_to_tas(cas_ms, p_pa, t_k):
    """CAS -> TAS, conversione compressibile subsonica standard."""
    qc = P0 * ((1.0 + 0.2 * (cas_ms / A0) ** 2) ** 3.5 - 1.0)
    m = np.sqrt(np.maximum(5.0 * ((qc / p_pa + 1.0) ** (2.0 / 7.0) - 1.0), 0.0))
    return m * np.sqrt(KAPPA * 287.05 * t_k)


def haversine_km(lat1, lon1, lat2, lon2):
    r = np.radians
    dlat, dlon = r(lat2 - lat1), r(lon2 - lon1)
    a = (np.sin(dlat / 2) ** 2
         + np.cos(r(lat1)) * np.cos(r(lat2)) * np.sin(dlon / 2) ** 2)
    return 6371.0088 * 2 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def campi(ds, cls_name, kwargs):
    """t e q come DataArray con assi NOMINATI e ordinati uguali per ogni ramo."""
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
        t, q = out["air_temperature"].data, out["specific_humidity"].data
    o = lambda a: (a.sortby("latitude").sortby("level").sortby("time")
                   .transpose("time", "level", "latitude", "longitude"))
    return o(t), o(q)


def interpolatore(da):
    grid = (da.time.values.astype("datetime64[s]").astype(np.int64),
            da.level.values.astype(float),
            da.latitude.values.astype(float),
            da.longitude.values.astype(float))
    return RegularGridInterpolator(grid, da.values, bounds_error=False,
                                   fill_value=np.nan)


def eta_poll_schumann(pts, tipo_di, t_amb, tas):
    """Efficienza propulsiva per waypoint. Ritorna un array allineato a pts."""
    from pycontrails import Flight
    from pycontrails.models.ps_model import PSFlight
    ps = PSFlight()
    eta = np.full(len(pts), np.nan)
    pos = {fid: idx.to_numpy() for fid, idx in
           pd.Series(np.arange(len(pts)), index=pts["flight_id"].to_numpy())
             .groupby(level=0)}
    for fid, idx in pos.items():
        tc = tipo_di.get(fid)
        if tc is None or len(idx) < 5:
            continue
        try:
            f = Flight(longitude=pts["lon"].to_numpy()[idx],
                       latitude=pts["lat"].to_numpy()[idx],
                       time=pts["tt"].to_numpy()[idx],
                       altitude_ft=pts["alt_ft"].to_numpy()[idx],
                       flight_id=str(fid), aircraft_type=tc)
            f["air_temperature"] = t_amb[idx]
            f["true_airspeed"] = tas[idx]
            eta[idx] = np.asarray(ps.eval(f)["engine_efficiency"], dtype=float)
        except Exception:
            continue
    return eta


def un_giorno(day: str, riprendi: bool = True) -> dict:
    nc = TQ_DIR / f"{day}.nc"
    pdir = FLIGHTS_DIR / day
    fatto = OUT_DIR / f"{day}.parquet"
    if riprendi and fatto.exists() and fatto.stat().st_size > 1000:
        return {"day": day, "stato": "gia' fatto"}
    if not nc.exists():
        return {"day": day, "stato": "manca ERA5"}
    if not (pdir / "points.parquet").exists():
        return {"day": day, "stato": "mancano traiettorie"}

    t0 = time.perf_counter()
    ds = xr.open_dataset(nc).load()
    fl = pq.read_table(pdir / "flights.parquet",
                       columns=["flight_id", "typecode", "origin_icao",
                                "dest_icao"]).to_pandas()
    pts = pq.read_table(pdir / "points.parquet",
                        columns=["flight_id", "t", "lat", "lon", "alt_ft",
                                 "ias_kt"]).to_pandas()
    pts = pts.sort_values(["flight_id", "t"], kind="stable").reset_index(drop=True)
    pts["tt"] = pd.to_datetime(pts["t"], unit="s")

    motori = {r["typecode"]: r["engine_uid"] for r in
              csv.DictReader(l for l in open(ENGINES) if not l.startswith("#"))}
    tipo_di = dict(zip(fl.flight_id, fl.typecode))

    fid = pts["flight_id"].to_numpy()
    lat = pts["lat"].to_numpy(float)
    lon = pts["lon"].to_numpy(float)
    alt = pts["alt_ft"].to_numpy(float)
    ts = pts["t"].to_numpy(float).astype("datetime64[s]").astype(np.int64)
    n = len(pts)

    # --- peso sulla distanza: meta' del segmento prima + meta' dopo ---------
    seg = np.zeros(n)
    same = fid[1:] == fid[:-1]
    d = np.where(same, haversine_km(lat[:-1], lon[:-1], lat[1:], lon[1:]), 0.0)
    seg[:-1] += d / 2.0
    seg[1:] += d / 2.0

    from pycontrails.physics.units import ft_to_pl
    p_hpa = ft_to_pl(alt)
    lv = ds.pressure_level.values.astype(float)
    in_band = (p_hpa <= lv.max()) & (p_hpa >= lv.min())
    pt4 = np.column_stack([ts, p_hpa, lat, lon])

    # --- temperatura: ERA5 in banda, atmosfera standard sotto --------------
    t_rif = None
    risultati = {}
    for etichetta, cls_name, kwargs in RAMI:
        ta, qa = campi(ds, cls_name, kwargs)
        if t_rif is None:
            t_rif = ta.values
            it = interpolatore(ta)
            t_band = it(pt4)
            t_amb = np.where(in_band & np.isfinite(t_band), t_band,
                             isa_temperature(alt))
            cas = np.nan_to_num(pts["ias_kt"].to_numpy(float), nan=250.0) * 0.514444
            tas = cas_to_tas(cas, p_hpa * 100.0, t_amb)
            eta = eta_poll_schumann(pts, tipo_di, t_amb, tas)
        elif not np.allclose(t_rif, ta.values, equal_nan=True):
            raise SystemExit(f"{day}/{etichetta}: temperatura cambiata "
                             "-> assi disallineati")
        qv = interpolatore(qa)(pt4)
        tv = t_amb

        # --- Schmidt-Appleman e persistenza --------------------------------
        # ⚠️ La catena e' quella del modello ufficiale pycontrails.models.sac,
        # copiata nell'ordine esatto: G -> T_sat_liquid(G) -> rh_critical_sac.
        # Passare la temperatura AMBIENTE dove va T_sat_liquid non da' errore e
        # produce formazione identicamente nulla.
        from pycontrails.models.sac import (slope_mixing_line, T_sat_liquid,
                                            rh_critical_sac, sac)
        from pycontrails.physics import thermo
        p_pa = p_hpa * 100.0
        g = slope_mixing_line(qv, p_pa, eta, EI_H2O, Q_FUEL)
        rh_liq = thermo.rh(qv, tv, p_pa)
        rh_crit = rh_critical_sac(tv, T_sat_liquid(g), g)
        forma = (in_band & np.isfinite(qv) & np.isfinite(eta)
                 & np.asarray(sac(rh_liq, rh_crit), dtype=bool))
        rhi = np.asarray(thermo.rhi(qv, tv, p_pa), dtype=float) * 100.0
        # invariante: la nostra catena Murphy&Koop deve concordare con thermo.rhi
        mine = rh_from(tv, qv, p_pa, esat_ice_pa) * 100.0
        m = np.isfinite(rhi) & np.isfinite(mine)
        if m.any() and np.nanmax(np.abs(rhi[m] - mine[m])) > 1.0:
            raise SystemExit(f"{day}: RHi diverge da thermo.rhi di "
                             f"{np.nanmax(np.abs(rhi[m]-mine[m])):.2f} punti")
        persiste = forma & (rhi >= 100.0)
        risultati[etichetta] = (forma, persiste)

    # --- aggregazione per volo --------------------------------------------
    out = pd.DataFrame({"flight_id": fid, "seg_km": seg})
    agg = out.groupby("flight_id")["seg_km"].sum().rename("dist_km_tot")
    tab = agg.to_frame()
    tab["dist_km_banda"] = (out.assign(w=seg * in_band)
                            .groupby("flight_id")["w"].sum())
    for etichetta, (forma, persiste) in risultati.items():
        tab[f"dist_km_forma_{etichetta}"] = (out.assign(w=seg * forma)
                                             .groupby("flight_id")["w"].sum())
        tab[f"dist_km_persiste_{etichetta}"] = (out.assign(w=seg * persiste)
                                                .groupby("flight_id")["w"].sum())
    tab = tab.reset_index().merge(fl, on="flight_id", how="left")
    tab["engine_uid"] = tab["typecode"].map(motori)
    tab["day"] = day

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(tab, preserve_index=False),
                   OUT_DIR / f"{day}.parquet", compression="zstd")

    dt = time.perf_counter() - t0
    tot = tab["dist_km_tot"].sum()
    r = {"day": day, "stato": "ok", "voli": len(tab), "secondi": round(dt, 1),
         "km": round(tot / 1e6, 2), "eta_medio": round(float(np.nanmean(eta)), 4)}
    for etichetta in risultati:
        r[f"persiste_{etichetta}_%"] = round(
            tab[f"dist_km_persiste_{etichetta}"].sum() / tot * 100, 2)
        r[f"forma_{etichetta}_%"] = round(
            tab[f"dist_km_forma_{etichetta}"].sum() / tot * 100, 2)
    return r


def main():
    giorni = sys.argv[1:] or sorted(p.stem for p in TQ_DIR.glob("*.nc"))
    t0 = time.time()
    for i, g in enumerate(giorni, 1):
        r = un_giorno(g)
        el = (time.time() - t0) / 60
        print(f"[{i}/{len(giorni)}] "
              + " · ".join(f"{k}={v}" for k, v in r.items())
              + f" · trascorsi {el:.1f} min", flush=True)


if __name__ == "__main__":
    sys.exit(main())
