"""yt_dlp-Instanzen, Metadaten-Cache und Download-Logik für den MusicBot."""

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path

import yt_dlp
from config import DOWNLOADS_MAX_MB, YDL_BROWSER, YDL_COOKIES_FILE
from utils.files import drain_pending_deletes, safe_unlink
from utils.logger import logger
from utils.autoplay import choose_autoplay_candidate, entry_url, select_autoplay_candidates, yt_video_id
# Konstanten leben beim progressiven Download; hier re-exportiert für
# bestehende Importe (music.py, Tests).
from cogs.downloader_progressive import (  # noqa: F401
    PROGRESSIVE_BLOCK_TTL, PROGRESSIVE_BYTES_PER_SECOND, PROGRESSIVE_MIN_BYTES,
    PROGRESSIVE_POLL_INTERVAL, PROGRESSIVE_SIDECAR_SUFFIX, PROGRESSIVE_WAIT_TIMEOUT,
    STREAM_THRESHOLD_SECONDS, ProgressiveDownloadMixin,
)

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

METADATA_CACHE_FILE = Path("metadata_cache.json")
CACHE_TTL = 7200  # 2 Stunden – CDN-URLs von YouTube laufen danach ab

# Marker in yt_dlp-Fehlermeldungen, die nach Authentifizierung verlangen. Nur
# dann lohnt der Zweitversuch mit Cookies – bei Netzwerk-/Format-Fehlern würde
# er nur Zeit kosten. Siehe _needs_cookies() → [ADR 0010].
COOKIE_ERROR_MARKERS = (
    "sign in to confirm",
    "confirm you're not a bot",
    "confirm your age",
    "age-restricted",
    "age restricted",
    "inappropriate for some users",
    "members-only",
    "members only",
    "private video",
    "login required",
    "this video is available to this channel's members",
    "use --cookies",
    "--cookies-from-browser",
)

# Nur diese Felder werden nach metadata_cache.json persistiert – ermittelt per
# grep über alle info.get()/info[...]-Zugriffe plus prepare_filename-Bedarf
# (outtmpl "%(title)s.%(ext)s"). Volle yt_dlp-Info-Dicts haben ~90 Keys und
# machen die Datei um Größenordnungen fetter. In-Memory bleibt immer das
# volle Dict; ein aus der Datei geladener reduzierter Eintrag, der beim
# Verwenden doch scheitert, wird verworfen und wie ein Cache-Miss behandelt
# (siehe prepare_filename-Fallback in resolve_track/prefetch_next).
#   title        Anzeige + prepare_filename
#   ext          prepare_filename
#   duration     Stream-Schwelle, Embed, Fortschrittsbalken
#   url          direkte CDN-Stream-URL (Stream-Pfad)
#   webpage_url  Embed-Link, Download-Ziel in prefetch_next
#   thumbnail    Embed-Thumbnail
#   uploader     Embed-Titel/-Footer
#   http_headers FFmpeg -headers im Stream-Pfad (music._ffmpeg_header_opts)
PERSISTED_CACHE_FIELDS = (
    "title", "ext", "duration", "url", "webpage_url",
    "thumbnail", "uploader", "http_headers",
)


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


