"""Abhängigkeitsfreie Text-Helfer (keine yt_dlp/discord-Importe → überall testbar)."""

import re

_BRACKET_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_FEAT_RE = re.compile(r"\s*(?:feat\.?|ft\.?|featuring)\s+\S.*", re.IGNORECASE)


def normalize_title(title: str) -> str:
    """Normalisiert einen Song-Titel für Duplikat-Erkennung.

    YouTube-Titel folgen oft dem Muster "Artist - Song | Context | Context".
    Wir nehmen bevorzugt das Segment mit ' - ' (Artist-Trenner), damit
    'Winner's Performance | DARA - Bangaranga (Reprise) | ...' und
    'DARA - Bangaranga | ...' beide auf 'bangaranga dara' reduziert werden.
    """
    segments = title.split(" | ")
    core = next((s for s in segments if " - " in s), segments[0])
    t = _BRACKET_RE.sub("", core)
    t = _FEAT_RE.sub("", t)
    t = re.sub(r"[^\w\s]", " ", t)
    words = re.sub(r"\s+", " ", t).strip().lower().split()
    return " ".join(sorted(words))


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
