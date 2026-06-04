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
