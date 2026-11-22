"""Downloader des MusicBot: resolve_track und seine vier Phasen.

Die übrigen Themen liegen in Mixins: yt_dlp-Instanzen (downloader_ydl),
Metadaten-Cache (downloader_cache), Downloads-Cleanup (downloader_cleanup),
Prefetch (downloader_prefetch), In-Flight-Registry und progressiver Download
(downloader_progressive).
"""

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

from utils.logger import logger
# Konstanten leben beim progressiven Download; hier re-exportiert für
# bestehende Importe (music.py, Tests).
from cogs.downloader_progressive import (  # noqa: F401
    PROGRESSIVE_BLOCK_TTL, PROGRESSIVE_BYTES_PER_SECOND, PROGRESSIVE_MIN_BYTES,
    PROGRESSIVE_POLL_INTERVAL, PROGRESSIVE_SIDECAR_SUFFIX, PROGRESSIVE_WAIT_TIMEOUT,
    STREAM_THRESHOLD_SECONDS, ProgressiveDownloadMixin,
)
# Nur gelesene Konstanten werden re-exportiert. Was Tests patchen (DOWNLOAD_DIR,
# DOWNLOADS_MAX_MB, YDL_COOKIES_FILE, YDL_BROWSER), steht ausschließlich im Zielmodul.
from cogs.downloader_cache import (  # noqa: F401
    CACHE_TTL, METADATA_CACHE_FILE, PERSISTED_CACHE_FIELDS, MetadataCacheMixin,
)
from cogs.downloader_ydl import FAST_PLAYER_CLIENTS, YdlMixin  # noqa: F401
from cogs.downloader_cleanup import DownloadsCleanupMixin
from cogs.downloader_prefetch import PrefetchMixin


@dataclass
class _Resolution:
    """Zwischenstand eines resolve_track-Aufrufs, von Phase zu Phase gereicht."""
    url: str
    info: dict
    filename: Path | None = None
    force_download: bool = False
    min_buffer_seconds: int = 0

    @property
    def title(self):
        return self.info.get("title", "Unbekannter Titel")

    @property
    def duration(self):
        return self.info.get("duration", 0)

    def result(self, source):
        """Rückgabe-Tupel von resolve_track; source ist Datei oder Stream-URL."""
        return self.info, source, self.title, self.duration


