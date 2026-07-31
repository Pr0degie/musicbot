"""Abhängigkeitsfreie Text-Helfer (keine yt_dlp/discord-Importe → überall testbar)."""

import re

_BRACKET_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_FEAT_RE = re.compile(r"\s*(?:feat\.?|ft\.?|featuring)\s+\S.*", re.IGNORECASE)

# Varianten-Schlagwörter (Cover, Live, Remix, Sped-up, …): markieren einen
# Kandidaten als Variante eines Songs. Immer auf dem ROH-Titel prüfen – die
# Schlagwörter stehen oft in Klammern, die normalize_title wegstrippt.
_VARIANT_RE = re.compile(
    r"\b(cover|live|remix|sped[\s-]*up|nightcore|slowed|reverb|acoustic|instrumental|karaoke|8d)\b",
    re.IGNORECASE,
)


def normalize_title(title: str) -> str:
    """Normalisiert einen Song-Titel für Duplikat-Erkennung.

    YouTube-Titel folgen oft dem Muster "Artist - Song | Context | Context".
    Wir nehmen bevorzugt das Segment mit ' - ' (Artist-Trenner), damit
    'Winner's Performance | DARA - Bangaranga (Reprise) | ...' und
    'DARA - Bangaranga | ...' beide auf 'bangaranga dara' reduziert werden.
    Hat kein Segment ein ' - ', wird der GANZE Titel normalisiert – der
    frühere Fallback auf segments[0] erzeugte Mini-Wortmengen, gegen die
    kein Duplikat-Check mehr matchen konnte.
    """
    segments = title.split(" | ")
    core = next((s for s in segments if " - " in s), title)
    t = _BRACKET_RE.sub("", core)
    t = _FEAT_RE.sub("", t)
    t = re.sub(r"[^\w\s]", " ", t)
    words = re.sub(r"\s+", " ", t).strip().lower().split()
    return " ".join(sorted(words))


def has_variant_keyword(title: str) -> bool:
    """True, wenn der Roh-Titel ein Varianten-Schlagwort enthält
    (Cover/Live/Remix/Sped-up/Nightcore/…) – auch in Klammern."""
    return bool(_VARIANT_RE.search(title or ""))


def title_core_words(title: str) -> set:
    """Kern-Wortmenge für den Varianten-Vergleich: Klammer-Inhalte bleiben
    erhalten (dort steht oft der Original-Künstler: 'Africa (Toto Cover) -
    Alex Melton' behält 'toto'), die Varianten-Schlagwörter selbst fliegen raus."""
    t = _VARIANT_RE.sub(" ", title or "")
    t = _FEAT_RE.sub("", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return set(re.sub(r"\s+", " ", t).strip().lower().split())


def parse_time(s: str):
    """Parst '1:23' → 83 oder '83' → 83. Gibt None bei ungültiger Eingabe zurück."""
    s = s.strip()
    if ":" in s:
        parts = s.split(":", 1)
        try:
            return int(parts[0]) * 60 + int(parts[1])
        except ValueError:
            return None
    try:
        return int(s)
    except ValueError:
        return None


def progress_bar(elapsed, total, length=21):
    """'▬▬▬🔘▬▬▬▬ m:ss / m:ss' – None wenn Dauer unbekannt."""
    if not total or total <= 0:
        return None
    # elapsed auch fürs Zeitlabel clampen – sonst läuft die Anzeige über die
    # Songdauer hinaus und der Bar-String ändert sich endlos weiter (jeder
    # Tick ein neuer String → die _np_last_desc-Dedupe greift nie).
    elapsed = min(max(0.0, elapsed), total)
    frac = elapsed / total
    pos = int(frac * (length - 1))
    bar = "▬" * pos + "🔘" + "▬" * (length - 1 - pos)
    fmt = lambda s: f"{int(s) // 60}:{int(s) % 60:02d}"
    return f"{bar} {fmt(elapsed)} / {fmt(total)}"
