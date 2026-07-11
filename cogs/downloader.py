"""yt_dlp-Instanzen, Metadaten-Cache und Download-Logik für den MusicBot."""

import asyncio
import json
import random
import re
import time
from pathlib import Path

import yt_dlp
from config import DOWNLOADS_MAX_MB, YDL_BROWSER, YDL_COOKIES_FILE
from utils.logger import logger
from utils.text import normalize_title  # re-exportiert: music.py importiert es von hier

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

STREAM_THRESHOLD_SECONDS = 20 * 60  # Videos > 20 min werden gestreamt statt heruntergeladen

METADATA_CACHE_FILE = Path("metadata_cache.json")
CACHE_TTL = 7200  # 2 Stunden – CDN-URLs von YouTube laufen danach ab

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

def yt_video_id(url: str) -> str | None:
    """Extrahiert die YouTube-Video-ID aus einer URL (v=..., youtu.be/...). Gibt None zurück wenn keine ID gefunden."""
    m = re.search(r"(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})", url or "")
    return m.group(1) if m else None


class Downloader:
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

    def __init__(self, audio_format: str = "webm"):
        self.audio_format = audio_format
        self._url_cache: dict = {}
        self._cache_timestamps: dict[str, float] = {}
        self._pending_resolves: dict[str, asyncio.Task] = {}
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
        self._cache_timestamps.clear()

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
        info = await asyncio.wait_for(
            asyncio.to_thread(self.ydl.extract_info, url, download=False),
            timeout=30.0,
        )
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
            info = await asyncio.wait_for(
                asyncio.to_thread(self.ydl.extract_info, warmup_url, download=False),
                timeout=25.0,
            )
            if info:
                if "entries" in info:
                    info = info["entries"][0]
                self._url_cache[warmup_url] = info
                self._cache_timestamps[warmup_url] = time.time()
                self._cache_dirty = True
            logger.info("[Warmup] yt_dlp JS-Player-Cache bereit.")
        except Exception as e:
            logger.debug(f"[Warmup] Fehlgeschlagen (ignoriert): {e}")

    def _init_ydl(self):
        if YDL_COOKIES_FILE:
            _cookies = {"cookiefile": YDL_COOKIES_FILE}
        elif YDL_BROWSER:
            _cookies = {"cookiesfrombrowser": (YDL_BROWSER,)}
        else:
            _cookies = {}

        base_opts = {
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
        # Autoplay: nur Metadaten, max. 6 Einträge – kein vollständiger Playlist-Scan.
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
        for tabu in (self.last_resolved_file, extra_protected):
            if tabu is not None:
                try:
                    protected_paths.add(Path(tabu).resolve())
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
            try:
                p.unlink()
            except OSError:
                continue  # z. B. Datei gerade in Benutzung (Windows) → überspringen
            total -= size
            logger.info(f"[Cleanup] Gelöscht ({size / (1024 * 1024):.1f} MB): {p.name}")

    # ------------------------------------------------------------------
    # Download-Logik
    # ------------------------------------------------------------------

    async def resolve_track(self, url: str, title: str, prefetch_task=None):
        """Löst URL auf: extrahiert Metadaten und stellt sicher dass die Audiodatei lokal vorliegt.

        prefetch_task: läuft ggf. parallel – warten statt doppelt herunterladen.
        Returns: (info, filename, title, duration)
        Raises asyncio.TimeoutError wenn extract_info > 30 s dauert.
        """
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

        title = info.get("title", "Unbekannter Titel")
        duration = info.get("duration", 0)

        if duration > STREAM_THRESHOLD_SECONDS:
            # Langer Track → direkt streamen, kein lokaler Download nötig
            audio_url = info.get("url") or url
            logger.info(f"[Stream] {title} ({duration//60} min) wird gestreamt – kein Download")
            self.last_resolved_file = None  # aktive Quelle ist ein Stream, keine Datei
            return info, audio_url, title, duration

        try:
            filename = Path(self.ydl.prepare_filename(info))
            # Sofort als tabu markieren (nicht erst beim Return): der Song ist
            # bereits aus der Queue gepoppt und damit nicht mehr über den
            # protected_provider geschützt – ein parallel fertig werdender
            # Prefetch könnte sonst per Cleanup genau diese Datei löschen.
            self.last_resolved_file = filename
        except Exception as e:
            # Fallback-Garantie: Ein aus der Datei geladener reduzierter (oder
            # sonst kaputter) Cache-Eintrag darf nie zum Fehler beim User werden.
            # Eintrag verwerfen und frisch extrahieren – exakt der Kaltstart-Pfad.
            logger.warning(f"[Cache] Eintrag für {title!r} unbrauchbar ({type(e).__name__}: {e}) – verworfen, extrahiere frisch")
            self.invalidate(url)
            info = await self._fetch_info(url)
            title = info.get("title", "Unbekannter Titel")
            duration = info.get("duration", 0)
            if duration > STREAM_THRESHOLD_SECONDS:
                audio_url = info.get("url") or url
                logger.info(f"[Stream] {title} ({duration//60} min) wird gestreamt – kein Download")
                self.last_resolved_file = None
                return info, audio_url, title, duration
            filename = Path(self.ydl.prepare_filename(info))
            self.last_resolved_file = filename

        if not filename.exists():
            # Wenn der Prefetch-Task diese Datei gerade lädt, warten statt
            # parallel runterzuladen – doppelte Downloads würden die Datei korrumpieren.
            if prefetch_task and not prefetch_task.done():
                logger.info(f"[Download] Warte auf laufenden Prefetch für: {title}")
                try:
                    await prefetch_task
                except Exception:
                    logger.debug("[Prefetch wait] Prefetch fehlgeschlagen, lade selbst herunter.")

            if not filename.exists():  # Prefetch hat es nicht erledigt → sofort als Stream starten
                audio_url = info.get("url") or url
                logger.info(f"[Stream] Nicht gecacht – starte sofort als Stream: {title}")
                self.last_resolved_file = None
                return info, audio_url, title, duration
            else:
                logger.info(f"[Wiedergabe] Prefetch erfolgreich – starte sofort: {filename.name}")
        else:
            logger.info(f"[Wiedergabe] Verwende vorhandene Datei: {filename.name}")

        return info, filename, title, duration

    async def prefetch_next(self, queue, idx: int = 0):
        """Lädt Song an Position idx der Queue still im Hintergrund herunter."""
        if len(queue) <= idx:
            return
        url, title = queue[idx]  # Peek – nicht aus der Queue entfernen
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
            if not filename.exists():
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

    async def prefetch_autoplay(self, ref_url, ref_title, recently_played, recently_played_titles=()):
        """Sucht + lädt nächsten Autoplay-Song im Hintergrund.

        Returns: (url, title) bei Erfolg, None bei Fehler/kein Ergebnis.
        Der Caller entscheidet ob der Song in die Queue kommt (autoplay_enabled-Check).
        """
        yt_id = None
        if ref_url:
            m = re.search(r"(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})", ref_url)
            if m:
                yt_id = m.group(1)

        fetch_url = (
            f"https://www.youtube.com/watch?v={yt_id}&list=RD{yt_id}"
            if yt_id
            else (f"ytsearch5:{ref_title}" if ref_title else "ytsearch5:top music")
        )
        logger.info(f"[Autoplay Prefetch] Starte Hintergrundsuche für: {ref_title!r}")

        try:
            info = await asyncio.wait_for(
                asyncio.to_thread(self.autoplay_ydl.extract_info, fetch_url, download=False),
                timeout=30.0,
            )
            entries = (info.get("entries") or []) if info else []

            def entry_url(e):
                return e.get("webpage_url") or e.get("url") or ""

            def is_video(e):
                if not e or e.get("_type") == "playlist":
                    return False
                u = entry_url(e)
                return bool(u) and "playlist?" not in u and "/playlist/" not in u

            played_ids = {yt_video_id(u) for u in recently_played} - {None}

            def is_seen(e):
                e_url = entry_url(e)
                if e_url in recently_played:
                    return True
                e_id = yt_video_id(e_url)
                if e_id and e_id in played_ids:
                    return True
                e_words = set(normalize_title(e.get("title", "")).split())
                if e_words:
                    return any(e_words.issubset(set(t.split())) for t in recently_played_titles)
                return False

            ref_id = yt_video_id(ref_url)
            candidates = [e for e in entries if is_video(e) and not is_seen(e)]
            if not candidates:
                candidates = [e for e in entries if is_video(e) and (yt_video_id(entry_url(e)) or entry_url(e)) != (ref_id or ref_url)]
            if not candidates:
                candidates = [e for e in entries if is_video(e)]
            if not candidates:
                logger.warning("[Autoplay Prefetch] Keine nutzbaren Einträge")
                return None

            pool = candidates[1:] if len(candidates) > 1 else candidates
            chosen = random.choice(pool)
            url = entry_url(chosen)
            title = chosen.get("title", "Unbekannt")

            logger.info(f"[Autoplay Prefetch] Lade vor: {title}")
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
            return url, title

        except asyncio.TimeoutError:
            logger.warning("[Autoplay Prefetch] Timeout – play_next fällt auf normales autoplay() zurück")
            return None
        except Exception as e:
            logger.warning(f"[Autoplay Prefetch] Fehler: {e}")
            return None
