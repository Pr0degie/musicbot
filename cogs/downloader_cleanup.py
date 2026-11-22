"""Downloads-Cleanup – Mixin für Downloader.

Hält downloads/ unter DOWNLOADS_MAX_MB (0 = aus), holt aufgeschobene
Löschungen nach und räumt beim Start die Reste abgebrochener progressiver
Downloads weg. Kein nacktes unlink, nur utils.files.safe_unlink (ADR 0003).
"""
import asyncio
import re
from pathlib import Path

from config import DOWNLOADS_MAX_MB
from utils.files import drain_pending_deletes, safe_unlink
from utils.logger import logger
from cogs.downloader_progressive import PROGRESSIVE_SIDECAR_SUFFIX
from cogs.downloader_ydl import DOWNLOAD_DIR

# Dateinamen in downloads/ enden auf " [<Video-ID>]" (siehe outtmpl). Der
# Titel-Stem-Schutz des Cleanups vergleicht den Teil davor mit den Queue-Titeln.
_ID_SUFFIX = re.compile(r" \[[^\]]*\]$")


class DownloadsCleanupMixin:
    # Callable ohne Argumente → (urls, titles) der Songs in Queue/current_track;
    # wird von MusicCommands gesetzt. None = Cleanup traut sich nichts zu löschen.
    protected_provider = None

    # ------------------------------------------------------------------
    # Downloads-Cleanup (DOWNLOADS_MAX_MB, 0 = aus)
    # ------------------------------------------------------------------

    async def cleanup_downloads(self, extra_protected: Path | None = None):
        """Hält downloads/ unter DOWNLOADS_MAX_MB; läuft nach jedem Download.

        Tabu sind: Dateien zu Songs in Queue/current_track (protected_provider,
        gesetzt von MusicCommands – doppelt abgesichert über prepare_filename
        UND den Titel als Datei-Stem), last_resolved_file (die aktive
        FFmpeg-Quelle) sowie extra_protected (z. B. der frisch geladene
        Autoplay-Song, der noch nicht in der Queue hängt). Jeder Fehler wird
        geschluckt: Cleanup darf niemals einen neuen Fehlerpfad einführen –
        im Zweifel bleibt eine Datei liegen.
        """
        # Zuerst und unabhängig vom DOWNLOADS_MAX_MB-Flag.
        await self._drain_pending()

        if DOWNLOADS_MAX_MB <= 0:
            return  # Flag nicht gesetzt → heutiges Verhalten, kein Cleanup
        if self.protected_provider is None:
            return  # ohne Queue-Info lieber gar nichts löschen
        try:
            urls, titles = self.protected_provider()
        except Exception:
            return
        protected_paths = self._protected_paths(urls, extra_protected)
        # Zweite Schutzschiene über den Titel (Dateiname = "<Titel> [<id>].<ext>"):
        # greift auch, wenn der Cache-Eintrag eines Queue-Songs fehlt/abgelaufen ist.
        protected_stems = {t for t in titles if t}
        try:
            await asyncio.to_thread(self._cleanup_downloads_sync, protected_paths, protected_stems)
        except Exception as e:
            logger.warning(f"[Cleanup] Fehlgeschlagen (ignoriert): {e}")

    async def _drain_pending(self):
        """Holt aufgeschobene Löschungen nach (Windows: Datei war beim unlink
        in Benutzung). Tabu bleiben die aktive FFmpeg-Quelle und wachsende
        progressive Dateien: ein Pfad aus der Pending-Liste könnte inzwischen
        neu heruntergeladen worden und wieder aktiv sein."""
        protect = set()
        for tabu in (self.last_resolved_file, *(self._progressive_files or {}).values()):
            if tabu is not None:
                try:
                    protect.add(Path(tabu).resolve())
                except OSError:
                    pass
        try:
            await asyncio.to_thread(drain_pending_deletes, protect)
        except Exception:
            pass  # Drain darf nie einen neuen Fehlerpfad einführen

    def _protected_paths(self, urls, extra_protected: Path | None = None) -> set:
        """Erste Schutzschiene des Größen-Cleanups: aufgelöste Pfade, die nicht
        gelöscht werden dürfen."""
        protected_paths = set()
        # Dateien der Songs in Queue/current_track, soweit ihr Cache-Eintrag
        # einen Dateinamen hergibt.
        for u in urls:
            info = self._url_cache.get(u)
            if not info:
                continue
            try:
                protected_paths.add(self._target_path(info).resolve())
            except Exception:
                continue
        # Wachsende Dateien laufender progressiver Downloads (samt Sidecars):
        # last_resolved_file reicht nicht – nach einem weiteren Skip zeigt es
        # schon auf den neuen Track, während der alte noch fertiglädt.
        _prog_files = list((self._progressive_files or {}).values())
        for tabu in (self.last_resolved_file, extra_protected, *_prog_files):
            if tabu is not None:
                try:
                    protected_paths.add(Path(tabu).resolve())
                    protected_paths.add(self._sidecar_for(Path(tabu)).resolve())
                except Exception:
                    pass
        return protected_paths

    def _cleanup_downloads_sync(self, protected_paths: set, protected_stems: set):
        """Löscht die ältesten Dateien (mtime), bis das Limit eingehalten ist.
        Läuft im Worker-Thread (asyncio.to_thread)."""
        limit = DOWNLOADS_MAX_MB * 1024 * 1024
        files = []
        for p in DOWNLOAD_DIR.iterdir():
            try:
                if p.is_file():
                    st = p.stat()
                    files.append((st.st_mtime, st.st_size, p))
            except OSError:
                continue
        total = sum(size for _, size, _ in files)
        if total <= limit:
            return
        for _, size, p in sorted(files, key=lambda item: item[0]):
            if total <= limit:
                break
            # Stem mit und ohne " [<id>]"-Suffix prüfen: neue Dateien tragen
            # die Video-ID, ältere heißen noch nur "<Titel>.<ext>".
            if (p.resolve() in protected_paths or p.stem in protected_stems
                    or _ID_SUFFIX.sub("", p.stem) in protected_stems):
                continue
            # defer=False: der Größen-Cleanup bewertet jeden Lauf frisch – eine
            # aufgeschobene Löschung könnte eine Datei treffen, die bis zum
            # Drain wieder in der Queue hängt. Gesperrte Datei → überspringen.
            if not safe_unlink(p, defer=False):
                continue
            total -= size
            logger.info(f"[Cleanup] Gelöscht ({size / (1024 * 1024):.1f} MB): {p.name}")

    def _purge_stale_progressive(self):
        """Startup-Aufräumen: .inprogress-Sidecars aus einem früheren Lauf
        markieren unvollständige Dateien (harter Crash mitten im Download,
        oder unlink scheiterte, weil FFmpeg noch las) → beide löschen."""
        purged = 0
        for sidecar in DOWNLOAD_DIR.glob(f"*{PROGRESSIVE_SIDECAR_SUFFIX}"):
            media = sidecar.with_name(sidecar.name[: -len(PROGRESSIVE_SIDECAR_SUFFIX)])
            if not safe_unlink(media):
                continue  # blockiert → Sidecar liegen lassen, nächster Start versucht es erneut
            if safe_unlink(sidecar):
                purged += 1
        # Aufgeschobene Löschungen aus diesem Prozess nachholen (beim echten
        # Start leer; relevant, wenn der Purge erneut aufgerufen wird).
        drain_pending_deletes()
        if purged:
            logger.info(f"[Progressiv] {purged} unvollständige Datei(en) aus früherem Lauf entfernt")
