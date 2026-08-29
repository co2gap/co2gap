"""Le soglie del cancello di qualita' della TRACCIA, in un posto solo.

Nome scelto per non confondersi con lab/gate.py, che e' un'altra cosa: quello
valida la correzione del vento sull'asimmetria per direzione. Questo tiene le
quattro soglie che decidono se una traiettoria entra nell'analisi.

Vive in pipeline/ e non in lab/ perche' deve servirli tutti e due: flightproc.py
gira sul Pi, dove lab/ non e' nel path, mentre lab/analysis.py e lab/site_build.py
mettono pipeline/ nel proprio. Nessuna dipendenza, di proposito.

Perche' esiste: il 29/08/2026 la metodologia diceva che si scartano i voli la cui
distanza volata e' *inferiore alla rotta diretta*, mentre il codice ne ammette
fino al 10% in meno -- e il paragrafo subito sotto, sulla stessa pagina, lo
diceva giusto. Le soglie stavano in due file e la pagina le raccontava a parole
in un terzo. Ora la pagina le LEGGE, e chi le cambia cambia il comportamento e il
testo insieme, che era il punto.
"""

# Quota della DURATA che deve stare fuori dalle lacune lunghe. Non e' un
# conteggio di campioni: e' tempo.
COV_MIN = 0.85

# Una lacuna e' "lunga" oltre questa soglia, in secondi.
GAP_THRESHOLD_S = 120.0

# La distanza ricostruita non puo' scendere sotto questa frazione
# dell'ortodromia. Le lacune fanno CONTARE MENO strada, quindi e' una guardia
# sulla troncatura della traccia, non un giudizio sul volo: un volo che devia
# molto ha volato PIU' della diretta e non viene toccato.
FLOWN_MIN_FRAC = 0.9

# Tratta minima, in km.
GC_MIN_KM = 150
