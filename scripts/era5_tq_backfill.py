#!/usr/bin/env python3
"""Scarica ERA5 t+q sui livelli di crociera, per la fase A delle scie.

⚠️ CARTELLA E VARIABILE D'AMBIENTE SEPARATE, DI PROPOSITO.
La catena del vento usa `ERA5_DIR` (wind/era5.py, lab/run_decompose.py,
lab/analysis.py, scripts/era5_backfill.py) e nomina i file `YYYY-MM-DD.nc`
senza box ne' variabili nel nome. Riusare quella variabile o quella cartella
farebbe leggere t/q dove ci si aspetta u/v — stessa forma di nome, nessun
errore, geografia o fisica sbagliata. Qui: `ERA5_TQ_DIR`, default
`data/era5_tq_ecac`, e un MANIFEST.json che registra la configurazione.

I giorni NON si scrivono a mano: si derivano da `data/flights_ecac`, cosi' non
si possono scaricare giorni senza traiettorie ne' dimenticarne di utili.

  contrail-venv/bin/python scripts/era5_tq_backfill.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

LEVELS = [400, 350, 300, 250, 225, 200, 175, 150]
VARS = ["temperature", "specific_humidity"]
AREA = [float(x) for x in os.environ.get("ERA5_AREA", "72,-32,27,45").split(",")]
WORKERS = int(os.environ.get("ERA5_TQ_WORKERS", "4"))

OUT_DIR = Path(os.environ.get("ERA5_TQ_DIR") or (ROOT / "data/era5_tq_ecac"))
FLIGHTS_DIR = Path(os.environ.get("ADSB_FLIGHTS_DIR") or (ROOT / "data/flights_ecac"))
LOG = ROOT / "logs/era5_tq_backfill.log"


def log(msg: str):
    line = f"{time.strftime('%F %T')} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def giorni_dai_voli() -> list[str]:
    """I giorni per cui esistono traiettorie. Mai una lista scritta a mano."""
    days = sorted(d.name for d in FLIGHTS_DIR.iterdir()
                  if d.is_dir() and (d / "points.parquet").exists())
    return days


def is_valid(path: Path) -> bool:
    """⚠️ «il file esiste» non e' un test: si guarda cosa contiene."""
    if not path.exists() or path.stat().st_size < 10_000:
        return False
    try:
        import xarray as xr
        ds = xr.open_dataset(str(path))
        ok = ("t" in ds.variables and "q" in ds.variables
              and ds.sizes.get("valid_time", 0) >= 24
              and ds.sizes.get("pressure_level", 0) == len(LEVELS))
        ds.close()
        return ok
    except Exception:
        return False


def fetch_one(day_iso: str):
    out = OUT_DIR / f"{day_iso}.nc"
    if is_valid(out):
        return day_iso, "SKIP", 0.0, out.stat().st_size
    if out.exists():
        out.unlink()
    t0 = time.time()
    tmp = out.with_suffix(".nc.part")
    try:
        import cdsapi
        y, m, d = day_iso.split("-")
        cdsapi.Client(quiet=True).retrieve("reanalysis-era5-pressure-levels", {
            "product_type": "reanalysis",
            "variable": VARS,
            "pressure_level": [str(x) for x in LEVELS],
            "year": y, "month": m, "day": d,
            "time": [f"{h:02d}:00" for h in range(24)],
            "area": AREA, "grid": [0.25, 0.25],
            "data_format": "netcdf",
        }, str(tmp))
        tmp.replace(out)
        dt = time.time() - t0
        if not is_valid(out):
            # Vicino al bordo ERA5T il CDS restituisce un giorno PARZIALE
            # invece di un errore. Si cancella: lasciarlo su disco farebbe
            # costruire i campi con un buco a chi controlla solo l'esistenza.
            out.unlink(missing_ok=True)
            return day_iso, "FAIL(incompleto: file parziale rimosso)", dt, 0
        return day_iso, "OK", dt, out.stat().st_size
    except Exception as e:
        tmp.unlink(missing_ok=True)
        return day_iso, f"FAIL({e.__class__.__name__}: {e})", time.time() - t0, 0


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    days = giorni_dai_voli()
    if not days:
        sys.exit(f"nessun giorno con traiettorie in {FLIGHTS_DIR}")

    manifest = {"variabili": VARS, "livelli": LEVELS, "area_NWSE": AREA,
                "griglia": [0.25, 0.25], "giorni": len(days),
                "primo": days[0], "ultimo": days[-1],
                "origine_giorni": str(FLIGHTS_DIR),
                "scritto": time.strftime("%F %T")}
    (OUT_DIR / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))

    log(f"==== ERA5 t/q START {days[0]} -> {days[-1]} ({len(days)} giorni, "
        f"{WORKERS} worker) -> {OUT_DIR} ====")
    ok = skip = fail = 0
    tot_b = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch_one, d): d for d in days}
        for i, f in enumerate(as_completed(futs), 1):
            day, status, dt, nb = f.result()
            tot_b += nb
            if status == "OK":
                ok += 1
            elif status == "SKIP":
                skip += 1
            else:
                fail += 1
            if status != "SKIP" or i % 25 == 0:
                el = time.time() - t0
                log(f"[{i}/{len(days)}] {day} {status} {dt:.0f}s · "
                    f"ok={ok} skip={skip} fail={fail} · "
                    f"{tot_b/1e9:.2f} GB · trascorsi {el/60:.1f} min")
    log(f"==== FINE · ok={ok} skip={skip} fail={fail} · {tot_b/1e9:.2f} GB · "
        f"{(time.time()-t0)/60:.1f} min ====")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
