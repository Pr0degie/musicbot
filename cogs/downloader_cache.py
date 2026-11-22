"""Metadaten-Cache – Mixin für Downloader.

In-Memory-Cache der yt_dlp-Info-Dicts samt Timestamps (CACHE_TTL), tolerantes
Laden und reduziertes, gedebounctes Persistieren nach metadata_cache.json
(ADR 0005) sowie die Extraktion, die den Cache füllt (_fetch_info,
_start_resolve, warmup). Der Zustand (_url_cache, _cache_timestamps,
_pending_resolves) wird in Downloader.__init__ angelegt.
"""
import asyncio
import json
import time
from pathlib import Path

from utils.files import atomic_write_text
from utils.logger import logger
from cogs.downloader_progressive import STREAM_THRESHOLD_SECONDS

METADATA_CACHE_FILE = Path("metadata_cache.json")
CACHE_TTL = 7200  # 2 Stunden – CDN-URLs von YouTube laufen danach ab

# Nur diese Felder werden nach metadata_cache.json persistiert – ermittelt per
# grep über alle info.get()/info[...]-Zugriffe plus prepare_filename-Bedarf
# (outtmpl "%(title)s [%(id)s].%(ext)s"). Volle yt_dlp-Info-Dicts haben ~90 Keys und
# machen die Datei um Größenordnungen fetter. In-Memory bleibt immer das
# volle Dict; ein aus der Datei geladener reduzierter Eintrag, der beim
# Verwenden doch scheitert, wird verworfen und wie ein Cache-Miss behandelt
# (siehe prepare_filename-Fallback in resolve_track/prefetch_next).
#   id           prepare_filename (Video-ID im Dateinamen: gleicher Titel ≠ gleiche Datei)
#   title        Anzeige + prepare_filename
#   ext          prepare_filename
#   duration     Stream-Schwelle, Embed, Fortschrittsbalken
#   url          direkte CDN-Stream-URL (Stream-Pfad)
#   webpage_url  Embed-Link, Download-Ziel in prefetch_next
#   thumbnail    Embed-Thumbnail
#   uploader     Embed-Titel/-Footer
#   http_headers FFmpeg -headers im Stream-Pfad (music._ffmpeg_header_opts)
PERSISTED_CACHE_FIELDS = (
    "id", "title", "ext", "duration", "url", "webpage_url",
    "thumbnail", "uploader", "http_headers",
)