class Downloader(ProgressiveDownloadMixin):
    """Verwaltet alle yt_dlp-Instanzen, den Metadaten-Cache und Download-Logik.

    Wird von MusicCommands als self.dl gehalten. Alle Methoden sind zustandslos
    bezüglich Queue/Playback – das bleibt in MusicCommands.
    """

    # Klassen-Defaults, damit auch ohne __init__ erzeugte Instanzen (Tests via
    # __new__) einen definierten Zustand haben. _cache_dirty: gesetzt bei jeder
    # Cache-Änderung, geleert vom Debounce-Flush (music._persist_flush_loop)
    # bzw. flush_cache_now.
    _cache_dirty = False
    # Zuletzt von resolve_track gelieferte lokale Datei = die Datei, die FFmpeg
    # gerade abspielt (None wenn die aktive Quelle ein HTTP-Stream ist). Für den
    # Downloads-Cleanup tabu – auch wenn current_track schon weitergewandert ist.
    last_resolved_file = None
    # Callable ohne Argumente → (urls, titles) der Songs in Queue/current_track;
    # wird von MusicCommands gesetzt. None = Cleanup traut sich nichts zu löschen.
    protected_provider = None
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
    # Serialisiert die eigentlichen yt_dlp-Downloads der Prefetch-Tasks:
    # yt_dlp ist nicht thread-safe, und ein adoptierter (vom Aufrufer
    # gecancelter, aber weiterlaufender) Download darf nie parallel zu einem
    # frisch gekickten laufen. Lazy angelegt (Tests via __new__).
    _prefetch_dl_lock = None

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
        self._init_ydl()

    # ------------------------------------------------------------------
    # Instanz-Verwaltung
    # ------------------------------------------------------------------

    def rebuild(self, audio_format: str, keep_urls: set | None = None):
        """Baut alle yt_dlp-Instanzen neu auf.

        keep_urls: Cache-Einträge für diese URLs behalten (z.B. Queue-Songs).
        None = Cache komplett leeren.
        """
        self._save_cache()
        self.audio_format = audio_format
        if keep_urls is not None:
            self._url_cache = {k: v for k, v in self._url_cache.items() if k in keep_urls}
            self._cache_timestamps = {k: v for k, v in self._cache_timestamps.items() if k in keep_urls}
        else:
            self._url_cache.clear()
            self._cache_timestamps.clear()
        self._pending_resolves.clear()
        self._init_ydl()

    def clear_cache(self):
        self._url_cache.clear()

    def invalidate(self, url: str):
        """Wirft den Cache-Eintrag einer URL weg (z.B. nach einem 403 vom CDN),
        damit der nächste resolve_track eine frische Stream-URL extrahiert."""
        self._url_cache.pop(url, None)
        self._cache_timestamps.pop(url, None)

    def _load_cache(self):
        """Lädt persistierten Metadaten-Cache vom letzten Bot-Lauf (TTL: 2h).

        Liest sowohl das reduzierte Format (PERSISTED_CACHE_FIELDS) als auch
        alte Dateien im vollen Info-Dict-Format. Unlesbare Dateien oder
        Einträge werden verworfen (geloggt) – der Bot startet dann einfach
        mit kaltem Cache, niemals mit einem Fehler.
        """
        if not METADATA_CACHE_FILE.exists():
            return
        try:
            data = json.loads(METADATA_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[Cache] {METADATA_CACHE_FILE.name} nicht lesbar – verworfen (Kaltstart): {e}")
            return
        if not isinstance(data, dict):
            logger.warning(f"[Cache] {METADATA_CACHE_FILE.name} hat unerwartetes Format – verworfen (Kaltstart)")
            return
        now = time.time()
        loaded = 0
        for url, entry in data.items():
            if not isinstance(entry, dict):
                continue
            ts = entry.pop("_ts", 0.0)
            if isinstance(ts, (int, float)) and now - ts < CACHE_TTL:
                self._url_cache[url] = entry
                self._cache_timestamps[url] = ts
                loaded += 1
        if loaded:
            logger.info(f"[Cache] {loaded} Einträge aus {METADATA_CACHE_FILE.name} geladen")

    def _serialize_cache(self) -> str:
        """Reduzierter JSON-Snapshot des Caches (nur TTL-gültige Einträge,
        nur PERSISTED_CACHE_FIELDS). Läuft auf dem Event-Loop – dadurch kann
        das Dict während der Serialisierung nicht gleichzeitig mutieren."""
        now = time.time()
        data = {}
        for url, info in list(self._url_cache.items()):
            ts = self._cache_timestamps.get(url, now)
            if now - ts < CACHE_TTL:
                data[url] = {k: info[k] for k in PERSISTED_CACHE_FIELDS if k in info}
                data[url]["_ts"] = ts
        return json.dumps(data, ensure_ascii=False, default=str)

    def _save_cache(self):
        """Schreibt aktuellen Metadaten-Cache synchron auf Disk (reduziert)."""
        try:
            METADATA_CACHE_FILE.write_text(self._serialize_cache(), encoding="utf-8")
        except Exception as e:
            logger.debug(f"[Cache] Speichern fehlgeschlagen: {e}")

    def flush_cache_now(self):
        """Synchroner Flush für Shutdown-Pfade (cog_unload, !restart)."""
        if not self._cache_dirty:
            return
        self._cache_dirty = False
        self._save_cache()

    async def flush_cache(self):
        """Debounce-Flush, aufgerufen vom 30-s-Loop in music.py. Serialisiert
        auf dem Event-Loop, schreibt im Worker-Thread. Bewusster Trade-off:
        bei hartem Crash gehen bis zu 30 s Cache-Änderungen verloren – das
        ist nur ein Cache-Miss beim nächsten Start, kein Datenverlust."""
        if not self._cache_dirty:
            return
        self._cache_dirty = False
        try:
            payload = self._serialize_cache()
            await asyncio.to_thread(METADATA_CACHE_FILE.write_text, payload, encoding="utf-8")
        except Exception as e:
            self._cache_dirty = True
            logger.debug(f"[Cache] Speichern fehlgeschlagen: {e}")

    async def _fetch_info(self, url: str) -> dict:
        """extract_info im Worker-Thread; entpackt Playlist-Wrapper, cached das
        volle Info-Dict in-memory und markiert den Cache als dirty (persistiert
        wird reduziert und gedebounct)."""
        info = await self.extract_info_async(url, "main")
        if "entries" in info:
            info = info["entries"][0]
        self._url_cache[url] = info
        self._cache_timestamps[url] = time.time()
        self._cache_dirty = True
        return info

    async def _start_resolve(self, url: str):
        """Startet extract_info als Hintergrund-Task damit resolve_track() einen Cache-Hit findet."""
        if url in self._url_cache or url in self._pending_resolves:
            return

        async def _fetch():
            try:
                await self._fetch_info(url)
            except Exception:
                pass
            finally:
                self._pending_resolves.pop(url, None)

        task = asyncio.create_task(_fetch())
        self._pending_resolves[url] = task

    async def warmup(self):
        """Wärmt yt_dlp's Node.js/JS-Player-Cache vor, damit der erste echte Song schneller startet."""
        warmup_url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # "Me at the zoo" – erstes YT-Video, wird nie gelöscht
        if warmup_url in self._url_cache:
            logger.info("[Warmup] JS-Player-Cache bereits warm (aus persistentem Cache).")
            return
        try:
            logger.info("[Warmup] Starte yt_dlp JS-Player-Vorwärmung im Hintergrund ...")
            info = await self.extract_info_async(warmup_url, "main", timeout=25.0)
            if info:
                if "entries" in info:
                    info = info["entries"][0]
                self._url_cache[warmup_url] = info
                self._cache_timestamps[warmup_url] = time.time()
                self._cache_dirty = True
            logger.info("[Warmup] yt_dlp JS-Player-Cache bereit.")
        except Exception as e:
            logger.debug(f"[Warmup] Fehlgeschlagen (ignoriert): {e}")

    @staticmethod
    def _cookies_configured() -> bool:
        """Ist überhaupt eine Cookie-Quelle eingerichtet?"""
        return bool(YDL_COOKIES_FILE or YDL_BROWSER)

    def _cookie_opts(self) -> dict:
        """Cookie-Optionen für yt_dlp – leer, solange der cookielose Modus
        aktiv ist (Normalfall, siehe __init__ und ADR 0010)."""
        if not self._cookie_mode:
            return {}
        if YDL_COOKIES_FILE:
            return {"cookiefile": YDL_COOKIES_FILE}
        if YDL_BROWSER:
            return {"cookiesfrombrowser": (YDL_BROWSER,)}
        return {}

    @staticmethod
    def _needs_cookies(exc: BaseException) -> bool:
        """Verlangt dieser yt_dlp-Fehler nach einer angemeldeten Session?"""
        msg = str(exc).lower()
        return any(marker in msg for marker in COOKIE_ERROR_MARKERS)

    def enable_cookie_mode(self, grund: str = "") -> bool:
        """Schaltet dauerhaft auf Cookie-Authentifizierung um und baut die
        yt_dlp-Instanzen neu auf. False, wenn schon aktiv oder keine Cookie-
        Quelle konfiguriert ist (dann bleibt nur der cookielose Versuch).

        Der Metadaten-Cache wird geleert: cookielos extrahierte Einträge
        stammen aus einer anderen Session, der Zweitversuch soll wirklich
        frisch mit Cookies auflösen.
        """
        if self._cookie_mode or not self._cookies_configured():
            return False
        self._cookie_mode = True
        self._url_cache.clear()
        self._cache_timestamps.clear()
        self._pending_resolves.clear()
        self._init_ydl()
        logger.warning(f"[Cookies] YouTube verlangt eine angemeldete Session ({grund}) – Cookie-Modus aktiv.")
        return True

    # Kind → Attributname; erst beim Aufruf aufgelöst, damit nach einem
    # _init_ydl()/rebuild() nie eine veraltete Instanz benutzt wird.
    _YDL_KINDS = {
        "main": "ydl",
        "search": "search_ydl",
        "url": "url_ydl",
        "playlist": "playlist_ydl",
        "autoplay": "autoplay_ydl",
    }

    async def extract_info_async(self, query: str, kind: str = "main", *, timeout: float = 30.0):
        """extract_info im Worker-Thread, mit Cookie-Fallback.

        Scheitert der cookielose Versuch an fehlender Authentifizierung
        (Alterssperre, Bot-Check, Mitglieder-Video), wird einmal mit Cookies
        wiederholt; danach bleibt der Cookie-Modus für alle Instanzen aktiv.
        Timeouts werden nicht wiederholt – der Aufrufer meldet sie selbst.
        Raises asyncio.TimeoutError nach `timeout` Sekunden.
        """
        def _extract():
            return asyncio.to_thread(
                getattr(self, self._YDL_KINDS[kind]).extract_info, query, download=False
            )

        try:
            return await asyncio.wait_for(_extract(), timeout=timeout)
        except asyncio.TimeoutError:
            raise
        except Exception as e:
            if not (self._needs_cookies(e) and self.enable_cookie_mode(str(e)[:120])):
                raise
            logger.info(f"[Cookies] Zweitversuch mit Cookies: {query[:80]}")
        return await asyncio.wait_for(_extract(), timeout=timeout)

    def _build_base_opts(self) -> dict:
        _cookies = self._cookie_opts()
        return {
            "quiet": True,
            "no_warnings": True,
            # Bevorzugt Opus/webm mit mindestens 160kbps (YouTube's höchste Audio-Tier),
            # fällt auf 128kbps, dann beliebiges webm, dann Opus, dann best zurück.
            # prefer_free_formats bevorzugt Opus über AAC bei gleichwertiger Qualität.
            "format": "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best",
            "prefer_free_formats": True,
            "default_search": "ytsearch",
            "noplaylist": False,
            # prepare_filename() gibt später den exakt gleichen Pfad zurück den
            # yt_dlp beim Speichern verwendet – inklusive Sonderzeichen-Bereinigung.
            "outtmpl": str(DOWNLOAD_DIR / "%(title)s.%(ext)s"),
            # Netzwerk-Timeout direkt in yt_dlp – bricht den Thread intern ab,
            # sodass asyncio.wait_for nicht auf den Thread warten muss.
            "socket_timeout": 15,
            # Node.js als JS-Runtime für den EJS-Signature-Solver aktivieren.
            "js_runtimes": {"node": {}},
            **_cookies,
        }

    def _make_progressive_ydl(self) -> "yt_dlp.YoutubeDL":
        """Frische Wegwerf-Instanz für einen progressiven Download.

        Eigene Instanz pro Download, weil yt_dlp nicht threadsafe ist (läuft
        parallel zu prefetch_next auf self.ydl) und rebuild() die geteilten
        Instanzen mitten im Download austauschen kann. nopart schreibt direkt
        in den Zielnamen: FFmpeg hält die wachsende Datei offen, das übliche
        .part-Rename würde auf Windows mit Sharing Violation scheitern.
        outtmpl ist identisch zu self.ydl → prepare_filename(info) der
        Hauptinstanz liefert exakt den Pfad, in den hier geschrieben wird.
        Keine mp3-Postprocessors: der progressive Pfad ist im mp3-Modus gesperrt.
        """
        return yt_dlp.YoutubeDL({**self._build_base_opts(), "nopart": True, "noplaylist": True})

    def _init_ydl(self):
        base_opts = self._build_base_opts()
        if self.audio_format == "mp3":
            # MP3-Konvertierung läuft über FFmpeg als Post-Processing-Schritt.
            base_opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ]
        self.ydl = yt_dlp.YoutubeDL(base_opts)

        # Gecachte Instanzen für Suche und URL-Abfragen.
        _cookies = self._cookie_opts()
        _js = {"js_runtimes": {"node": {}}}
        self.search_ydl = yt_dlp.YoutubeDL({
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
            "socket_timeout": 15,
            **_cookies,
            **_js,
        })
        _url_base = {
            "quiet": True,
            "no_warnings": True,
            "format": "bestaudio/best",
            "default_search": "ytsearch",
            "socket_timeout": 15,
            **_cookies,
            **_js,
        }
        self.url_ydl = yt_dlp.YoutubeDL({**_url_base, "noplaylist": True, "extract_flat": False})
        self.playlist_ydl = yt_dlp.YoutubeDL({**_url_base, "noplaylist": False, "extract_flat": "in_playlist"})
        # Autoplay: nur Metadaten, max. 10 Einträge – kein vollständiger Playlist-Scan.
        self.autoplay_ydl = yt_dlp.YoutubeDL({
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
            "noplaylist": False,
            "playlistend": 10,
            "socket_timeout": 15,
            **_cookies,
            **_js,
        })

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
        # Aufgeschobene Löschungen (Windows: Datei war beim unlink in Benutzung)
        # zuerst nachholen – unabhängig vom DOWNLOADS_MAX_MB-Flag. Tabu bleiben
        # die aktive FFmpeg-Quelle und wachsende progressive Dateien: ein Pfad
        # aus der Pending-Liste könnte inzwischen neu heruntergeladen worden
        # und wieder aktiv sein.
        _drain_protect = set()
        for tabu in (self.last_resolved_file, *(self._progressive_files or {}).values()):
            if tabu is not None:
                try:
                    _drain_protect.add(Path(tabu).resolve())
                except OSError:
                    pass
        try:
            await asyncio.to_thread(drain_pending_deletes, _drain_protect)
        except Exception:
            pass  # Drain darf nie einen neuen Fehlerpfad einführen

        if DOWNLOADS_MAX_MB <= 0:
            return  # Flag nicht gesetzt → heutiges Verhalten, kein Cleanup
        if self.protected_provider is None:
            return  # ohne Queue-Info lieber gar nichts löschen
        try:
            urls, titles = self.protected_provider()
        except Exception:
            return
        protected_paths = set()
        for u in urls:
            info = self._url_cache.get(u)
            if not info:
                continue
            try:
                protected_paths.add(Path(self.ydl.prepare_filename(info)).resolve())
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
        # Zweite Schutzschiene über den Titel (Dateiname = "<Titel>.<ext>"):
        # greift auch, wenn der Cache-Eintrag eines Queue-Songs fehlt/abgelaufen ist.
        protected_stems = {t for t in titles if t}
        try:
            await asyncio.to_thread(self._cleanup_downloads_sync, protected_paths, protected_stems)
        except Exception as e:
            logger.warning(f"[Cleanup] Fehlgeschlagen (ignoriert): {e}")

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
            if p.resolve() in protected_paths or p.stem in protected_stems:
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
        if url in self._url_cache:
            info = self._url_cache[url]
            logger.info(f"[Resolve] Metadaten aus Cache: {title}")
        else:
            if url in self._pending_resolves:
                logger.info(f"[Resolve] Warte auf laufenden Prefetch-Task: {title}")
                try:
                    await self._pending_resolves[url]
                except Exception:
                    pass
            if url in self._url_cache:
                info = self._url_cache[url]
                logger.info(f"[Resolve] Metadaten aus Prefetch-Task: {title}")
            else:
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

    async def prefetch_next(self, queue, idx: int = 0):
        """Lädt Song an Position idx der Queue still im Hintergrund herunter.

        Der eigentliche Download läuft als eigener Task pro URL, der auch in
        der In-Flight-Registry steht: resolve_track wartet damit nur auf die
        konkret gebrauchte URL statt auf den Sammel-Task des Aufrufers, und
        ein Cancel des Aufrufers (play_next, !clear, Queue-Umbau) killt den
        Worker-Thread nicht mitten im Schreiben – der Download wird adoptiert,
        läuft zu Ende und deregistriert sich erst dann selbst (ADR 0002).
        """
        if len(queue) <= idx:
            return
        url, title = queue[idx]  # Peek – nicht aus der Queue entfernen
        if self.progressive_task_for(url) is not None:
            # Läuft bereits progressiv → nie zwei Downloads auf dieselbe Datei
            # (das .part-Rename des Prefetch liefe gegen die offene FFmpeg-Datei).
            logger.info(f"[Prefetch] Übersprungen – läuft bereits progressiv: {title}")
            return
        worker = asyncio.create_task(self._prefetch_one(url, title))
        if self._register_inflight(url, "Queue-Prefetch", title, task=worker) is not None:
            # Anderer Pfad schreibt diese URL bereits – kein zweiter Writer.
            # Der Worker hat noch keine Zeile ausgeführt → sauber abbrechen.
            worker.cancel()
            return
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            if not worker.done():
                logger.info(f"[Prefetch] Aufrufer abgebrochen – laufender Download wird adoptiert und läuft zu Ende: {title}")
            raise
        except Exception:
            pass  # Fehler hat _prefetch_one bereits geloggt – Prefetch ist nie fatal

    async def _prefetch_one(self, url: str, title: str):
        """Der eigentliche Prefetch-Download einer URL (siehe prefetch_next).
        Deregistriert sich erst im finally – solange der Worker-Thread
        schreibt, meldet die Registry also einen lebenden Writer."""
        try:
            if url in self._url_cache:
                info = self._url_cache[url]
                logger.info(f"[Prefetch] Metadaten aus Cache: {title}")
            else:
                info = await self._fetch_info(url)
            if info.get("duration", 0) > STREAM_THRESHOLD_SECONDS:
                logger.info(f"[Prefetch] Übersprungen – {info.get('title', title)} wird gestreamt")
                return
            try:
                filename = Path(self.ydl.prepare_filename(info))
            except Exception:
                # Kaputter/reduzierter Cache-Eintrag → verwerfen, frisch
                # extrahieren (Cache-Miss-Pfad). Schlägt auch das fehl, fängt
                # der äußere except unten – Prefetch-Fehler sind nie fatal.
                self.invalidate(url)
                info = await self._fetch_info(url)
                if info.get("duration", 0) > STREAM_THRESHOLD_SECONDS:
                    return
                filename = Path(self.ydl.prepare_filename(info))
            if filename.exists() and self.is_incomplete(filename):
                # Leiche eines gescheiterten progressiven Downloads → erst weg,
                # sonst hält yt_dlp die Datei für fertig. Blockiert (FFmpeg liest
                # noch) → diesen Prefetch auslassen.
                if not safe_unlink(filename):
                    return
                safe_unlink(self._sidecar_for(filename))
                self._incomplete_files.discard(filename.resolve())
            if not filename.exists():
                if self._prefetch_dl_lock is None:
                    self._prefetch_dl_lock = asyncio.Lock()
                # Lock statt Task-Reihenfolge: auch ein adoptierter Download
                # (Aufrufer gecancelt, Worker läuft weiter) bleibt so garantiert
                # der einzige aktive yt_dlp-Download (nicht thread-safe).
                async with self._prefetch_dl_lock:
                    if filename.exists():
                        logger.info(f"[Prefetch] Bereits im Cache: {filename.name}")
                        return
                    logger.info(f"[Prefetch] Lade vor: {info.get('title', title)}")
                    await asyncio.to_thread(self.ydl.download, [info.get("webpage_url") or url])
                logger.info(f"[Prefetch] Fertig: {filename.name}")
                await self.cleanup_downloads()
            else:
                logger.info(f"[Prefetch] Bereits im Cache: {filename.name}")
        except asyncio.TimeoutError:
            logger.warning(f"[Prefetch] Timeout für: {title}")
        except Exception as e:
            # Prefetch-Fehler sind nicht fatal – resolve_track lädt im Zweifelsfall selbst.
            logger.warning(f"[Prefetch] Vorladen fehlgeschlagen für: {title}: {e}")
        finally:
            self._inflight_done(url, asyncio.current_task())

    async def prefetch_autoplay(self, ref_url, ref_title, recently_played, recently_played_titles=()):
        """Sucht + lädt nächsten Autoplay-Song im Hintergrund.

        Returns: (url, title) bei Erfolg, None bei Fehler/kein Ergebnis.
        Der Caller entscheidet ob der Song in die Queue kommt (autoplay_enabled-Check).
        """
        yt_id = yt_video_id(ref_url)

        fetch_url = (
            f"https://www.youtube.com/watch?v={yt_id}&list=RD{yt_id}"
            if yt_id
            else (f"ytsearch5:{ref_title}" if ref_title else "ytsearch5:top music")
        )
        logger.info(f"[Autoplay Prefetch] Starte Hintergrundsuche für: {ref_title!r}")

        try:
            info = await self.extract_info_async(fetch_url, "autoplay")
            entries = (info.get("entries") or []) if info else []

            candidates = select_autoplay_candidates(
                entries, ref_url, recently_played, recently_played_titles, ref_title=ref_title
            )
            if not candidates:
                logger.warning("[Autoplay Prefetch] Keine nutzbaren Einträge")
                return None

            chosen = choose_autoplay_candidate(candidates)
            url = entry_url(chosen)
            title = chosen.get("title", "Unbekannt")

            logger.info(f"[Autoplay Prefetch] Lade vor: {title}")
            existing = self._register_inflight(url, "Autoplay-Prefetch", title)
            if existing is not None:
                # Anderer Pfad schreibt diese URL bereits – dessen Ende abwarten
                # statt parallel zu laden; der Song wird trotzdem eingereiht.
                try:
                    await asyncio.wait_for(asyncio.shield(existing[0]), timeout=120.0)
                except Exception:
                    pass
                return url, title
            try:
                # extract_info(download=True) lädt die Datei herunter UND gibt das volle
                # Info-Dict zurück (inkl. ext, webpage_url). Das flache Entry aus autoplay_ydl
                # hat diese Felder nicht – prepare_filename() und info["webpage_url"] in
                # resolve_track() würden sonst fehlschlagen.
                full_info = await asyncio.wait_for(
                    asyncio.to_thread(self.ydl.extract_info, url, download=True),
                    timeout=120.0,
                )
                if full_info:
                    if "entries" in full_info:
                        full_info = full_info["entries"][0]
                    self._url_cache[url] = full_info
                    self._cache_timestamps[url] = time.time()
                    self._cache_dirty = True
                logger.info(f"[Autoplay Prefetch] Fertig: {title}")
                # Der frisch geladene Song hängt noch nicht in der Queue (macht der
                # Caller) → seine Datei explizit vor dem Cleanup schützen.
                just_downloaded = None
                if full_info:
                    try:
                        just_downloaded = Path(self.ydl.prepare_filename(full_info))
                    except Exception:
                        pass
                await self.cleanup_downloads(extra_protected=just_downloaded)
            finally:
                self._inflight_done(url, asyncio.current_task())
            return url, title

        except asyncio.TimeoutError:
            logger.warning("[Autoplay Prefetch] Timeout – play_next fällt auf normales autoplay() zurück")
            return None
        except Exception as e:
            logger.warning(f"[Autoplay Prefetch] Fehler: {e}")
            return None
