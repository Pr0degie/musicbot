"""Windows-sichere Dateilöschung.

Unter Linux ist unlink-while-open legal; Windows wirft PermissionError, wenn
ein anderer Prozess (typisch: FFmpeg) die Datei noch offen hält. safe_unlink
fängt das ab und merkt sich den Pfad in einer prozessweiten Pending-Liste;
drain_pending_deletes holt die Löschungen später nach (aufgerufen von
Downloader.cleanup_downloads nach jedem Download und von
_purge_stale_progressive beim Start). Über Neustarts hinweg persistiert die
Liste nicht — für Dateien in downloads/ übernehmen die .inprogress-Sidecars
(_purge_stale_progressive), DM-Bridge-Temp-WAVs räumt deren cog_load auf.

Auf Linux ist safe_unlink ein transparenter Durchgriff (unlink scheitert dort
nicht an offenen Handles), die Pending-Liste bleibt schlicht leer.
"""

import threading
from pathlib import Path

from utils.logger import logger

# Resolved Pfade, deren Löschung fehlschlug. Zugriff aus Event-Loop UND
# Worker-Threads (asyncio.to_thread) → Lock.
_pending_deletes: set[Path] = set()
_lock = threading.Lock()


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def safe_unlink(path, defer: bool = True) -> bool:
    """Löscht path; True = Datei ist danach weg (auch wenn sie nie existierte).

    Scheitert das Löschen (Windows: Datei in Benutzung → PermissionError),
    kommt False zurück; bei defer=True landet der Pfad zusätzlich in der
    Pending-Liste und wird beim nächsten drain_pending_deletes erneut
    versucht. defer=False nur, wenn der Aufrufer selbst neu entscheidet
    (z. B. der Größen-Cleanup, der jeden Lauf frisch bewertet).
    """
    p = Path(path)
    try:
        p.unlink()
    except FileNotFoundError:
        pass  # weg ist weg
    except OSError as e:
        if defer:
            with _lock:
                _pending_deletes.add(_resolved(p))
            logger.info(f"[SafeUnlink] {p.name} in Benutzung ({type(e).__name__}) – Löschung aufgeschoben")
        return False
    with _lock:
        _pending_deletes.discard(_resolved(p))
    return True


def drain_pending_deletes(protect: set | frozenset = frozenset()) -> int:
    """Holt aufgeschobene Löschungen nach; protect (resolved Pfade) wird
    übersprungen und bleibt in der Liste. Läuft synchron (Aufrufer nutzt
    asyncio.to_thread). Rückgabe: Anzahl jetzt entfernter Dateien."""
    with _lock:
        pending = list(_pending_deletes)
    deleted = 0
    for p in pending:
        if p in protect:
            continue
        try:
            p.unlink()
        except FileNotFoundError:
            pass  # schon weg → nur aus der Liste nehmen
        except OSError:
            continue  # weiterhin in Benutzung → nächster Drain
        with _lock:
            _pending_deletes.discard(p)
        deleted += 1
    if deleted:
        logger.info(f"[SafeUnlink] {deleted} aufgeschobene Löschung(en) nachgeholt")
    return deleted
