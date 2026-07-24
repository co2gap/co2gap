# adsb-co2 — Osservatorio CO₂ / inefficienza aviazione (EU-Sud)

Calcola le emissioni **CO₂ reali** dei voli a partire dalle traiettorie ADS-B
(modello fisico **OpenAP**, TU Delft) e in particolare l'**excess CO₂**: la
differenza fra le emissioni della traiettoria effettivamente volata e quelle
dell'ottimo *great-circle* **wind-aware**. L'excess quantifica l'inefficienza da
routing / livelli di volo / holding / congestione, al netto del vento.

**Tutto è aggregato per aeroporto / rotta. Mai per individuo o singolo velivolo
identificabile** (vincolo GDPR: nessun tracciamento di persone). Il dataset
durevole non contiene identificatori di aeromobile.

Stato: **fase 1** — excess wind-aware + calibrazione per-tipo + accumulo
giornaliero. Report fase 0: `reports/fase0.md`.

## Fonte dati e licenza

Traiettorie storiche da **adsb.lol**, dump giornalieri pubblici:

> Traiettorie: **© adsb.lol contributors**, **Open Database License (ODbL) v1.0**.
> Fonte: https://www.adsb.lol/docs/open-data/historical/

Questo progetto è un *Produced Work* ai sensi di ODbL: in caso di pubblicazione
va mantenuta l'attribuzione ad adsb.lol e indicata la licenza ODbL del database.

Aeroporti: **OurAirports** (dominio pubblico, CC0), pre-filtrati al box in
`data/airports.csv`. Vento: **ERA5 / ERA5T** (Copernicus CDS), reanalisi ~0,25°.
Modello emissioni: **OpenAP** (https://openap.dev), open source.

**Non si usano dati OpenSky** (termini diversi; scelta per tenere aperta la via
commerciale su fonte ODbL).

## Architettura a due macchine

```
PI (sensecapAI, .90)  =  PRODUZIONE          MAC (MacBook Air M1)  =  LABORATORIO
  cron 02:00 accumulo                          excess wind-aware (ERA5)
  parse dump globale -> box EU-Sud             calibrazione per-tipo
  parquet per-volo su WD (per sempre)          report statici
  NIENTE cdsapi/xarray                         git clone del repo del Pi
        |  git push (updateInstead)                    ^  rsync SOLO parquet
        +----------------- repo condiviso --------------+  (mai i dump grezzi)
```

- Il **Pi** fa il lavoro costoso e irripetibile (parse del dump globale ~4,3 GB)
  e scrive il **dataset durevole** (traiettoria + fuel v0 + qualità). L'excess
  **non** si calcola qui: si ricalcola sul Mac quando il baseline migliora,
  **senza rifare il parsing**.
- Il **Mac** (~5–10× più veloce) fa il lavoro iterativo (vento, calibrazione).
  Riceve solo i parquet processati (piccoli) via `rsync`; **mai** i dump grezzi.
- Codice+config validati sul Mac tornano in produzione con un `git pull` sul Pi.

## Pipeline (moduli)

```
ingest/     source.py        astrazione fonte dati (oggi adsb.lol day dump)
pipeline/   trajectories.py  segmentazione traiettorie per volo + pulizia
            flightproc.py    traiettoria assottigliata + metriche di qualità
            emissions.py     fuel/CO2 via OpenAP (integratore VETTORIALE)
            excess.py        baseline nominale wind-free (fase 0, riferimento)
            excess_wind.py   baseline nominale WIND-AWARE (fase 1)  [Mac]
            airports.py      risoluzione aeroporto più vicino
            store.py         writer parquet (row-group incrementali, GDPR-clean)
            run_daily.py     orchestratore di produzione (multiprocessing)
wind/       era5.py          download ERA5 (CDS) + campo di vento 4-D  [Mac]
lab/        gate.py          gate del vento (crollo del dir_spread)
            calibrate.py     derivazione fattori di calibrazione per-tipo
scripts/    dl_day.sh        download parametrico di un giorno
            daily_cron.sh    job notturno (lock/log/retry/rotazione)
            backfill.sh      backfill di giorni specifici
data/       raw/ (dump, ruotati)  flights/<giorno>/{points,flights}.parquet
            era5/ (vento, Mac)    airports.csv  calibration.json
```

Il layer `ingest/source.py` è l'unico punto che conosce la fonte: per passare a
un'altra sorgente (es. Wingbits) basta implementare `iter_traces()` con la
stessa forma, il resto non cambia.

## Metodo — CO₂ reale ed excess

- **CO₂ reale**: `FuelFlow.enroute(mass, TAS, alt, vs)` integrato sulla
  traiettoria. TAS da IAS (compressibile, **indipendente dal vento**), fallback
  a GS. Massa stimata iterativamente (OEW + payload@load-factor + riserva +
  trip-fuel, chiuso a punto fisso). CO₂ = fuel × 3,16. Integratore
  **vettoriale** (una chiamata array per iterazione, ~100× vs loop per-step;
  accordo <0,01% con il riferimento scalare).
- **Excess wind-aware**: il baseline è un volo nominale sul great-circle O→D
  alla quota/Mach ottimi del tipo, **con il tempo di crociera calcolato a ground
  speed = TAS + vento along-track** (ERA5, al livello di crociera, all'ora del
  volo). Il fuel reale riflette già il vento *attraverso la durata effettiva*;
  dando al nominale lo stesso termine, il vento **si cancella** e resta la sola
  inefficienza (percorso extra, quote non ottime, holding).
  `excess% = (CO₂_reale − CO₂_ideale_windaware) / CO₂_ideale_windaware × 100`.

## Qualità dei dati (scarti)

Un volo entra nell'analisi solo se: O/D noti (aeroporto entro 8 km), `flown ≥
0,9·GC` (buchi di copertura non hanno tagliato distanza), `coverage_frac ≥ 0,85`,
`GC ≥ 150 km`. Le metriche (`max_gap_s`, `hole_time_s`, `coverage_frac`) sono nel
parquet per volo.

## Calibrazione per-tipo

Il modello OpenAP ha bias sistematici per alcuni tipi (neo Airbus bassi, Embraer
E-Jet alti). Si correggono con un **fattore scalare per tipo** ancorato al fuel
flow di crociera pubblicato, applicato **a valle** (il `co2_kg_v0` nel parquet
resta grezzo e model-indipendente). Fattori in `data/calibration.json`, derivati
da `lab/calibrate.py`. _(Tabella e ancoraggi: vedi report fase 1.)_

## Esecuzione

Produzione (Pi, dentro il venv):
```
WORKERS=4 nice -n15 ionice -c3 venv/bin/python pipeline/run_daily.py --day 2026.07.19
```
Cron notturno: `scripts/daily_cron.sh` (installato in crontab alle 02:00).

Laboratorio (Mac):
```
./sync_parquet.sh                        # rsync parquet dal Pi
lab-venv/bin/python lab/gate.py          # gate del vento
lab-venv/bin/python lab/calibrate.py     # fattori di calibrazione
```
