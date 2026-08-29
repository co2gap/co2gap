"""Le soglie del cancello di qualita', in un posto solo.

Esistevano in due file e la metodologia le raccontava in un terzo, a parole. Il
29/08/2026 la frase pubblicata diceva che si scartano i voli la cui distanza
volata e' *inferiore alla rotta diretta*: il codice ne ammette fino al 10% in
meno, e il paragrafo subito sotto sulla stessa pagina lo diceva giusto. Una
pagina che si contraddice da sola su cosa entra nell'analisi e' peggio di una
vaga, quindi ora la frase legge queste costanti invece di descriverle.

Nessuna dipendenza, di proposito: lo importano sia lab/analysis.py (che tira
dentro openap) sia lab/site_build.py (che non deve).
"""

# quota della DURATA che deve stare fuori dalle lacune lunghe
COV_MIN = 0.85
# una lacuna e' "lunga" oltre questa soglia — pipeline/flightproc.py:33
GAP_THRESHOLD_S = 120.0
# la distanza ricostruita non puo' scendere sotto questa frazione dell'ortodromia:
# le lacune fanno CONTARE MENO strada, quindi e' una guardia sulla troncatura,
# non un giudizio sul volo — pipeline/flightproc.py:70
FLOWN_MIN_FRAC = 0.9
# tratta minima, in km
GC_MIN_KM = 150
