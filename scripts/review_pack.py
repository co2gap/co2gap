#!/usr/bin/env python3
"""Pacchetto autoportante per la revisione incrociata da altri modelli.

Estrae il testo delle pagine dal sito GENERATO — non da una copia scritta a
mano, che divergerebbe al primo aggiornamento — e lo impacchetta con le
istruzioni di revisione. Il file che ne esce si incolla o si allega a un modello
esterno, che non ha accesso al repo.

    lab-venv/bin/python scripts/review_pack.py > /tmp/co2gap_review.md
"""
import argparse
import html
import re
import sys
from pathlib import Path

SITE = Path(__file__).resolve().parents[1] / "site"

# Cosa mandare, e in quale ordine. La sezione firmata e la FAQ per prime: sono
# le uniche parti nuove e non ancora lette da nessuno, e le sole che esprimano
# opinioni attaccabili.
WANT = [
    ("index.html", "What I make of this", "Check it yourself",
     "LA SEZIONE FIRMATA DI OPINIONE (nuova, mai revisionata)"),
    ("faq.html", None, None, "LA PAGINA DI DOMANDE E RISPOSTE (nuova)"),
    ("index.html", "This is not fuel that could be saved", "flights analysed",
     "LA FRASE-SCUDO, che accompagna il numero principale"),
    ("index.html", "Read this before reading the table", "movements",
     "LA NOTA DI ATTRIBUZIONE sopra la tabella aeroporti"),
    ("methodology.html", "2. What is NOT being measured", "3. Why the comparison",
     "METODOLOGIA — cosa NON viene misurato"),
    ("methodology.html", "8. Stated limitations", "9. Privacy",
     "METODOLOGIA — limiti dichiarati"),
    ("methodology.html", "9. Privacy", "10. Who made this",
     "METODOLOGIA — privacy"),
]


BRIEF_2 = """# Seconda revisione critica — co2gap.org

Sei uno di piu' revisori interpellati separatamente sullo stesso materiale.

## Che cos'e'

co2gap.org e' un osservatorio indipendente che, dalle traiettorie ADS-B pubbliche
di 1.833.127 voli europei del 2026, calcola quanta CO2 ogni volo ha emesso in piu'
rispetto a un volo ideale: stesso aereo, rotta diretta, profilo di quota e
velocita' ottimo, **stesso vento reale**. Lo scarto e' scomposto in una parte
**laterale** (chilometri in piu') e una **verticale** (profilo meno efficiente).
Il sito e' scritto e firmato da una persona che **non e' un professionista
dell'aviazione ne' un climatologo**, dichiara di usare assistenza AI, e nomina
aeroporti reali. Va pubblicato fra pochi giorni; quattro organizzazioni citate
hanno gia' ricevuto un preavviso.

⚠️ **Questo materiale ha gia' superato una revisione da avversario**, e diverse
frasi sono state riscritte di conseguenza. Le obiezioni ovvie sono probabilmente
gia' state affrontate: **non fermarti alla prima cosa che salta all'occhio**, e se
un rilievo ti sembra evidente chiediti perche' sia sopravvissuto.

## Le tre domande

**1. Contraddizioni interne.** Il sito dice una cosa in un punto e un'altra
altrove? Cerca affermazioni che non stanno insieme: una pagina che rivendica cio'
che un'altra ammette di non poter sostenere, una cautela dichiarata in un posto e
smentita dal tono in un altro, due numeri presentati come se misurassero la stessa
cosa. **Cita entrambe le frasi**, non solo quella che ti sembra sbagliata.

**2. Il titolo peggiore.** Scrivi il titolo piu' dannoso e piu' fuorviante che un
giornalista ostile potrebbe trarre da questo materiale **restando formalmente
difendibile**, e indica **quale frase esatta glielo consente**. Non uno che
inventa: uno che si appoggia a qualcosa di davvero scritto qui.

**3. La domanda da conferenza stampa.** Hai trenta secondi e un microfono. Qual e'
la domanda che metterebbe l'autore piu' in difficolta' — non la piu' aggressiva,
la piu' difficile da rispondere onestamente senza indebolire il sito? Spiega
perche' e' difficile.

⚠️ **Quello che NON serve**: verifiche di fatti, cifre, citazioni o riferimenti
normativi. Non hai modo di controllarli e l'esperienza su questo progetto e' che
i modelli, in questi casi, **attribuiscono numeri inventati a fonti reali**. Se un
dato ti sembra sbagliato, **dillo come sospetto da verificare alla fonte**, mai
come correzione. Non proporre riscritture: indica il problema e perche' lo e'.

---
"""

