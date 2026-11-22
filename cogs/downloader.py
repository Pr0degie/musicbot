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

from utils.files import safe_unlink
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


class Downloader(PrefetchMixin, DownloadsCleanupMixin, MetadataCacheMixin, YdlMixin,
                 ProgressiveDownloadMixin):
    """Verwaltet alle yt_dlp-Instanzen, den Metadaten-Cache und Download-Logik.

    Wird von MusicCommands als self.dl gehalten. Alle Methoden sind zustandslos
    bezüglich Queue/Playback – das bleibt in MusicCommands.
    """

    # Klassen-Defaults, damit auch ohne __init__ erzeugte Instanzen (Tests via
    # __new__) einen definierten Zustand haben.
    # Zuletzt von resolve_track gelieferte lokale Datei = die Datei, die FFmpeg
    # gerade abspielt (None wenn die aktive Quelle ein HTTP-Stream ist). Für den
    # Downloads-Cleanup tabu – auch wenn current_track schon weitergewandert ist.
    last_resolved_file = None
    # Progressive Downloads (Klassen-Defaults None, damit Tests via __new__ ohne
    # Voll-Setup funktionieren – alle Lese-Methoden sind None-tolerant).
    _progressive = None          # dict[url → asyncio.Task]: laufende progressive Downloads
    _progressive_files = None    # dict[url → Path]: Zieldateien (Schutz vor cleanup_downloads)
    _incomplete_files = None     # set[Path]: resolved Pfade unvollständiger Dateien
    _progressive_blocked = None  # dict[url → monotonic]: TTL-Blockliste nach Fehlschlag
    # In-Flight-Registry – Invariante: pro URL maximal ein schreibender Download.
    # Jeder Pfad, der Bytes für eine URL auf die Platte schreibt (Queue-Prefetch,
    # Autoplay-Prefetch, progressiv, Download-Fallback), registriert sich hier.
    _inflight = None             # dict[url → (asyncio.Task, quelle)]

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
        self._progressive: dict[str, asyncio.Task] = {}
        self._progressive_files: dict[str, Path] = {}
        self._incomplete_files: set[Path] = set()
        self._progressive_blocked: dict[str, float] = {}
        self._inflight: dict[str, tuple] = {}
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
        return r.info, r.info.get("url") or r.url, r.title, r.duration

    def _file_result(self, r):
        self.last_resolved_file = r.filename
        return r.info, r.filename, r.title, r.duration

    async def _resolve_metadata(self, url, title):
        """Phase 1: Metadaten (Cache, laufender Prefetch-Resolve oder frisch)
        und Zieldatei. filename bleibt None für Tracks über
        STREAM_THRESHOLD_SECONDS."""
        # Abgelaufene Einträge (CACHE_TTL) zählen als Cache-Miss, siehe
        # _usable_cached_info.
        info = self._usable_cached_info(url)
        if info is not None:
            logger.info(f"[Resolve] Metadaten aus Cache: {title}")
        else:
            if url in self._pending_resolves:
                logger.info(f"[Resolve] Warte auf laufenden Prefetch-Task: {title}")
                try:
                    await self._pending_resolves[url]
                except Exception:
                    pass
            info = self._usable_cached_info(url)
            if info is not None:
                logger.info(f"[Resolve] Metadaten aus Prefetch-Task: {title}")
            else:
                if url in self._url_cache:
                    logger.info(f"[Resolve] Cache-Eintrag abgelaufen – extrahiere frisch: {title}")
                info = await self._fetch_info(url)

        r = _Resolution(url, info)
        if r.duration > STREAM_THRESHOLD_SECONDS:
            return r
        try:
            r.filename = Path(self.ydl.prepare_filename(info))
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
            r.filename = Path(self.ydl.prepare_filename(r.info))
            self.last_resolved_file = r.filename
        return r

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
        url, title, filename = r.url, r.title, r.filename
        prog_task = self.progressive_task_for(url)
        entry = None if prog_task is not None else self.inflight_download(url)
        if entry is not None:
            if entry[1] == "progressiv":
                prog_task = entry[0]
            else:
                logger.info(f"[Download] Warte auf laufenden {entry[1]} für: {title}")
                try:
                    await asyncio.shield(entry[0])
                except Exception:
                    pass  # gescheitert → unten normaler Datei-Check, ggf. selbst laden
        if prog_task is not None:
            if await self._wait_for_buffer(filename, prog_task, r.min_buffer_seconds):
                logger.info(f"[Progressiv] Laufenden Download adoptiert: {title}")
                return self._file_result(r)
            if not prog_task.done():
                if r.force_download:
                    # Letzter Anlauf nach zwei toten Streams: der lebende Task
                    # IST der Download – zu Ende warten, Datei-Check unten.
                    try:
                        await asyncio.shield(prog_task)
                    except Exception:
                        pass
                else:
                    # Task lebt, puffert nur zu langsam → Stream-Fallback; der
                    # Download läuft weiter und wird Cache. KEIN unlink, kein
                    # Neustart: die wachsende Datei ist keine Leiche.
                    return self._stream_result(
                        r, f"[Stream] Laufender Download puffert zu langsam – starte als Stream: {title}")
            # Download inzwischen gescheitert → unten wie "Datei fehlt" behandeln
            # (Aufräumen hat _run_progressive selbst erledigt).
        return None

    def _clear_corpse(self, r):
        """Phase 3: Leiche eines früheren Fehlschlags (unlink scheiterte, weil
        FFmpeg die Datei noch offen hatte): existiert, ist aber unvollständig,
        und die Registry kennt keinen lebenden Task mehr → wegräumen."""
        filename = r.filename
        if filename.exists() and self.is_incomplete(filename):
            _resolved = filename.resolve()
            if safe_unlink(filename):
                safe_unlink(self._sidecar_for(filename))
                self._incomplete_files.discard(_resolved)
            else:
                # Datei blockiert → als Stream ausweichen; der nächste Bot-Start
                # räumt sie via _purge_stale_progressive weg.
                return self._stream_result(
                    r, f"[Stream] Unvollständige Datei blockiert – starte als Stream: {r.title}")
        return None

    async def _acquire_file(self, r):
        """Phase 4: vorhandene Datei nehmen, sonst letzter Anlauf
        (force_download), progressiver Download oder CDN-Stream."""
        url, info, title, filename, force_download = r.url, r.info, r.title, r.filename, r.force_download
        if not filename.exists():
            if force_download:
                # Läuft (noch/inzwischen) ein lebender Download für die URL,
                # IST der der letzte Anlauf – awaiten und Datei-Check
                # wiederholen; nur wenn keiner läuft oder er scheitert,
                # blockierend selbst laden.
                other = self.inflight_download(url)
                if other is not None:
                    logger.info(f"[Download-Fallback] Warte auf laufenden {other[1]}: {title}")
                    try:
                        await asyncio.shield(other[0])
                    except Exception:
                        pass
            if not filename.exists() and force_download:
                # Letzter Anlauf: kein Stream mehr, Datei jetzt blockierend laden.
                # Schlägt der Download fehl, propagiert die Exception zu play_next
                # (Fehlerpfad überspringt den Track mit i18n-Meldung).
                logger.warning(f"[Download-Fallback] Stream schlug zweimal fehl – lade lokal herunter: {title}")
                self._register_inflight(url, "Download-Fallback", title)
                try:
                    await asyncio.to_thread(self.ydl.download, [info.get("webpage_url") or url])
                    logger.info(f"[Download-Fallback] Fertig: {filename.name}")
                finally:
                    self._inflight_done(url, asyncio.current_task())
                await self.cleanup_downloads()
            if not filename.exists():
                # Primärpfad: progressiver Download – yt_dlp lädt sofort, FFmpeg
                # spielt die wachsende Datei, sobald der Startpuffer steht. Gates:
                # mp3-Modus konvertiert erst NACH dem Download; m4a/mp4 ist
                # wachsend nicht lesbar (moov-Atom am Dateiende); geblockte URLs
                # sind gerade erst progressiv gescheitert.
                if not force_download and self._may_start_progressive(url, info):
                    _t0 = time.monotonic()
                    task = self._start_progressive(url, info, filename, title)
                    if await self._wait_for_buffer(filename, task, r.min_buffer_seconds):
                        try:
                            _kb = filename.stat().st_size // 1024
                        except OSError:
                            _kb = 0
                        logger.info(
                            f"[Progressiv] Puffer erreicht ({_kb} KB nach {time.monotonic() - _t0:.1f}s) "
                            f"– Wiedergabe startet: {title}"
                        )
                        return self._file_result(r)
                    # Task NICHT canceln: bei bloßem Timeout darf er fertigladen
                    # (Datei wird Cache); war er gescheitert, hat _run_progressive
                    # bereits aufgeräumt.
                    self.block_progressive(url)
                    if task.done():
                        logger.warning(f"[Progressiv] Download scheiterte sofort – Fallback auf CDN-Stream: {title}")
                    else:
                        logger.warning(
                            f"[Progressiv] Pufferung nicht rechtzeitig ({PROGRESSIVE_WAIT_TIMEOUT:.0f}s) "
                            f"– Fallback auf CDN-Stream: {title}"
                        )
                # Fallback: direkter CDN-Stream (früherer Primärpfad).
                return self._stream_result(r, f"[Stream] Nicht gecacht – starte sofort als Stream: {title}")
            else:
                logger.info(f"[Wiedergabe] Prefetch erfolgreich – starte sofort: {filename.name}")
        else:
            logger.info(f"[Wiedergabe] Verwende vorhandene Datei: {filename.name}")

        # Bewusst ohne erneutes last_resolved_file: das setzte schon Phase 1.
        return r.info, r.filename, r.title, r.duration
