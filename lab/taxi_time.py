"""Tempo di rullaggio per volo, dalla campata a terra.

NON usato per le cifre pubblicate: tarato contro EUROCONTROL su 129
aeroporti da un rapporto 0,29-0,73 con correlazione 0,50, quindi la
copertura ADS-B al suolo lo rende non correggibile con un fattore.
Conservato perche' e' la prova di quella conclusione.
"""
import argparse, sys
from pathlib import Path
import numpy as np, pandas as pd

ap = argparse.ArgumentParser()
ap.add_argument("--src", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--days", default=None)
ap.add_argument("--alt-ft", type=float, default=3000.0)
ap.add_argument("--tas-kt", type=float, default=70.0)
ap.add_argument("--min-punti", type=int, default=3)
a = ap.parse_args()

SRC, OUT = Path(a.src), Path(a.out); OUT.mkdir(parents=True, exist_ok=True)
days = sorted(p.name for p in SRC.iterdir() if p.is_dir())
if a.days:
    lo, hi = a.days.split(":"); days = [d for d in days if lo <= d <= hi]
print(f"  {len(days)} giorni · terra: alt<{a.alt_ft:.0f} ft e gs<{a.tas_kt:.0f} kt", flush=True)

for day in days:
    dst = OUT / f"{day}.parquet"
    if dst.exists():
        continue
    try:
        pt = pd.read_parquet(SRC/day/"points.parquet",
                             columns=["flight_id", "t", "alt_ft", "gs_kt"])
        fl = pd.read_parquet(SRC/day/"flights.parquet",
                             columns=["flight_id", "typecode", "origin_icao", "dest_icao"])
    except Exception as e:
        print(f"  {day}  illeggibile: {e}", flush=True); continue

    terra = (pt.alt_ft < a.alt_ft) & (pt.gs_kt < a.tas_kt)
    aria = pt[~terra].groupby("flight_id").t
    r = pd.DataFrame({"t_volo_min": aria.min(), "t_volo_max": aria.max()})
    # 🔑 I due capi vanno misurati SEPARATAMENTE. Con un solo minimo sull'intero
    # volo, un aereo senza punti a terra alla partenza ma con punti all'arrivo
    # produce una campata di partenza pari a ZERO invece che "non misurata":
    # Dusseldorf risultava con zero rullaggio su 45.642 movimenti.
    pt = pt.join(r, on="flight_id")
    dep = pt[terra & (pt.t < pt.t_volo_min)].groupby("flight_id").t
    arr = pt[terra & (pt.t > pt.t_volo_max)].groupby("flight_id").t
    r["n_dep"] = dep.size(); r["n_arr"] = arr.size()
    # TRE definizioni registrate insieme: la scelta si fa a valle e la
    # sensibilita' si dichiara, come per la soglia di suolo.
    #  campata     primo punto a terra -> decollo. Include il tempo allo stand
    #              a motori SPENTI: addebita carburante non bruciato.
    #  osservato   somma degli intervalli visti, saltando i buchi >600 s.
    #  movimento   dal primo movimento (gs>3 kt) al decollo. I motori girano
    #              quando l'aereo si muove: conservativa e fisica. E' la scelta.
    r["taxi_dep_campata_s"] = r.t_volo_min - dep.min()
    r["taxi_arr_campata_s"] = arr.max() - r.t_volo_max
    pt = pt.sort_values(["flight_id", "t"]); pt["dt"] = pt.groupby("flight_id").t.diff()
    d2 = pt[terra & (pt.t < pt.t_volo_min) & pt.dt.le(600)]
    a2 = pt[terra & (pt.t > pt.t_volo_max) & pt.dt.le(600)]
    r["taxi_dep_osservato_s"] = d2.groupby("flight_id").dt.sum()
    r["taxi_arr_osservato_s"] = a2.groupby("flight_id").dt.sum()
    mv = pt[terra & (pt.t < pt.t_volo_min) & (pt.gs_kt > 3)].groupby("flight_id").t.min()
    ms = pt[terra & (pt.t > pt.t_volo_max) & (pt.gs_kt > 3)].groupby("flight_id").t.max()
    r["taxi_dep_movimento_s"] = r.t_volo_min - mv
    r["taxi_arr_movimento_s"] = ms - r.t_volo_max
    r["taxi_dep_s"] = r.taxi_dep_movimento_s
    r["taxi_arr_s"] = r.taxi_arr_movimento_s
    # misurabilita' per capo: un capo scoperto non e' un capo con zero rullaggio
    r["mis_dep"] = r.n_dep.fillna(0) >= a.min_punti
    r["mis_arr"] = r.n_arr.fillna(0) >= a.min_punti
    for _c in ("campata", "osservato", "movimento", ""):
        _s = f"_{_c}_s" if _c else "_s"
        r.loc[~r.mis_dep, f"taxi_dep{_s}"] = np.nan
        r.loc[~r.mis_arr, f"taxi_arr{_s}"] = np.nan
    r["misurabile"] = r.mis_dep | r.mis_arr
    r["n_terra"] = r.n_dep.fillna(0) + r.n_arr.fillna(0)
    r = r.join(fl.set_index("flight_id"), how="right")
    r["day"] = day
    out = r.reset_index()[["day", "flight_id", "typecode", "origin_icao",
                           "dest_icao", "n_terra", "misurabile",
                           "mis_dep", "mis_arr", "taxi_dep_s", "taxi_arr_s",
                           "taxi_dep_campata_s", "taxi_arr_campata_s",
                           "taxi_dep_osservato_s", "taxi_arr_osservato_s",
                           "taxi_dep_movimento_s", "taxi_arr_movimento_s"]]
    out.to_parquet(dst, index=False)
    print(f"  {day}  voli {len(out):6,}  partenze misurate {out.mis_dep.fillna(False).mean()*100:3.0f}%"
          f"  arrivi {out.mis_arr.fillna(False).mean()*100:3.0f}%"
          f"  taxi-out {out.taxi_dep_s.median()/60:4.1f} min  taxi-in {out.taxi_arr_s.median()/60:4.1f}", flush=True)
print("  fatto.", flush=True)
