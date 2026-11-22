"""Windows-sichere Dateilöschung und atomares Schreiben (atomic_write_text).

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

import os
import stat
import threading
import time
from pathlib import Path

from utils.logger import logger

# os.replace auf eine Zieldatei, die gerade jemand offen hält (Virenscanner,
# Indexer, ein paralleler Leser), wirft unter Windows PermissionError – meist
# nur für Millisekunden. So oft/lange wird es erneut versucht.
_REPLACE_ATTEMPTS = 5
_REPLACE_RETRY_SECONDS = 0.05


def atomic_write_text(path, text: str, encoding: str = "utf-8") -> None:
    """Schreibt text atomar nach path: erst in eine temporäre Datei im selben
    Ordner, dann os.replace auf das Ziel.

    Ein Absturz oder Fehler mitten im Schreiben lässt damit die alte Datei
    vollständig stehen – ein direktes open(path, "w") hätte sie schon beim
    Öffnen abgeschnitten und eine halbe (unlesbare) JSON-Datei hinterlassen.
    Der Inhalt ist byte-identisch zu Path.write_text (gleicher Textmodus).

    Blockiert (Datei-I/O) – aus dem Event-Loop nur über asyncio.to_thread
    aufrufen, außer auf Shutdown-Pfaden. Fehler beim Schreiben propagieren wie
    bisher zum Aufrufer; die Temp-Datei wird dann wieder entfernt.

    Bleibt das Ziel dauerhaft gesperrt (Windows, siehe _REPLACE_ATTEMPTS), wird
    wie früher direkt geschrieben: was vorher funktionierte, scheitert durch
    den atomaren Weg nicht.
    """
    # realpath: ein Symlink als Ziel bleibt Symlink, ersetzt wird die Datei dahinter.
    target = Path(os.path.realpath(path))
    tmp = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass  # Dateisystem ohne fsync – der Rename bleibt trotzdem atomar
        try:
            # Rechte der bestehenden Datei übernehmen (relevant unter Linux/macOS).
            os.chmod(tmp, stat.S_IMODE(os.stat(target).st_mode))
        except OSError:
            pass  # Ziel existiert noch nicht → Standardrechte
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, target)
                return
            except PermissionError:
                if attempt + 1 < _REPLACE_ATTEMPTS:
                    time.sleep(_REPLACE_RETRY_SECONDS)
    except BaseException:
        _remove_quietly(tmp)
        raise
    # Ziel blieb gesperrt → direkter Write wie vor der Umstellung.
    _remove_quietly(tmp)
    logger.warning(f"[SAVE] {target.name} ließ sich nicht atomar ersetzen (in Benutzung) – schreibe direkt")
    with open(target, "w", encoding=encoding) as f:
        f.write(text)


def _remove_quietly(path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass

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
