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

## Scomposizione laterale / verticale dell'excess (fase 2a)

L'excess misurato contro un ottimo great-circle mette insieme due cose molto
diverse: aver volato **più lontano** del necessario, e aver volato **male in
verticale** il percorso assegnato (quota di crociera non ottima, discese
anticipate, attese). Solo la prima è confrontabile con le metriche pubblicate.
`pipeline/decompose.py` introduce una **baseline intermedia**:

```
ideal_gc = profilo ottimo sulla distanza GREAT-CIRCLE
hybrid   = profilo ottimo sulla GROUND TRACK REALE
real     = volo effettivo

excess_laterale  = (hybrid   − ideal_gc) / ideal_gc × 100
excess_verticale = (real     − hybrid  ) / ideal_gc × 100
excess_totale    = (real     − ideal_gc) / ideal_gc × 100  = laterale + verticale
```

Le due componenti sono **additive per costruzione** (stesso denominatore).
Due scelte deliberate: la quota di crociera dell'`hybrid` è quella ottima per
la distanza *great-circle* (così la componente laterale isola una sola cosa,
la distanza in più), mentre il **vento** dell'`hybrid` è campionato lungo la
**traccia reale** (una deviazione fatta per prendere vento in coda deve
risultare "laterale ma economica").

Oltre al fuel si produce la metrica puramente geometrica **omogenea al KEA di
EUROCONTROL**: `dist_ratio_enroute`, cioè il rapporto distanza volata /
great-circle calcolato **escludendo le aree terminali** (punti entro 40 NM dai
due aeroporti, esattamente la definizione en-route del KEA). Il rapporto
gate-to-gate è riportato accanto ma **non** è confrontabile col KEA, perché
include SID/STAR e vettoramento che il KEA esclude per definizione.

## Calibrazione per-tipo e ancoraggio delle fonti

Il modello OpenAP ha bias sistematici per alcuni tipi (neo Airbus bassi,
alcuni Embraer alti). Si correggono con un **fattore scalare per tipo**
ancorato al fuel flow di crociera pubblicato, applicato **a valle** (il
`co2_kg_v0` nel parquet resta grezzo e model-indipendente). Fattori in
`data/calibration.json`, derivati da `lab/calibrate.py`.

**Fase 2a — i riferimenti sono ora citabili.** Le cifre industriali indicative
usate in fase 1 sono state sostituite con valori derivati dalla
**ICAO Carbon Emissions Calculator (ICEC) Methodology v13.1 (agosto 2024),
Appendice C "ICAO Fuel Consumption Table"**
(<https://icec.icao.int/Documents/Methodology%20ICAO%20Carbon%20Emissions%20Calculator_v13_Final.pdf>).
La tabella ICAO dà il **carburante totale di tratta** (kg) a distanze fisse,
non un consumo orario di crociera: `lab/anchor_refs.py` ricava il kg/h
prendendo la **pendenza** della curva sul segmento **1500–2000 NM** (regime
dominato dalla crociera) e moltiplicandola per la TAS di crociera del tipo.
La derivazione è esplicita e riproducibile, ed è documentata come *conversione*
— non come misura indipendente. Tabella completa in `data/icao_fuel_table.json`
(dati grezzi ICAO, versionati) e `data/anchored_cruise_ff.json` (derivati).
I business jet (C550, GLF6) non sono coperti dall'ICEC e restano non ancorati.

## Esecuzione

Produzione (Pi, dentro il venv):
```
WORKERS=4 nice -n15 ionice -c3 venv/bin/python pipeline/run_daily.py --day 2026.07.19
```
Cron notturno: `scripts/daily_cron.sh` (installato in crontab alle 02:00).

Laboratorio (Mac) — **catena completa in un comando**, idempotente e
riprendibile (è lo stesso comando che rifà la fase 2b sull'anno intero):
```
scripts/run_phase2.sh
```
Singoli passi, se servono:
```
./sync_parquet.sh                              # rsync parquet dal Pi
lab-venv/bin/python scripts/era5_backfill.py   # ERA5 per tutti i giorni
lab-venv/bin/python lab/gate.py                # gate del vento
lab-venv/bin/python lab/anchor_refs.py         # riferimenti ICAO -> kg/h
lab-venv/bin/python lab/calibrate.py           # fattori di calibrazione
lab-venv/bin/python lab/run_decompose.py       # scomposizione lat/vert
lab-venv/bin/python lab/decompose_report.py    # tabelle del report
```
Nessuno di questi comandi pubblica niente: il deploy resta una decisione
esplicita e separata.