class Downloader(PrefetchMixin, DownloadsCleanupMixin, MetadataCacheMixin, YdlMixin,
                 ProgressiveDownloadMixin):
    """Verwaltet alle yt_dlp-Instanzen, den Metadaten-Cache und Download-Logik.

    Wird von MusicCommands als self.dl gehalten. Alle Methoden sind zustandslos
    bezüglich Queue/Playback – das bleibt in MusicCommands.
    """

    # Klassen-Default, damit auch ohne __init__ erzeugte Instanzen (Tests via
    # __new__) einen definierten Zustand haben.
    # Zuletzt von resolve_track gelieferte lokale Datei = die Datei, die FFmpeg
    # gerade abspielt (None wenn die aktive Quelle ein HTTP-Stream ist). Für den
    # Downloads-Cleanup tabu – auch wenn current_track schon weitergewandert ist.
    last_resolved_file = None

    def __init__(self, audio_format: str = "webm"):
        self.audio_format = audio_format
        # Cookielos starten (ADR 0010): eine eingeloggte YouTube-Session bekommt
        # Pre-Roll-Werbung, deren Skip-Zeit yt_dlp vor dem ersten Byte abwarten
        # MUSS (format["available_at"], sonst 403) – das kostet 5-6 s Startlatenz.
        # Cookies kommen erst, wenn YouTube sie wirklich verlangt.
        self._cookie_mode = False
        self._url_cache: dict = {}
        self._cache_timestamps: dict[str, float] = {}
        self._pending_resolves: dict[str, asyncio.Task] = {}
        self._init_progressive_state()
        self._purge_stale_progressive()
        self._load_cache()
        self._drop_cache_entries_without_id()
        self._init_ydl()

    # ------------------------------------------------------------------
    # Download-Logik
    # ------------------------------------------------------------------

    async def resolve_track(self, url: str, title: str, force_download=False,
                            min_buffer_seconds: int = 0):
        """Löst URL auf: extrahiert Metadaten und stellt sicher dass die Audiodatei lokal vorliegt.

        Läuft für die URL bereits ein schreibender Download (In-Flight-Registry:
        Queue-/Autoplay-Prefetch, progressiv, Download-Fallback), wird der
        adoptiert bzw. abgewartet statt ein zweiter gestartet.
        min_buffer_seconds: -ss-Ziel des Aufrufers – der progressive Pfad puffert
        zusätzlich so viele Sekunden, bevor die wachsende Datei zurückgegeben wird.
        Ungecachte kurze Tracks starten als progressiver Download (wachsende
        Datei, siehe is_incomplete()), nicht mehr als CDN-Stream; der Stream
        ist nur noch Fallback.
        force_download: Stream-Abkürzung deaktivieren und die Datei sofort lokal
        herunterladen (letzter Anlauf nach zwei toten Stream-Versuchen; yt_dlp
        nutzt den eigenen HTTP-Client, den CDN-403s gegen FFmpeg nicht treffen).
        Tracks über STREAM_THRESHOLD_SECONDS bleiben trotzdem Streams.
        Returns: (info, filename, title, duration)
        Raises asyncio.TimeoutError wenn extract_info > 30 s dauert.

        Vier Phasen, jede liefert ein Ergebnis oder reicht weiter:
        _resolve_metadata → _adopt_inflight → _clear_corpse → _acquire_file.
        """
        r = await self._resolve_metadata(url, title)
        if r.filename is None:
            # Langer Track → direkt streamen, kein lokaler Download nötig
            return self._stream_result(r, f"[Stream] {r.title} ({r.duration//60} min) wird gestreamt – kein Download")
        r.force_download, r.min_buffer_seconds = force_download, min_buffer_seconds
        result = await self._adopt_inflight(r)
        if result is None:
            result = self._clear_corpse(r)
        if result is None:
            result = await self._acquire_file(r)
        return result

    def _stream_result(self, r, message):
        """Ergebnis "als CDN-Stream spielen" – die aktive Quelle ist dann keine Datei."""
        logger.info(message)
        self.last_resolved_file = None
        return r.result(r.info.get("url") or r.url)

    def _file_result(self, r):
        self.last_resolved_file = r.filename
        return r.result(r.filename)

    async def _resolve_metadata(self, url, title):
        """Phase 1: Metadaten (Cache, laufender Prefetch-Resolve oder frisch)
        und Zieldatei. filename bleibt None für Tracks über
        STREAM_THRESHOLD_SECONDS."""
        info = await self._metadata_for(url, title)
        r = _Resolution(url, info)
        if r.duration > STREAM_THRESHOLD_SECONDS:
            return r
        try:
            r.filename = self._target_path(info)
            # Sofort als tabu markieren (nicht erst beim Return): der Song ist
            # bereits aus der Queue gepoppt und damit nicht mehr über den
            # protected_provider geschützt – ein parallel fertig werdender
            # Prefetch könnte sonst per Cleanup genau diese Datei löschen.
            self.last_resolved_file = r.filename
        except Exception as e:
            # Fallback-Garantie: Ein aus der Datei geladener reduzierter (oder
            # sonst kaputter) Cache-Eintrag darf nie zum Fehler beim User werden.
            # Eintrag verwerfen und frisch extrahieren – exakt der Kaltstart-Pfad.
            logger.warning(f"[Cache] Eintrag für {r.title!r} unbrauchbar ({type(e).__name__}: {e}) – verworfen, extrahiere frisch")
            self.invalidate(url)
            r = _Resolution(url, await self._fetch_info(url))
            if r.duration > STREAM_THRESHOLD_SECONDS:
                return r
            r.filename = self._target_path(r.info)
            self.last_resolved_file = r.filename
        return r

    async def _metadata_for(self, url, title):
        """Metadaten aus dem Cache, vom laufenden Prefetch-Resolve oder frisch
        extrahiert. Abgelaufene Einträge (CACHE_TTL) zählen als Cache-Miss,
        siehe _usable_cached_info."""
        info = self._usable_cached_info(url)
        if info is not None:
            logger.info(f"[Resolve] Metadaten aus Cache: {title}")
            return info
        pending = self._pending_resolves.get(url)
        if pending is not None:
            logger.info(f"[Resolve] Warte auf laufenden Prefetch-Task: {title}")
            # Ohne shield (anders als _settle): ein Cancel des Aufrufers
            # trifft auch den Resolve-Task.
            try:
                await pending
            except Exception:
                pass
        info = self._usable_cached_info(url)
        if info is not None:
            logger.info(f"[Resolve] Metadaten aus Prefetch-Task: {title}")
            return info
        if url in self._url_cache:
            logger.info(f"[Resolve] Cache-Eintrag abgelaufen – extrahiere frisch: {title}")
        return await self._fetch_info(url)

    async def _adopt_inflight(self, r):
        """Phase 2: In-Flight-Registry zuerst – schreibt bereits irgendein Pfad
        Bytes für diese URL? Lebendig vs. Leiche entscheidet NUR die Registry
        (bzw. _progressive), nie eine Datei-Heuristik.

        a) progressiv (Loop-Mode, !replay, !eq-Restart re-queuen den aktuellen
           Track) → adoptieren: am laufenden Task/der wachsenden Datei
           teilnehmen statt sie als fertig oder als Leiche zu missdeuten.
        b) Queue-/Autoplay-Prefetch o. Ä. → Task abwarten, danach normaler
           Datei-Check. None = weiter mit der nächsten Phase.
        """
        prog_task = self.progressive_task_for(r.url)
        if prog_task is None and (entry := self.inflight_download(r.url)) is not None:
            task, quelle = entry
            if quelle == "progressiv":
                prog_task = task
            else:
                logger.info(f"[Download] Warte auf laufenden {quelle} für: {r.title}")
                # Gescheitert → normaler Datei-Check in Phase 4, ggf. selbst laden.
                await self._settle(task)
        if prog_task is None:
            return None
        return await self._join_progressive(r, prog_task)

    async def _join_progressive(self, r, prog_task):
        """Fall a) von _adopt_inflight: am laufenden progressiven Download
        teilnehmen. Vier Ausgänge, None = weiter mit der nächsten Phase."""
        if await self._wait_for_buffer(r.filename, prog_task, r.min_buffer_seconds):
            logger.info(f"[Progressiv] Laufenden Download adoptiert: {r.title}")
            return self._file_result(r)
        if prog_task.done():
            # Download inzwischen gescheitert → wie "Datei fehlt" behandeln
            # (Aufräumen hat _run_progressive selbst erledigt).
            return None
        if not r.force_download:
            # Task lebt, puffert nur zu langsam → Stream-Fallback; der
            # Download läuft weiter und wird Cache. KEIN unlink, kein
            # Neustart: die wachsende Datei ist keine Leiche.
            return self._stream_result(
                r, f"[Stream] Laufender Download puffert zu langsam – starte als Stream: {r.title}")
        # Letzter Anlauf nach zwei toten Streams: der lebende Task IST der
        # Download – zu Ende warten, Datei-Check in Phase 4.
        await self._settle(prog_task)
        return None

    def _clear_corpse(self, r):
        """Phase 3: Leiche eines früheren Fehlschlags (unlink scheiterte, weil
        FFmpeg die Datei noch offen hatte): existiert, ist aber unvollständig,
        und die Registry kennt keinen lebenden Task mehr → wegräumen."""
        if self._remove_corpse(r.filename):
            return None
        # Datei blockiert → als Stream ausweichen; der nächste Bot-Start
        # räumt sie via _purge_stale_progressive weg.
        return self._stream_result(
            r, f"[Stream] Unvollständige Datei blockiert – starte als Stream: {r.title}")

    async def _acquire_file(self, r):
        """Phase 4: vorhandene Datei nehmen, sonst letzter Anlauf
        (force_download), progressiver Download oder CDN-Stream."""
        # Die zwei Datei-Rückgaben bewusst ohne erneutes last_resolved_file
        # (also nicht über _file_result): das setzte schon Phase 1.
        if r.filename.exists():
            logger.info(f"[Wiedergabe] Verwende vorhandene Datei: {r.filename.name}")
            return r.result(r.filename)
        if r.force_download:
            await self._last_resort_download(r)
        if r.filename.exists():
            logger.info(f"[Wiedergabe] Prefetch erfolgreich – starte sofort: {r.filename.name}")
            return r.result(r.filename)
        # Primärpfad: progressiver Download – yt_dlp lädt sofort, FFmpeg
        # spielt die wachsende Datei, sobald der Startpuffer steht. Gates:
        # mp3-Modus konvertiert erst NACH dem Download; m4a/mp4 ist
        # wachsend nicht lesbar (moov-Atom am Dateiende); geblockte URLs
        # sind gerade erst progressiv gescheitert.
        if not r.force_download and self._may_start_progressive(r.url, r.info):
            result = await self._play_progressive(r)
            if result is not None:
                return result
        # Fallback: direkter CDN-Stream (früherer Primärpfad).
        return self._stream_result(r, f"[Stream] Nicht gecacht – starte sofort als Stream: {r.title}")

    async def _last_resort_download(self, r):
        """Letzter Anlauf (force_download): kein Stream mehr, die Datei muss
        lokal her. Läuft (noch/inzwischen) ein lebender Download für die URL,
        IST der der letzte Anlauf – abwarten; nur wenn keiner läuft oder er
        scheitert, blockierend selbst laden. Schlägt der eigene Download fehl,
        propagiert die Exception zu play_next (Fehlerpfad überspringt den
        Track mit i18n-Meldung)."""
        other = self.inflight_download(r.url)
        if other is not None:
            logger.info(f"[Download-Fallback] Warte auf laufenden {other[1]}: {r.title}")
            await self._settle(other[0])
        if r.filename.exists():
            return
        logger.warning(f"[Download-Fallback] Stream schlug zweimal fehl – lade lokal herunter: {r.title}")
        self._register_inflight(r.url, "Download-Fallback", r.title)
        try:
            await asyncio.to_thread(self.ydl.download, [r.info.get("webpage_url") or r.url])
            logger.info(f"[Download-Fallback] Fertig: {r.filename.name}")
        finally:
            self._inflight_done(r.url, asyncio.current_task())
        await self.cleanup_downloads()

    async def _play_progressive(self, r):
        """Startet den progressiven Download und wartet auf den Startpuffer.
        None = Puffer kam nicht zustande, der Aufrufer weicht auf den Stream aus."""
        _t0 = time.monotonic()
        task = self._start_progressive(r.url, r.info, r.filename, r.title)
        if await self._wait_for_buffer(r.filename, task, r.min_buffer_seconds):
            try:
                _kb = r.filename.stat().st_size // 1024
            except OSError:
                _kb = 0
            logger.info(
                f"[Progressiv] Puffer erreicht ({_kb} KB nach {time.monotonic() - _t0:.1f}s) "
                f"– Wiedergabe startet: {r.title}"
            )
            return self._file_result(r)
        # Task NICHT canceln: bei bloßem Timeout darf er fertigladen
        # (Datei wird Cache); war er gescheitert, hat _run_progressive
        # bereits aufgeräumt.
        self.block_progressive(r.url)
        if task.done():
            logger.warning(f"[Progressiv] Download scheiterte sofort – Fallback auf CDN-Stream: {r.title}")
        else:
            logger.warning(
                f"[Progressiv] Pufferung nicht rechtzeitig ({PROGRESSIVE_WAIT_TIMEOUT:.0f}s) "
                f"– Fallback auf CDN-Stream: {r.title}"
            )
        return None