class MetadataCacheMixin:
    # Klassen-Default, damit auch ohne __init__ erzeugte Instanzen (Tests via
    # __new__) einen definierten Zustand haben. _cache_dirty: gesetzt bei jeder
    # Cache-Änderung, geleert vom Debounce-Flush (music._persist_flush_loop)
    # bzw. flush_cache_now.
    _cache_dirty = False

    def clear_cache(self):
        self._url_cache.clear()

    def invalidate(self, url: str):
        """Wirft den Cache-Eintrag einer URL weg (z.B. nach einem 403 vom CDN),
        damit der nächste resolve_track eine frische Stream-URL extrahiert."""
        self._url_cache.pop(url, None)
        self._cache_timestamps.pop(url, None)

    def _prune_cache(self, keep_urls: set | None = None):
        """Dünnt den Cache auf keep_urls aus (None = komplett leeren) und
        vergisst die wartenden Resolves. Nur auf dem Event-Loop aufrufen."""
        if keep_urls is not None:
            self._url_cache = {k: v for k, v in self._url_cache.items() if k in keep_urls}
            self._cache_timestamps = {k: v for k, v in self._cache_timestamps.items() if k in keep_urls}
        else:
            self._url_cache.clear()
            self._cache_timestamps.clear()
        self._pending_resolves.clear()

    def _usable_cached_info(self, url: str):
        """Cache-Eintrag für url, sofern er noch verwendbar ist – sonst None
        (der Aufrufer behandelt das als Cache-Miss und extrahiert frisch).

        Verwendbar heißt: innerhalb von CACHE_TTL (danach sind die CDN-URLs im
        Eintrag abgelaufen) – oder abgelaufen, aber die lokale Datei liegt schon
        vollständig vor: dann braucht niemand mehr eine CDN-URL, die Datei
        spielt ohne neue Extraktion. Einträge ohne Timestamp gelten wie beim
        Speichern (_serialize_cache) als frisch."""
        info = self._url_cache.get(url)
        if info is None:
            return None
        ts = self._cache_timestamps.get(url)
        if ts is None or time.time() - ts < CACHE_TTL:
            return info
        if (info.get("duration") or 0) <= STREAM_THRESHOLD_SECONDS:
            try:
                filename = Path(self.ydl.prepare_filename(info))
                if filename.exists() and not self.is_incomplete(filename):
                    return info
            except Exception:
                pass   # unbrauchbarer Eintrag → wie abgelaufen behandeln
        return None

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

    def _drop_cache_entries_without_id(self):
        """Nur beim Start: Einträge aus einer metadata_cache.json, die vor der
        Video-ID im Dateinamen geschrieben wurde, haben kein "id". Mit ihnen
        ergäbe prepare_filename "<Titel> [NA].<ext>" – ein Pfad, in den nie
        ein Download schreibt. Verwerfen → Cache-Miss, frische Extraktion.
        Zur Laufzeit kommen nur volle Info-Dicts (immer mit id) in den Cache."""
        stale = [url for url, info in self._url_cache.items() if not info.get("id")]
        for url in stale:
            self.invalidate(url)
        if stale:
            logger.info(f"[Cache] {len(stale)} Einträge ohne Video-ID verworfen (altes Dateinamen-Schema)")

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

    def _snapshot_cache(self) -> str | None:
        """_serialize_cache, das nie wirft: None, wenn der Snapshot scheitert
        (dann wird nichts geschrieben). Wie _serialize_cache auf dem Event-Loop."""
        try:
            return self._serialize_cache()
        except Exception as e:
            logger.debug(f"[Cache] Speichern fehlgeschlagen: {e}")
            return None

    @staticmethod
    def _write_cache(payload: str | None):
        """Schreibt einen Snapshot aus _snapshot_cache auf Disk. Fasst self
        nicht an und darf deshalb im Worker-Thread laufen."""
        if payload is None:
            return
        try:
            atomic_write_text(METADATA_CACHE_FILE, payload)
        except Exception as e:
            logger.debug(f"[Cache] Speichern fehlgeschlagen: {e}")

    def _save_cache(self):
        """Schreibt aktuellen Metadaten-Cache synchron auf Disk (reduziert)."""
        self._write_cache(self._snapshot_cache())

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
            await asyncio.to_thread(atomic_write_text, METADATA_CACHE_FILE, payload)
        except Exception as e:
            self._cache_dirty = True
            logger.debug(f"[Cache] Speichern fehlgeschlagen: {e}")

    def _store_info(self, url: str, info: dict) -> dict:
        """Legt ein frisch extrahiertes Info-Dict im Cache ab: entpackt den
        Playlist-Wrapper, cached das volle Dict in-memory und markiert den
        Cache als dirty (persistiert wird reduziert und gedebounct). Gibt das
        abgelegte Dict zurück."""
        if "entries" in info:
            info = info["entries"][0]
        self._url_cache[url] = info
        self._cache_timestamps[url] = time.time()
        self._cache_dirty = True
        return info

    async def _fetch_info(self, url: str) -> dict:
        """extract_info im Worker-Thread, Ergebnis landet über _store_info im Cache."""
        return self._store_info(url, await self.extract_info_async(url, "main"))

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
                self._store_info(warmup_url, info)
            logger.info("[Warmup] yt_dlp JS-Player-Cache bereit.")
        except Exception as e:
            logger.debug(f"[Warmup] Fehlgeschlagen (ignoriert): {e}")
