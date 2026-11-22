"""yt_dlp-Instanzen, Cookie-Modus und Extraktion – Mixin für Downloader.

Instanzen entstehen nur über _build_ydl() (setzt den schnellen Client,
ADR 0013), extrahiert wird nur über extract_info_async() (cookielos zuerst,
ADR 0010). rebuild()/rebuild_async() bauen alle Instanzen neu auf.
"""
import asyncio
from pathlib import Path

import yt_dlp
from config import YDL_BROWSER, YDL_COOKIES_FILE
from utils.logger import logger

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

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

# Schneller YouTube-Client für den cookielosen Modus. Mit Node-Runtime nimmt
# yt_dlp sonst tv_samsung/visionos/web und wählt die TV-URL: deren
# available_at sperrt den Abruf ~4–5 s (yt_dlp schläft so lange vor dem
# Download, FFmpeg bekommt 403). visionos liefert dieselbe Opus-Spur (itag 251)
# ohne Sperrfrist und extrahiert schneller (gemessen 22.11.2026: 1,3 s statt
# 2,1 s + 4,3 s Wartezeit). yt_dlps eigener Default ohne JS-Runtime ist
# ebenfalls nur visionos. Im Cookie-Modus bleiben yt_dlps authed-Defaults.
FAST_PLAYER_CLIENTS = ("visionos",)

# Fehler, nach denen sich ein Zweitversuch mit den Standard-Clients lohnt:
# der schnelle Client lieferte für dieses Video nichts Brauchbares.
CLIENT_FALLBACK_MARKERS = (
    "requested format is not available",
    "no video formats found",
    "only images are available",
    "po token",
    "http error 403",
    "this video is not available",
)


