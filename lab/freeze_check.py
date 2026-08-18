#!/usr/bin/env python3
"""Cancello del congelamento del contenuto durante il redesign.

Il redesign cambia la PRESENTAZIONE. Questo script rende meccanico il fatto che
non cambi anche ciò che il sito AFFERMA: fotografa le frasi del sito attuale e,
dopo il porting, dice quali sono sparite o sono state riscritte.

    freeze_check.py snapshot   # prima del porting, dal sito attuale
    freeze_check.py check      # dopo, contro le pagine nuove

Una frase che contiene una cifra è un'affermazione numerica: deve sopravvivere
identica. Le altre possono cambiare pagina, non parole.
"""
import json, os, re, html, sys, unicodedata
from pathlib import Path

HERE = Path(__file__).parent
# Mai un percorso assoluto: in questo progetto i percorsi fissi hanno gia' fatto
# girare sette volte un calcolo sul dataset sbagliato senza un errore. La radice
# si deriva dalla posizione dello script, e l'ambiente puo' sovrascriverla.
ROOT = HERE.parent
SITE = Path(os.environ.get("ADSB_SITE_DIR") or (ROOT / "site"))
BASE = HERE / "freeze_baseline.json"

# Pagine da confrontare: sorgente attuale -> pagine nuove che devono coprirle.
SNAP_PAGES = ["index.html", "methodology.html"]
CHECK_PAGES = ["index.html", "data.html", "methodology.html", "faq.html"]

DIGIT = re.compile(r"\d")
TAG = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.S | re.I)
WS = re.compile(r"\s+")
# I confini di blocco sono confini di frase: senza questo, celle e stat card —
# che non hanno punteggiatura — si incollano fra loro e producono "frasi"
# inesistenti che poi risultano sempre mancanti.
BLOCK = re.compile(r"</(p|div|li|td|th|tr|h[1-6]|section|table|dt|dd)\s*>|<br\s*/?>",
                   re.I)


def sentences(path: Path):
    """Frasi visibili di una pagina, normalizzate."""
    t = BLOCK.sub(" ¶ ", path.read_text())
    t = TAG.sub(" ", t)
    t = unicodedata.normalize("NFKC", html.unescape(t))
    t = t.replace("’", "'").replace("‑", "-")
    t = WS.sub(" ", t)
    # Taglio dopo . ! ? seguiti da spazio e maiuscola, e sui separatori di lista.
    parts = re.split(r"¶|(?<=[.!?])\s+(?=[A-Z“\"(])|\s+·\s+|\s+—\s+", t)
    out = []
    for p in parts:
        p = p.strip(" ·—-")
        # Le frasi cortissime sono etichette di colonna e navigazione, non
        # affermazioni: filtrarle evita un rumore che nasconde i veri scarti.
        if len(p) >= 40:
            out.append(p)
    return out


def norm(s):
    """Confronto insensibile a spaziatura e punteggiatura di contorno."""
    return WS.sub(" ", s.lower().strip(" .,;:()")).strip()


def snapshot():
    data = {}
    for p in SNAP_PAGES:
        f = SITE / p
        if not f.exists():
            sys.exit(f"manca {f}")
        data[p] = sentences(f)
    n_num = sum(1 for v in data.values() for s in v if DIGIT.search(s))
    BASE.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tot = sum(len(v) for v in data.values())
    print(f"fotografate {tot} frasi ({n_num} con cifre) da {len(data)} pagine")
    print(f"-> {BASE}")


def check(new_dir: Path):
    if not BASE.exists():
        sys.exit("nessuna fotografia: lancia prima 'snapshot'")
    base = json.loads(BASE.read_text())
    have = set()
    for p in CHECK_PAGES:
        f = new_dir / p
        if f.exists():
            have |= {norm(s) for s in sentences(f)}

    # Eccezioni approvate: modifiche di contenuto decise a voce, con la loro
    # motivazione scritta. Senza questo elenco il cancello accumula rumore noto
    # e si smette di leggerlo — che e' il modo in cui un cancello muore.
    exc = []
    f = HERE / "freeze_exceptions.json"
    if f.exists():
        exc = json.loads(f.read_text())

    def approved(s):
        n = norm(s)
        return next((e for e in exc if e["match"] in n), None)

    missing_num, missing_txt, waived = [], [], []
    for page, sents in base.items():
        for s in sents:
            if norm(s) in have:
                continue
            e = approved(s)
            if e:
                waived.append((page, s, e["reason"]))
            else:
                (missing_num if DIGIT.search(s) else missing_txt).append((page, s))

    print(f"frasi di riferimento: {sum(len(v) for v in base.values())}")
    tot = sum(len(v) for v in base.values())
    print(f"presenti invariate: {tot - len(missing_num) - len(missing_txt) - len(waived)}"
          f" · approvate come modificate: {len(waived)}")
    for label, items in (("AFFERMAZIONI NUMERICHE MANCANTI", missing_num),
                         ("frasi mancanti (senza cifre)", missing_txt)):
        print(f"\n=== {label}: {len(items)} ===")
        for page, s in items:
            print(f"  [{page}] {s[:150]}")
    print(f"\n=== modifiche approvate (non sono perdite): {len(waived)} ===")
    for page, sent, reason in waived:
        print(f"  [{page}] {sent[:90]}\n           -> {reason[:110]}")
    if missing_num:
        print("\n⚠️  Ogni riga della prima lista va giustificata una per una: "
              "spostata altrove, oppure è una modifica di contenuto non voluta.")
    return 1 if missing_num else 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "snapshot":
        snapshot()
    else:
        sys.exit(check(Path(sys.argv[2]) if len(sys.argv) > 2 else SITE))
