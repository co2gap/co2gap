"""Quota di carburante bruciata A TERRA, per volo.

Prodotto: data/ground_share_ecac/<giorno>.parquet, letto da site_build.py.
Senza, il gap conterrebbe il rullaggio prezzato da FuelFlow.enroute a
~7.750 kg/h contro i ~800 reali: e' il difetto corretto il 2026-08-28.

  PYTHONPATH=pipeline:ingest lab-venv/bin/python lab/ground_share.py \\
      --root $PWD --src data/flights_ecac --out data/ground_share_ecac \\
      --days-from data/decomposition_ecac

⚠️ Uno dei due NON e' facoltativo in pratica. Senza, il perimetro lo detta
data/flights_ecac, che accumula OGNI notte e oggi ha quattro giorni in piu'
della release (fino al 24/07 contro il 20/07 congelato).

⚠️ E i due NON sono equivalenti, per quanto si somiglino:
  --days-from  dichiara IL perimetro. Verifica che ogni giorno chiesto esista
               negli input, e se in uscita trova giorni fuori perimetro ESCE.
  --days       seleziona una FETTA, per lavorare a pezzi. Sui giorni fuori
               dall'intervallo avvisa e prosegue: farlo fallire renderebbe
               impossibile ogni giro parziale, che e' il suo scopo.
Per riprodurre la release si usa --days-from, ed e' quello nel README. Il sito non se ne
accorgerebbe -- parte dai giorni della decomposizione e ignora le righe di terra
in eccesso nel merge -- ma questa cartella e' descritta come autorevole, e una
cartella autorevole che contiene giorni non pubblicati e' un invito a sbagliare
al prossimo che la legge. Derivare i giorni dalla decomposizione congelata e'
meglio di scrivere le date a mano: non invecchia alla prossima release.

Non e' SOLA LETTURA: non tocca gli input, ma crea la cartella di uscita e ci
scrive un parquet per giorno.
"""
import os, sys, argparse, time
from pathlib import Path
import numpy as np, pandas as pd

ap = argparse.ArgumentParser()
ap.add_argument("--root", default=os.environ.get("ADSB_ROOT", "/mnt/wd_elements/adsb-co2"))
ap.add_argument("--src", default=None, help="cartella flights_ecac")
ap.add_argument("--out", required=True)
ap.add_argument("--days", default=None, help="es. 2026-01-01:2026-07-20")
ap.add_argument("--days-from", default=None,
                help="cartella da cui prendere l'ELENCO esatto dei giorni "
                     "(es. data/decomposition_ecac): il perimetro segue la "
                     "release invece della cartella che accumula")
ap.add_argument("--limit-days", type=int, default=None)
ap.add_argument("--alt-ft", type=float, default=1000.0)
ap.add_argument("--tas-kt", type=float, default=70.0)
a = ap.parse_args()

sys.path[:0] = [f"{a.root}/pipeline", f"{a.root}/ingest", a.root]
from trajectories import Point, Flight                      # noqa
from emissions import openap_model, estimate_fuel, _steps_from_flight  # noqa

SRC = Path(a.src or f"{a.root}/data/flights_ecac")
OUT = Path(a.out); OUT.mkdir(parents=True, exist_ok=True)

def _completo(path: Path) -> bool:
    """Esiste E si apre E ha righe. `exists()` da solo accetta un troncato."""
    if not path.exists():
        return False
    try:
        import pyarrow.parquet as _pq
        return _pq.read_metadata(path).num_rows > 0
    except Exception:
        return False


days = sorted(p.name for p in SRC.iterdir() if p.is_dir())
# Il perimetro, da qualunque delle due opzioni arrivi. Erano due rami separati e
# il controllo sull'uscita stava solo dentro --days-from, mentre la docstring le
# presenta come alternative: chi usava --days a mano aveva una protezione in
# meno di quella che il file gli prometteva. Ora e' un ramo solo.
voluti = None
if a.days_from:
    src = Path(a.days_from)
    voluti = {p.stem for p in src.glob("*.parquet")} or {p.name for p in src.iterdir() if p.is_dir()}
    if not voluti:
        raise SystemExit(f"--days-from: nessun giorno in {src}")
    mancanti = sorted(voluti - set(days))
    if mancanti:
        raise SystemExit(f"--days-from: {len(mancanti)} giorni chiesti e assenti "
                         f"in {SRC}, il primo e' {mancanti[0]}")