class YdlMixin:
    # Klassen-Default für ohne __init__ erzeugte Instanzen (Tests via __new__).
    audio_format = "webm"

    def _target_path(self, info: dict) -> Path:
        """Lokale Datei, unter der dieser Track nach dem Download liegt.

        Einzige Stelle, die prepare_filename auswertet. prepare_filename nennt
        die Endung der Quelle (z. B. .webm); im mp3-Modus ersetzt der
        FFmpeg-Postprocessor die geladene Datei aber durch <name>.mp3."""
        path = Path(self.ydl.prepare_filename(info))
        if self.audio_format == "mp3":
            path = path.with_suffix(".mp3")
        return path

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
        self._prune_cache()
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
        def attempt(ydl=None):
            # Ohne ydl: die Instanz der Art, erst jetzt aufgelöst (siehe _YDL_KINDS).
            if ydl is None:
                ydl = getattr(self, self._YDL_KINDS[kind])
            return asyncio.wait_for(
                asyncio.to_thread(ydl.extract_info, query, download=False), timeout=timeout
            )

        try:
            return await attempt()
        except asyncio.TimeoutError:
            raise
        except Exception as e:
            if self._needs_cookies(e) and self.enable_cookie_mode(str(e)[:120]):
                logger.info(f"[Cookies] Zweitversuch mit Cookies: {query[:80]}")
            elif self._wants_client_fallback(e, kind):
                # Schneller Client kann dieses Video nicht → einmal mit den
                # yt_dlp-Standard-Clients (frische Wegwerf-Instanz, threadsicher).
                logger.info(f"[yt-dlp] {'/'.join(FAST_PLAYER_CLIENTS)} scheiterte ({str(e)[:80]}) – "
                            f"Zweitversuch mit Standard-Clients: {query[:80]}")
                return await attempt(self._build_ydl(kind, fast=False))
            else:
                raise
        return await attempt()

    def _player_opts(self, fast: bool = True) -> dict:
        """extractor_args für den schnellen YouTube-Client (FAST_PLAYER_CLIENTS).
        Leer im Cookie-Modus oder wenn fast=False (Rückfall auf yt_dlp-Defaults)."""
        if not fast or self._cookie_mode:
            return {}
        return {"extractor_args": {"youtube": {"player_client": list(FAST_PLAYER_CLIENTS)}}}

    @staticmethod
    def _needs_client_fallback(exc: BaseException) -> bool:
        msg = str(exc).lower()
        return any(marker in msg for marker in CLIENT_FALLBACK_MARKERS)

    def _wants_client_fallback(self, exc: BaseException, kind: str) -> bool:
        """Lohnt ein Zweitversuch mit den yt_dlp-Standard-Clients? Nur wenn der
        schnelle Client aktiv ist, die Art ihn überhaupt benutzt (search und
        autoplay sind flach) und der Fehler danach aussieht."""
        return bool(self._player_opts()) and kind in ("main", "url", "playlist") and self._needs_client_fallback(exc)

    def _quiet_opts(self) -> dict:
        """Gemeinsamer Sockel aller yt_dlp-Instanzen."""
        return {
            "quiet": True,
            "no_warnings": True,
            # Netzwerk-Timeout direkt in yt_dlp – bricht den Thread intern ab,
            # sodass asyncio.wait_for nicht auf den Thread warten muss.
            "socket_timeout": 15,
            # Node.js als JS-Runtime für den EJS-Signature-Solver aktivieren.
            "js_runtimes": {"node": {}},
            **self._cookie_opts(),
        }

    def _build_base_opts(self, fast: bool = True) -> dict:
        return {
            **self._quiet_opts(),
            # Bevorzugt Opus/webm mit mindestens 160kbps (YouTube's höchste Audio-Tier),
            # fällt auf 128kbps, dann beliebiges webm, dann Opus, dann best zurück.
            # prefer_free_formats bevorzugt Opus über AAC bei gleichwertiger Qualität.
            "format": "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best",
            "prefer_free_formats": True,
            "default_search": "ytsearch",
            "noplaylist": False,
            # prepare_filename() gibt später den exakt gleichen Pfad zurück den
            # yt_dlp beim Speichern verwendet – inklusive Sonderzeichen-Bereinigung.
            # Die Video-ID gehört in den Namen: zwei verschiedene Videos mit
            # gleichem Titel teilten sich sonst eine Cache-Datei.
            "outtmpl": str(DOWNLOAD_DIR / "%(title)s [%(id)s].%(ext)s"),
            **self._player_opts(fast),
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

    def _build_ydl(self, kind: str, fast: bool = True) -> "yt_dlp.YoutubeDL":
        """yt_dlp-Instanz für einen Abfrage-Typ aus _YDL_KINDS bauen.

        fast=False baut sie ohne den schnellen Client – für den einmaligen
        Rückfall in extract_info_async. search/autoplay sind flach (keine
        Player-Abfrage), dort ändert fast nichts."""
        if kind == "main":
            base_opts = self._build_base_opts(fast)
            if self.audio_format == "mp3":
                # MP3-Konvertierung läuft über FFmpeg als Post-Processing-Schritt.
                base_opts["postprocessors"] = [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ]
            return yt_dlp.YoutubeDL(base_opts)
        quiet = self._quiet_opts()
        # Flach, nur Metadaten: Suche und Autoplay.
        flat = {**quiet, "extract_flat": True, "skip_download": True}
        if kind == "search":
            return yt_dlp.YoutubeDL(flat)
        if kind == "autoplay":
            # Max. 10 Einträge – kein vollständiger Playlist-Scan.
            return yt_dlp.YoutubeDL({**flat, "noplaylist": False, "playlistend": 10})
        # Mit Player-Abfrage: einzelne URL und Playlist.
        by_url = {**quiet, "format": "bestaudio/best", "default_search": "ytsearch", **self._player_opts(fast)}
        if kind == "url":
            return yt_dlp.YoutubeDL({**by_url, "noplaylist": True, "extract_flat": False})
        if kind == "playlist":
            return yt_dlp.YoutubeDL({**by_url, "noplaylist": False, "extract_flat": "in_playlist"})
        raise ValueError(f"Unbekannter yt_dlp-Typ: {kind}")

    def _init_ydl(self):
        # Gecachte Instanzen je Abfrage-Typ (Suche, URL, Playlist, Autoplay, Haupt).
        for kind, attr in self._YDL_KINDS.items():
            setattr(self, attr, self._build_ydl(kind))

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
        self._prune_cache(keep_urls)
        self._init_ydl()

    async def rebuild_async(self, audio_format: str, keep_urls: set | None = None):
        """Wie rebuild(), aber für den laufenden Betrieb (Wartung alle 50 Songs).

        Nur das Bauen der yt_dlp-Instanzen und der Datei-Write laufen im
        Worker-Thread. Jede Zustandsänderung – Format, _url_cache,
        _cache_timestamps, _pending_resolves und der Tausch der Instanzen –
        passiert auf dem Event-Loop, der diese Strukturen gleichzeitig benutzt.
        Muss auf dem Event-Loop aufgerufen werden.
        """
        # --- Event-Loop: Snapshot für die Datei, dann Zustand umstellen ---
        payload = self._snapshot_cache()
        self.audio_format = audio_format
        self._prune_cache(keep_urls)
        replaced = self.ydl

        # --- Worker-Thread: schreiben und bauen, ohne self zu verändern ---
        def _write_and_build():
            self._write_cache(payload)
            return {attr: self._build_ydl(kind) for kind, attr in self._YDL_KINDS.items()}

        built = await asyncio.to_thread(_write_and_build)

        # --- Event-Loop: Instanzen tauschen ---
        if self.ydl is not replaced:
            # Während des Bauens hat ein anderer Pfad die Instanzen bereits neu
            # aufgebaut (!format, !reloadcookies, Cookie-Fallback) – dessen
            # Stand ist der neuere, die hier gebauten wären veraltet.
            return
        for attr, instance in built.items():
            setattr(self, attr, instance)