ROLES = {
    "aeroporto": "\n**Il tuo ruolo**: leggi come l'ufficio comunicazione di un "
                 "aeroporto che si trova nominato in queste pagine. Che cosa "
                 "contesti, e con quale argomento?\n",
    "ricercatore": "\n**Il tuo ruolo**: leggi come chi lavora sull'efficienza di "
                   "volo per mestiere. Dove il metodo descritto non sostiene cio' "
                   "che il testo afferma?\n",
    "giornalista": "\n**Il tuo ruolo**: leggi come un giornalista che ha due ore e "
                   "deve scriverne. Quale frase citeresti, e come potrebbe essere "
                   "fraintesa una volta estratta dal contesto?\n",
}

def text_of(page, start=None, end=None):
    t = (SITE / page).read_text()
    t = re.sub(r"<(script|style)\b.*?</\1>", " ", t, flags=re.S)
    # L'indice della metodologia ripete OGNI titolo di sezione come voce di
    # elenco: cercandoci dentro, il marcatore di inizio trova la voce e quello
    # di fine la voce successiva, e si estrae un frammento vuoto. Va tolto
    # prima di cercare qualsiasi cosa.
    t = re.sub(r"<nav class=toc.*?</nav>", " ", t, flags=re.S)
    if start:
        i = t.find(start)
        if i < 0:
            return f"[non trovato: {start}]"
        t = t[i:]
    if end:
        j = t.find(end)
        if j > 0:
            t = t[:j]
    t = re.sub(r"</(p|div|li|h[1-6]|tr|section)\s*>|<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t]+", " ", t)
    return "\n".join(l.strip() for l in t.splitlines() if l.strip())


BRIEF = """# Richiesta di revisione critica — co2gap.org

Sei uno di piu' revisori interpellati separatamente sullo stesso materiale.

## Che cos'e'

co2gap.org e' un osservatorio indipendente che, dalle traiettorie ADS-B
pubbliche di 1.833.127 voli europei del 2026, calcola quanta CO2 ogni volo ha
emesso in piu' rispetto a un volo ideale: stesso aereo, rotta diretta, profilo
di quota e velocita' ottimo, **stesso vento reale**. Lo scarto e' scomposto in
una parte **laterale** (chilometri in piu') e una **verticale** (profilo meno
efficiente). Il sito e' scritto e firmato da una persona che **non e' un
professionista dell'aviazione ne' un climatologo**, dichiara di usare assistenza
AI, e nomina aeroporti reali. Va pubblicato fra pochi giorni; quattro
organizzazioni citate hanno gia' ricevuto un preavviso.

## Che cosa ti chiedo, e che cosa NO

**Quello che serve** e' una lettura da avversario:

1. **Quale frase verra' attaccata per prima**, e da chi — un aeroporto nominato,
   un fornitore di servizi di navigazione aerea, un giornalista scettico, un
   ricercatore del settore.
2. **Che cosa un lettore capira' male** anche in buona fede: ambiguita', frasi
   che sembrano dire piu' di quanto dicono, numeri che si prestano a essere
   citati fuori contesto.
3. **Quale affermazione non e' sostenuta** da cio' che il metodo dichiara di
   fare. Se il testo dice X ma il metodo descritto puo' sostenere solo Y,
   segnalalo: e' il difetto piu' grave possibile qui.
4. **Che cosa manca** che un lettore competente si aspetterebbe di trovare.
5. Nella **sezione firmata di opinione**: le affermazioni sono marcate
   abbastanza chiaramente come opinioni? Ce n'e' una che rivendica una
   competenza che l'autore dichiara di non avere?

⚠️ **Quello che NON serve**: verifiche di fatti, cifre, citazioni o riferimenti
normativi. Non hai modo di controllarli e l'esperienza su questo progetto e' che
i modelli, in questi casi, **attribuiscono numeri inventati a fonti reali**. Se
un dato ti sembra sbagliato, **dillo come sospetto da verificare alla fonte**,
mai come correzione. Non proporre riscritture del testo: indica il problema e
perche' e' un problema.

Rispondi per punti, dal piu' grave al piu' lieve, e per ciascuno di' **su quale
frase esatta** stai intervenendo.

---
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--giro", type=int, default=1, choices=(1, 2),
                    help="1 = lettura da avversario; 2 = contraddizioni, titolo, domanda")
    ap.add_argument("--ruolo", choices=sorted(ROLES),
                    help="inquadratura: stesso materiale, incarico diverso")
    a = ap.parse_args()
    out = [BRIEF_2 if a.giro == 2 else BRIEF]
    if a.ruolo:
        out.append(ROLES[a.ruolo])
    for page, start, end, label in WANT:
        out.append(f"\n## {label}\n\n_(da {page})_\n\n```\n{text_of(page, start, end)}\n```\n")
    sys.stdout.write("\n".join(out))


if __name__ == "__main__":
    main()
