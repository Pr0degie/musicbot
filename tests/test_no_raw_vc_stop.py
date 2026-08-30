"""Verhindert, dass wieder ein rohes voice_client.stop() ins Repo wandert.

Hintergrund: sobald der Bot mit VoiceRecvClient verbunden ist (Sprachsteuerung),
beendet dessen stop() nicht nur die Wiedergabe, sondern auch den Audio-Empfang.
Ein !s würde das Zuhören also stillschweigend abschalten – ein Fehler, der sich
nur als "nach dem ersten Skip reagiert er nicht mehr auf Sprache" zeigt und
stundenlang Suche kostet.

Deshalb läuft jeder Wiedergabe-Stopp über utils/voice.py → stop_playback().
"""
import re
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# utils/voice.py enthält den einzigen erlaubten rohen Aufruf.
ERLAUBT = {ROOT / "utils" / "voice.py"}

_RAW_STOP = re.compile(r"\b(?:voice_client|vc)\.stop\(\)")


def _quelldateien():
    for muster in ("cogs/*.py", "views/*.py", "utils/*.py"):
        for pfad in sorted(ROOT.glob(muster)):
            if pfad not in ERLAUBT:
                yield pfad


def _code_zeilen(pfad):
    """Zeilen mit echtem Code – Kommentare und Docstrings ausgenommen.

    Über vc.stop() muss man in Prosa reden dürfen, ohne den Test zu brechen.
    """
    prosa = set()
    with tokenize.open(pfad) as fh:
        for tok in tokenize.generate_tokens(fh.readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                prosa.update(range(tok.start[0], tok.end[0] + 1))
    quelle = pfad.read_text(encoding="utf-8").splitlines()
    return [(nr, zeile) for nr, zeile in enumerate(quelle, 1) if nr not in prosa]


@pytest.mark.parametrize("pfad", list(_quelldateien()), ids=lambda p: p.name)
def test_kein_rohes_vc_stop(pfad):
    treffer = [
        f"{pfad.relative_to(ROOT)}:{nr}: {zeile.strip()}"
        for nr, zeile in _code_zeilen(pfad)
        if _RAW_STOP.search(zeile)
    ]
    assert not treffer, (
        "Rohes .stop() gefunden – stattdessen utils.voice.stop_playback(vc) benutzen:\n"
        + "\n".join(treffer)
    )
