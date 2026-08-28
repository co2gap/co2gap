"""Emissioni di rullaggio per aeroporto — stima composta, non una misura.

  carburante = tempo(EUROCONTROL) x portata al minimo(ICAO) x movimenti(nostri)

Tre fonti, e vanno nominate tutte e tre:
  tempo     EUROCONTROL, file *Taxi times*, media per aeroporto, taxi-out e
            taxi-in separati, STAGIONE APPAIATA alla data di ogni volo.
  portata   databank motori ICAO via openap.prop (`ff_idl`, 7% di spinta del
            ciclo LTO). Motore di DEFAULT per typecode: un A319 con CFM invece
            di V2500 e' prezzato col V2500. Incertezza per tipo, dichiararla.
  flotta    nostra, dai dati ADS-B, per aeroporto e per stagione.

⚠️ PERIMETRO. Il rullaggio si restringe agli STESSI voli che producono la CO2
in volo (quelli presenti nella decomposizione): senza, il numeratore ha il 13%
di voli in piu' del denominatore e il rapporto confronta due popolazioni
diverse. E' il rilievo della verifica del 2026-08-28.

⚠️ La nostra misura ADS-B del tempo di rullaggio NON si usa: tarata contro
EUROCONTROL su 129 aeroporti da un rapporto 0,29-0,73 con correlazione 0,50,
quindi la copertura al suolo la rende non correggibile con un fattore.
Vedi lab/taxi_time.py, conservato come prova di quella conclusione.
"""
import argparse, glob
from pathlib import Path
import numpy as np, pandas as pd
from openap import prop

ap = argparse.ArgumentParser()
ap.add_argument("--taxi", required=True, help="output di lab/taxi_time.py")
ap.add_argument("--decomp", required=True, help="decomposizione: definisce il PERIMETRO")
ap.add_argument("--ec-dir", required=True, help="cartella con gli xlsx EUROCONTROL")
ap.add_argument("--stagione-da", default="2026-03-29", help="inizio stagione estiva IATA")
ap.add_argument("--min-mov", type=int, default=2000)
ap.add_argument("--out", default=None)
a = ap.parse_args()

def ec(f, col):
    d = pd.read_excel(Path(a.ec_dir) / f).dropna(subset=["ICAO"])
    return d.set_index("ICAO")["Mean TX" + ("O" if "out" in f else "I") + " (mins)"].rename(col)

EC = pd.concat([ec("w2526-out.xlsx", "txo_w"), ec("w2526-in.xlsx", "txi_w"),
                ec("s25-out.xlsx", "txo_s"),  ec("s25-in.xlsx", "txi_s")], axis=1)

t = pd.concat([pd.read_parquet(f) for f in sorted(glob.glob(f"{a.taxi}/*.parquet"))])
d = pd.concat([pd.read_parquet(f, columns=["day", "flight_id"])
               for f in sorted(glob.glob(f"{a.decomp}/*.parquet"))])
n0 = len(t)
t = t.merge(d, on=["day", "flight_id"], how="inner")      # PERIMETRO
print(f"  perimetro: {n0:,} -> {len(t):,} voli (gli stessi della CO2 in volo)")

t["inverno"] = pd.to_datetime(t.day) < a.stagione_da
print(f"  stagioni: {t.inverno.mean()*100:.0f}% inverno · {(1-t.inverno.mean())*100:.0f}% estate")

ffi = {}
for tc in t.typecode.dropna().unique():
    try:
        x = prop.aircraft(str(tc).lower()); e = prop.engine(x["engine"]["default"])
        ffi[tc] = float(x["engine"].get("number", 2)) * float(e["ff_idl"])
    except Exception:
        pass
t["ff"] = t.typecode.map(ffi)
print(f"  portata al minimo nota per {len(ffi)} tipi su {t.typecode.nunique()}")

out = []
for key, cw, cs, lab in (("origin_icao", "txo_w", "txo_s", "dep"),
                         ("dest_icao", "txi_w", "txi_s", "arr")):
    g = t.groupby([key, "inverno"]).agg(n=("flight_id", "size"), ff=("ff", "mean")).reset_index()
    g = g.rename(columns={key: "ICAO"}).merge(EC.reset_index(), on="ICAO", how="inner")
    g["tempo"] = np.where(g.inverno, g[cw], g[cs])
    g = g.dropna(subset=["tempo", "ff"])
    g["co2"] = g.tempo * 60 * g.ff * 3.16 * g.n
    out.append(g.groupby("ICAO").apply(lambda x: pd.Series({
        f"n_{lab}": x.n.sum(), f"co2_{lab}": x.co2.sum(),
        f"t_{lab}": np.average(x.tempo, weights=x.n)}), include_groups=False))

k = out[0].join(out[1], how="outer").fillna(0)
k["mov"] = k.n_dep + k.n_arr
k["co2_mov"] = (k.co2_dep + k.co2_arr) / k.mov
k = k[k.mov >= a.min_mov]
tot = (k.co2_dep + k.co2_arr).sum()
print(f"\n  aeroporti {len(k)} · movimenti {k.mov.sum():,.0f}")
print(f"  CO2 di rullaggio {tot/1e9:.2f} Mt")
print(f"  per movimento: mediana FRA AEROPORTI {k.co2_mov.median():.0f} kg · "
      f"media pesata sui movimenti {(k.co2_dep+k.co2_arr).sum()/k.mov.sum():.0f} kg")
print(f"\n  {'':6}{'movim.':>10}{'taxi-out':>10}{'kg CO2/mov':>12}")
for i, r in k.nlargest(8, "co2_mov").iterrows():
    print(f"  {i:6}{r.mov:10,.0f}{r.t_dep:9.1f}m{r.co2_mov:11.0f}")
if a.out:
    k.to_parquet(a.out); print(f"\n  -> {a.out}")
