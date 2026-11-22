"""Wo der Bot seine Laufzeitdaten ablegt – und der einmalige Umzug dorthin.

Alle Pfade sind relativ zum Arbeitsverzeichnis, nicht zum Repo:
tools/multi_bots.py startet jede Instanz in multi_bots/botN/ und trennt die
Daten der Instanzen genau darüber.

Bewusst ohne Import von utils.logger: migrate_legacy_layout() muss laufen,
bevor der Logger beim Import das Log rotiert (main.py).
"""

import os
import re
from pathlib import Path

# Zustand, den der Bot selbst schreibt: Queue-Snapshot, Metadaten-Cache,
# Play-Counts, Playlists, heruntergeladene Tracks.
DATA_DIR = Path("data")
# bot.log des laufenden und bot.log.N der vorherigen Läufe.
LOG_DIR = Path("logs")

# Bis November 2026 lag das alles direkt im Arbeitsverzeichnis.
_LEGACY_DATA = ("last_queue.json", "metadata_cache.json", "play_counts.json", "playlists", "downloads")
_LEGACY_LOG = re.compile(r"bot\.log(\.\d+)?")


def _move(src: Path, dst: Path) -> bool:
    """Verschiebt src nach dst, ohne Vorhandenes zu überschreiben.
    True, wenn src danach weg ist."""
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            try:
                os.replace(src, dst)
                return True
            except OSError:
                # Ordner mit einer gerade geöffneten Datei darin (Windows):
                # dann einzeln, was sich bewegen lässt.
                if not src.is_dir():
                    return False
                dst.mkdir(exist_ok=True)
        if src.is_dir() and dst.is_dir():
            rest = [child for child in list(src.iterdir()) if not _move(child, dst / child.name)]
            if not rest:
                src.rmdir()
            return not rest
    except OSError:
        pass
    return False


def migrate_legacy_layout(base=".") -> tuple[list[str], list[str]]:
    """Zieht Laufzeitdaten aus dem alten Layout (alles im Arbeitsverzeichnis)
    nach data/ und logs/ um. Gibt (umgezogen, liegengeblieben) zurück.

    Läuft bei jedem Start und findet nach dem ersten Mal nichts mehr. Liegen
    bleibt, was am Ziel schon existiert oder gerade gesperrt ist – das wird
    nie überschrieben und beim nächsten Start erneut versucht.
    """
    base = Path(base)
    jobs = [(base / name, base / DATA_DIR / name) for name in _LEGACY_DATA]
    try:
        logs = sorted(p for p in base.iterdir() if _LEGACY_LOG.fullmatch(p.name) and p.is_file())
    except OSError:
        logs = []
    jobs += [(p, base / LOG_DIR / p.name) for p in logs]

    moved, left = [], []
    for src, dst in jobs:
        if src.exists():
            (moved if _move(src, dst) else left).append(src.name)
    return moved, left