if a.days:
    lo, hi = a.days.split(":")
    entro = {d for d in days if lo <= d <= hi}
    if voluti is None:
        # --days da solo NON dichiara un perimetro: e' un selettore di fetta,
        # e si usa apposta per lavorare a pezzi. Trattarlo come --days-from
        # farebbe fallire ogni giro parziale legittimo. Quindi qui si AVVISA e
        # si prosegue, mentre --days-from esce. Le due opzioni non sono
        # equivalenti e la docstring lo dice.
        fuori = sorted(q.stem for q in OUT.glob("*.parquet") if q.stem not in entro)
        if fuori:
            print(f"  ⓘ {len(fuori)} giorni gia' in {OUT.name} stanno fuori da "
                  f"{lo}:{hi} (il primo e' {fuori[0]}). Con --days e' normale, "
                  "e' una fetta; con --days-from sarebbe un errore.", flush=True)
        days = [d for d in days if d in entro]
    else:
        voluti &= entro

if voluti is not None:
    days = [d for d in days if d in voluti]
    # Limitare cio' che si ELABORA non basta: se un giro precedente aveva un
    # perimetro piu' largo, i suoi parquet restano qui e la cartella continua a
    # contenere giorni che la release non ha, mentre il comando stampa "fatto.".
    # Non li cancello -- buttare dati non e' compito di uno script di calcolo --
    # ma non lascio nemmeno che passino inosservati.
    fuori = sorted(q.stem for q in OUT.glob("*.parquet") if q.stem not in voluti)
    if fuori:
        raise SystemExit(
            f"{OUT} contiene {len(fuori)} giorni fuori dal perimetro chiesto "
            f"({fuori[0]}{'...' if len(fuori) > 1 else ''}). Il sito li "
            "ignorerebbe nel merge, ma questa cartella e' descritta come "
            "autorevole. Spostarli o cancellarli a mano, poi rilanciare.")
if a.limit_days: days = days[:a.limit_days]
print(f"  {len(days)} giorni da elaborare · soglia terra: alt<{a.alt_ft:.0f} ft e tas<{a.tas_kt:.0f} kt", flush=True)

PCOLS = ["flight_id","t","lat","lon","alt_ft","gs_kt","ias_kt","vs_fpm"]
FCOLS = ["flight_id","typecode","co2_kg_v0","fuel_kg_v0","load_factor",
         "reserve_kg","tas_mode","origin_icao","dest_icao","gc_km"]

