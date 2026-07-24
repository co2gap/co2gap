# adsb-co2 — Osservatorio CO₂ / inefficienza aviazione (EU-Sud)

Calcola le emissioni **CO₂ reali** dei voli a partire dalle traiettorie ADS-B
(modello fisico **OpenAP**, TU Delft) e in particolare l'**excess CO₂**: la
differenza fra le emissioni della traiettoria effettivamente volata e quelle
dell'ottimo *great-circle*. L'excess quantifica l'inefficienza da
routing / livelli di volo / holding / congestione.

**Tutto è aggregato per aeroporto / rotta. Mai per individuo o singolo velivolo
identificabile** (vincolo GDPR: nessun tracciamento di persone).

Stato: **fase 0** — proof-of-pipeline. Vedi `reports/fase0.md`.

## Fonte dati e licenza

I dati storici delle traiettorie provengono da **adsb.lol**, dump giornalieri
pubblici:

> Traiettorie: **© adsb.lol contributors**, distribuite sotto
> **Open Database License (ODbL) v1.0**.
> Fonte: https://www.adsb.lol/docs/open-data/historical/

Questo progetto è un *Produced Work* ai sensi di ODbL. In caso di
pubblicazione dei risultati va mantenuta l'attribuzione ad adsb.lol e indicata
la licenza ODbL del database. Il file `LICENSE-ODbL.txt` è incluso in ogni
dump.

**Non si usano dati OpenSky** in questo progetto (termini d'uso diversi;
scelta deliberata per tenere aperta la strada commerciale su fonte ODbL).

Il modello di consumo/emissioni usa **OpenAP** (https://openap.dev), open source.

## Architettura

```
ingest/     source.py      astrazione sulla fonte dati (oggi adsb.lol day dump)
pipeline/   trajectories.py segmentazione traiettorie per volo + pulizia
            emissions.py    fuel burn + CO2 via OpenAP (stima massa iterativa)
            excess.py       profilo nominale great-circle + excess %
            run_fase0.py    orchestratore fase 0
data/       raw/ (dump, git-ignored)  interim/ (output)
reports/    fase0.md
```

Il layer `ingest/source.py` è l'unico punto che conosce la fonte: per passare a
un'altra sorgente (es. Wingbits) basta implementare `iter_traces()` con la
stessa forma di output, il resto della pipeline non cambia.

## Esecuzione (sul Pi, dentro il venv)

```
nice -n15 ionice -c3 venv/bin/python pipeline/run_fase0.py
```

Processing a chunk / streaming del tar splittato: RAM contenuta (<300 MB
misurati), pensato per girare come cron notturno.