for day in days:
    dst = OUT / f"{day}.parquet"
    if _completo(dst):
        print(f"  {day}  gia' fatto, salto", flush=True); continue
    t0 = time.time()
    try:
        fl_df = pd.read_parquet(SRC/day/"flights.parquet", columns=FCOLS)
        pt_df = pd.read_parquet(SRC/day/"points.parquet", columns=PCOLS)
    except Exception as e:
        print(f"  {day}  ⚠️ illeggibile: {e}", flush=True); continue

    pt_df = pt_df.sort_values(["flight_id","t"])
    groups = dict(tuple(pt_df.groupby("flight_id", sort=False)))
    rows = []
    for r in fl_df.itertuples(index=False):
        if openap_model(r.typecode) is None:      # stesso filtro della produzione
            continue
        g = groups.get(r.flight_id)
        if g is None or len(g) < 11:
            continue
        pts = [Point(t=float(t), lat=float(la), lon=float(lo),
                     alt=(None if np.isnan(al) else float(al)),
                     gs=(None if np.isnan(gs) else float(gs)),
                     ias=(None if np.isnan(ia) else float(ia)),
                     vs_rep=(None if np.isnan(vs) else float(vs)))
                for t,la,lo,al,gs,ia,vs in zip(g.t, g.lat, g.lon, g.alt_ft,
                                               g.gs_kt, g.ias_kt, g.vs_fpm)]
        fl = Flight(icao="X", typecode=r.typecode, reg=None, points=pts)
        mode = r.tas_mode if isinstance(r.tas_mode, str) and r.tas_mode else "ias"
        res = estimate_fuel(fl, load_factor=float(r.load_factor),
                            reserve_kg=float(r.reserve_kg), tas_mode=mode,
                            with_steps=True)
        if not res.ok or res.burn_kg_step is None:
            continue
        st = _steps_from_flight(fl, mode)
        if st is None: continue
        dt, alt, vs, tas, d_km, valid = st
        burn = np.asarray(res.burn_kg_step, dtype=float)
        if len(burn) != len(alt): continue
        # PIU' DEFINIZIONI INSIEME: lo stadio 2 sceglie la soglia senza rifare
        # questo stadio. "suolo" e' la definizione letterale del parser
        # (trajectories.py:77 mappa la stringa "ground" a 0 ft): non ha soglie.
        masks = {
            "suolo":     (alt <= 0.0),
            "a1000t40":  (alt < 1000) & (tas <  40),
            "a1000t70":  (alt < 1000) & (tas <  70),
            "a1000t100": (alt < 1000) & (tas < 100),
            "a3000t70":  (alt < 3000) & (tas <  70),
        }
        # semitratto per frazione d'arco: i secchielli TMA di phase_split partizionano
        # cosi'. Senza queste due colonne, correggere l'84%/90% costa un'altra passata.
        arc = np.cumsum(d_km); tot_arc = arc[-1] if len(arc) and arc[-1] > 0 else 1.0
        first_half = (arc - d_km/2.0) / tot_arc < 0.5
        tb = float(burn.sum())
        vals = []
        for k, m in masks.items():
            vals += [float(burn[m].sum()), float(dt[m].sum()), float(m.sum()),
                     float(burn[m & first_half].sum()), float(burn[m & ~first_half].sum())]
        rows.append((day, r.flight_id, r.typecode, r.origin_icao, r.dest_icao,
                     float(r.gc_km), float(r.co2_kg_v0), tb, float(len(burn)),
                     float(np.nanmin(tas)), float(np.nanmin(alt))) + tuple(vals))
    MK = ["suolo","a1000t40","a1000t70","a1000t100","a3000t70"]
    cols = ["day","flight_id","typecode","origin_icao","dest_icao","gc_km",
            "co2_kg_v0_frozen","fuel_recomputed_kg","n_steps","tas_min_kt","alt_min_ft"]
    for k in MK:
        cols += [f"fuel_{k}_kg", f"t_{k}_s", f"n_{k}",
                 f"fuel_{k}_dep_kg", f"fuel_{k}_arr_kg"]
    out = pd.DataFrame(rows, columns=cols)
    for k in MK:   # quota da applicare al livello congelato nello stadio 2
        out[f"share_{k}"] = np.where(out.fuel_recomputed_kg > 0,
                                     out[f"fuel_{k}_kg"] / out.fuel_recomputed_kg, 0.0)
    # Scrittura ATOMICA: prima un temporaneo, poi replace(). Scrivere sul nome
    # definitivo significa che un'interruzione lascia un parquet troncato che al
    # rilancio passa per fatto. E' il modello gia' usato da run_phase_split.py.
    tmp = OUT / f".{day}.parquet.tmp"
    out.to_parquet(tmp, index=False)
    tmp.replace(dst)
    # 🔑 CONTROLLO DI EQUIVALENZA: il burn ricalcolato dal diradato deve stare
    # vicino al congelato (atteso ~-0,3% per il diradamento, gia' misurato).
    ratio = (out.fuel_recomputed_kg.sum()*3.16) / out.co2_kg_v0_frozen.sum()
    print(f"  {day}  voli {len(out):6,}  suolo {out.fuel_suolo_kg.sum()/out.fuel_recomputed_kg.sum()*100:5.2f}%"
          f"  a1000t70 {out.fuel_a1000t70_kg.sum()/out.fuel_recomputed_kg.sum()*100:5.2f}%"
          f"  ricalc/congelato {ratio:6.4f}  {time.time()-t0:5.1f}s", flush=True)
print("  fatto.", flush=True)
