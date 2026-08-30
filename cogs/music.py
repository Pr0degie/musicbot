# Hier lebt die gesamte Musik-Logik: Queue, Downloads, Wiedergabe, EQ und Autoplay.
# Kurz gesagt: die wichtigste Datei im ganzen Bot. Treat her well.

import asyncio
import json
import random
import re
import time
import urllib.parse
from collections import deque
from pathlib import Path

import aiohttp

import psutil
from utils.logger import logger
from utils.voice import stop_playback
from utils.i18n import t
from utils.url_check import enforce_url_policy
from utils.checks import require_same_voice, require_admin
from utils.ffmpeg import ffmpeg_header_opts, stderr_tail, classify_ffmpeg_error
from utils.text import parse_time, progress_bar
from discord.ext import commands, tasks
from cogs.downloader import (
    Downloader, choose_autoplay_candidate, entry_url,
    select_autoplay_candidates, yt_video_id,
)
from cogs.presets import EQ_PRESETS
from cogs.music_radio import RadioMixin
from cogs.music_stats import StatsMixin
from cogs.music_queue_io import QueuePersistenceMixin
from cogs.music_voice_ui import VoiceLifecycleMixin, PlaybackUiMixin
from cogs.music_playback import PlaybackMixin, _YTDLP_FAILED

SCORE_FILE = Path("play_counts.json")


class MusicCommands(RadioMixin, StatsMixin, QueuePersistenceMixin,
                    VoiceLifecycleMixin, PlaybackUiMixin, PlaybackMixin,
                    commands.Cog):
    """Cog für alle Musikbefehle: Wiedergabe, Queue, EQ, Autoplay.

    Radio-, Statistik- und Queue-Persistenz-Befehle sind in Mixins ausgelagert
    (cogs/music_radio.py, music_stats.py, music_queue_io.py) – sie teilen sich
    denselben Instanz-State und werden über die MRO als Cog-Commands registriert.
    """

    # Maximale Anzahl an Titeln die aus einer Playlist eingelesen werden.
    HARD_PLAYLIST_LIMIT = 150
    # Sekunden ohne User im Channel bevor der Bot den Channel verlässt.
    AUTO_LEAVE_SECONDS = 300
    # Sekunden ohne Wiedergabe (Queue leer, kein Autoplay) bevor der Bot den
    # Channel verlässt. 2 Stunden – der Bot bleibt lange verfügbar und räumt
    # sich erst danach selbst auf.
    IDLE_LEAVE_SECONDS = 7200
    # Klassen-Defaults, damit auch ohne __init__ erzeugte Instanzen (Tests via
    # __new__) einen definierten Zustand haben. _score_dirty: gesetzt von
    # _record_play, geleert von _persist_flush_loop/_flush_scores_now.
    _score_dirty = False
    # Eine wiederverwendete aiohttp-Session für alle HTTP-Aufrufe des Cogs
    # (lyrics.ovh) – erstellt in cog_load, geschlossen in cog_unload.
    _http_session = None

    # Diese zustandslosen Helfer leben jetzt in utils/ (ffmpeg.py, text.py).
    # Die Klassen-Aliase bleiben, weil Tests und interne Aufrufer sie über die
    # Klasse ansprechen (MusicCommands._stderr_tail, self._parse_time, …) bzw.
    # dort patchen – die Aliase halten diese Zugriffs-/Patch-Punkte stabil.
    _ffmpeg_header_opts = staticmethod(ffmpeg_header_opts)
    _stderr_tail = staticmethod(stderr_tail)
    _classify_ffmpeg_error = staticmethod(classify_ffmpeg_error)
    _parse_time = staticmethod(parse_time)
    _progress_bar = staticmethod(progress_bar)

    def __init__(self, bot):
        self.bot = bot

        # deque statt list, weil popleft() in O(1) läuft – bei langen Queues
        # ist das deutlich schneller als list.pop(0).
        self.queue = deque()

        self.is_playing = False
        self.current_track = None   # Aktuell spielender Song (url, title, duration) – für !now
        self.last_played = None     # Wird von !replay genutzt
        self.prefetch_task = None         # Läuft im Hintergrund während ein Song spielt
        self._autoplay_prefetch_task = None  # Sucht+lädt nächsten Autoplay-Song vor
        self._autoplay_queued_url = None     # URL die zuletzt von Autoplay in die Queue gelegt wurde
        self._recently_played: deque = deque(maxlen=15)       # URLs der zuletzt gespielten Songs
        self._recently_played_titles: deque = deque(maxlen=15)  # normalisierte Titel (Subset-Duplikat-Check)
        self.auto_leave_task = None # Timer: verlässt Channel wenn alle User weg sind
        self.idle_leave_task = None # Timer: verlässt Channel nach langer Stille (gegen 1006)
        self._last_ctx = None       # Letzter Wiedergabe-Kontext – für Reconnect-Watchdog
        self._stuck_ticks = 0       # Aufeinanderfolgende Watchdog-Ticks im Hänge-Zustand
        self._stopped_by_user = False  # True nach !stop/!x – unterdrückt den Watchdog-Restart
        self.text_channel = None    # Letzter Textkanal – für Auto-Leave-Nachricht
        self.now_playing_msg = None # Aktuelle "Jetzt läuft"-Nachricht – für Button-Cleanup
        self.now_playing_embed = None  # Embed-Referenz für Live-Edit des Fortschrittsbalkens
        self._np_title = None       # Titel/Sender der aktuellen Karte – für ihren Rückbau zur Textzeile
        self.track_start_time = None  # Zeitstempel kurz vor play() – FFmpeg-Crash-Erkennung
        self._np_paused_total = 0.0   # aufsummierte Pausensekunden des aktuellen Songs
        self._np_paused_at = None     # monotonic-Zeitstempel seit Pause-Beginn (None = läuft)
        self._np_last_desc = None     # zuletzt gesetzte Balken-Zeile – spart redundante Edits
        self._skip_resolving = False  # Gesetzt von SearchAutoplayView wenn Alternative gewählt wird während resolve läuft
        # Generationszähler gegen Race-Zuweisungen: Stirbt ein Track schneller als die
        # awaits im Post-Play-Teil von play_next, läuft der Leere-Queue-Cleanup zuerst –
        # späte Zuweisungen des toten Durchlaufs würden ihn sonst überschreiben (der
        # _progress_loop editiert dann endlos eine tote Nachricht → Discord-429).
        self._track_generation = 0
        # (msg, title) der letzten Now-Playing-Nachricht nach Queue-Ende: Referenzen für
        # den _progress_loop werden im Cleanup gekappt, aber die Buttons der Nachricht
        # sollen erst beim nächsten Track entfernt werden (Resume/Autoplay bleiben nutzbar).
        self._ended_np = None
        # URL, für die gerade ein Stream-Retry läuft (Sofort-Tod + Input-Fehler →
        # genau EIN Neuversuch mit frischer URL). Verhindert Retry-Schleifen und
        # doppelte Play-Counts; wird bei normalem Track-Ende wieder gelöscht.
        self._stream_retry_url = None
        # URL, die nach zwei toten Stream-Versuchen als letzter Anlauf lokal
        # heruntergeladen wird (yt_dlp-eigener HTTP-Client statt FFmpeg – von
        # CDN-403s gegen FFmpeg meist nicht betroffen). Lebenszyklus wie
        # _stream_retry_url; beide zusammen begrenzen einen Track auf 3 Versuche.
        self._force_download_url = None
        # Progressiver Download: Endet die wachsende Datei vorzeitig (FFmpeg hat
        # den Download überholt), wird der Track mit -ss wieder vorn eingereiht.
        # Mechanik wie _stream_retry_url (kein doppelter Play-Count); die Kappe
        # verhindert Endlos-Resumes bei stotterndem Download.
        self._progressive_resume_url = None
        self._progressive_resume_count = 0
        # One-Shot-Flag: absichtlicher Stopp (Skip, !now, !eq, !seek, Auto-Leave …)
        # → after_playing darf die wachsende Datei NICHT wieder einreihen.
        # Gesetzt via _stop_for_advance(), konsumiert (und gelöscht) in after_playing.
        self._suppress_resume = False

        # Standard-EQ und -Format beim Start
        self.equalizer = "punchy"
        self.audio_format = "webm"

        # Loop-Modi: None = aus, "song" = aktuellen Song wiederholen, "queue" = ganze Queue loopen
        self.loop_mode = None

        # Zählt gespielte Songs – alle 50 Songs werden yt_dlp-Instanzen neu erstellt,
        # damit deren interne Caches (Signatur-Parser, Format-Metadaten) nicht unbegrenzt wachsen.
        self._songs_played = 0

        self.eq_presets = EQ_PRESETS

        # Autoplay ist standardmäßig aus – niemand will, dass der Bot
        # nach Mitternacht eigenständig Jazz spielt.
        self.autoplay_enabled = False

        # Radio-Modus
        self.is_radio = False
        self.radio_station_name = None
        self.radio_stream_url = None
        self._radio_reconnect_count = 0

        # Event wird gesetzt wenn FFmpeg stoppt – ersetzt Sleep-Loop-Polling.
        self._playback_done = asyncio.Event()
        self._playback_done.set()

        # Seek-Offset in Sekunden für den nächsten play_next()-Aufruf.
        self._seek_offset: int = 0

        self._start_time = time.monotonic()
        self._process = psutil.Process()

        try:
            with open(SCORE_FILE, encoding="utf-8") as f:
                self._play_counts: dict = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self._play_counts: dict = {}

        # Downloader hält alle yt_dlp-Instanzen und den Metadaten-Cache.
        self.dl = Downloader(self.audio_format)
        # Downloads-Cleanup (DOWNLOADS_MAX_MB): welche Songs dürfen NIE
        # gelöscht werden – Queue + current_track, geliefert bei jedem Lauf.
        self.dl.protected_provider = self._protected_tracks
        logger.info("[INIT] MusicCommands erfolgreich initialisiert.")

    def _protected_tracks(self):
        """(urls, titles) aller Songs, deren Downloads der Cleanup nicht anfassen darf."""
        urls = [url for url, _ in self.queue]
        titles = [title for _, title in self.queue]
        if self.current_track:
            url, title, *_ = self.current_track
            urls.append(url)
            titles.append(title)
        return urls, titles

    async def cog_load(self):
        asyncio.create_task(self.dl.warmup())
        self._voice_watchdog.start()
        self._progress_loop.start()
        self._persist_flush_loop.start()
        self._http_session = aiohttp.ClientSession()

    async def cog_unload(self):
        self._voice_watchdog.cancel()
        self._progress_loop.cancel()
        self._persist_flush_loop.cancel()
        if self.idle_leave_task and not self.idle_leave_task.done():
            self.idle_leave_task.cancel()
        # Gedebouncte Writes dürfen beim Entladen nicht verloren gehen.
        self._flush_scores_now()
        self.dl.flush_cache_now()
        if self._http_session is not None and not self._http_session.closed:
            await self._http_session.close()

    def _http(self) -> aiohttp.ClientSession:
        """Die geteilte HTTP-Session des Cogs. Ist sie unerwartet geschlossen
        (oder cog_load lief nie, z. B. in Tests), wird eine neue erstellt statt
        einen Fehler zu werfen."""
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    def update_ydl(self):
        """Baut yt_dlp-Instanzen neu auf. Cache-Einträge für Queue-Songs bleiben erhalten."""
        keep = {url for url, _ in self.queue}
        if self.current_track:
            keep.add(self.current_track[0])
        self.dl.rebuild(self.audio_format, keep_urls=keep)

    def _record_play(self, url: str, title: str):
        entry = self._play_counts.get(url)
        if entry:
            entry["count"] += 1
            entry["title"] = title
        else:
            self._play_counts[url] = {"title": title, "count": 1}
        # Kein sofortiger Disk-Write: _persist_flush_loop schreibt spätestens
        # alle 30 s im Worker-Thread. Bewusster Trade-off für den Dauerbetrieb
        # auf schwacher Hardware: bei einem harten Crash können bis zu 30 s
        # Play-Counts verloren gehen – akzeptiert. cog_unload und !restart
        # flushen zusätzlich sofort (_flush_scores_now).
        self._score_dirty = True

    def _flush_scores_now(self):
        """Synchroner Score-Flush für Shutdown-Pfade (cog_unload, !restart)."""
        if not self._score_dirty:
            return
        self._score_dirty = False
        try:
            SCORE_FILE.write_text(
                json.dumps(self._play_counts, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            self._score_dirty = True
            logger.warning(f"[Score] Fehler beim Speichern: {e}")

    @tasks.loop(seconds=30)
    async def _persist_flush_loop(self):
        """Schreibt geänderte Play-Counts und den Metadaten-Cache gebündelt
        (Debounce) im Worker-Thread."""
        if self._score_dirty:
            self._score_dirty = False
            # Snapshot per json.dumps auf dem Event-Loop, damit der Worker-Thread
            # nie ein Dict serialisiert, das gleichzeitig mutiert wird.
            payload = json.dumps(self._play_counts, ensure_ascii=False, indent=2)
            try:
                await asyncio.to_thread(SCORE_FILE.write_text, payload, encoding="utf-8")
            except Exception as e:
                self._score_dirty = True
                logger.warning(f"[Score] Fehler beim Speichern: {e}")
        await self.dl.flush_cache()

    @commands.command(name="reloadcookies")
    @require_admin()
    async def reloadcookies(self, ctx):
        """Lädt die cookies.txt neu ohne Bot-Neustart (nach manuellem Upload auf den Server)."""
        # Expliziter Wunsch → Cookie-Modus an, auch wenn cookielos gerade läuft (ADR 0010).
        self.dl.enable_cookie_mode("!reloadcookies")
        self.update_ydl()
        from config import YDL_COOKIES_FILE, YDL_BROWSER
        if YDL_COOKIES_FILE:
            source = f"`{YDL_COOKIES_FILE}`"
        elif YDL_BROWSER:
            source = f"Browser ({YDL_BROWSER})"
        else:
            source = "keine Cookie-Quelle konfiguriert"
        await ctx.send(t("misc.cookies_reloaded", source=source))

    @commands.command(usage="!format <mp3|webm>")
    @require_admin()
    async def format(self, ctx, typ: str):
        """Wechselt das Audioformat (mp3 oder webm). Wirkt ab dem nächsten Track."""
        if typ.lower() in ["mp3", "webm"]:
            self.audio_format = typ.lower()
            self.update_ydl()
            # Kleiner Hinweis, falls gerade etwas läuft – der aktuelle Track
            # wird nicht neu gestartet, das wäre nervig.
            note = t("status.format_note") if self.is_playing else ""
            await ctx.send(t("status.format_set", format=self.audio_format, note=note))
        else:
            await ctx.send(t("error.invalid_format"))

    async def autoplay(self, ctx):
        """Sucht einen zum letzten Song passenden Track und spielt ihn einmalig.

        Nutzt den Titel des zuletzt gespielten Songs als Suchbasis, damit das
        Ergebnis thematisch passt. Autoplay deaktiviert sich danach selbst –
        für dauerhafte Wiederholung gibt es !loop.
        """
        # Referenz-Track: aktueller Song wenn vorhanden, sonst letzter gespielter
        ref_url = None
        ref_title = None
        if self.current_track:
            ref_url, ref_title, *_ = self.current_track
        elif self.last_played:
            ref_url, ref_title, *_ = self.last_played

        # YouTube Mix/Radio-URL: gibt echte Empfehlungen basierend auf dem Video
        yt_id = yt_video_id(ref_url)

        if yt_id:
            # YouTube-eigene Empfehlungen via RD-Mix-Playlist
            fetch_url = f"https://www.youtube.com/watch?v={yt_id}&list=RD{yt_id}"
            logger.info(f"[Autoplay] Lade YouTube-Mix für Video {yt_id} (Basis: {ref_title!r})")
        else:
            # Fallback für Nicht-YouTube-Quellen: titelbasierte Suche
            fetch_url = f"ytsearch5:{ref_title}" if ref_title else "ytsearch5:top music"
            logger.info(f"[Autoplay] Kein YT-Video-ID – Suche per Query: {fetch_url!r}")

        try:
            info = await self.dl.extract_info_async(fetch_url, "autoplay")
            entries = (info.get("entries") or []) if info else []

            candidates = select_autoplay_candidates(
                entries, ref_url, self._recently_played, self._recently_played_titles,
                ref_title=ref_title,
            )

            if not candidates:
                await ctx.send(t("error.autoplay_no_results"))
                logger.warning("[Autoplay] Keine nutzbaren Einträge im Mix")
                return

            chosen = choose_autoplay_candidate(candidates)
            url = entry_url(chosen)
            title = chosen.get("title", t("misc.unknown"))

            self.queue.appendleft((url, title))
            self._autoplay_queued_url = url
            logger.info(f"[Autoplay] Hinzugefügt: {title} ({url})")
            await ctx.send(t("status.autoplay_added", title=title), delete_after=20)
            if not self.is_playing:
                self.is_playing = True
                await self.play_next(ctx)
        except asyncio.TimeoutError:
            await ctx.send(t("error.autoplay_timeout"))
            logger.warning("[Autoplay] Timeout bei extract_info")
        except Exception:
            await ctx.send(t("error.autoplay_error"))
            logger.exception("[Autoplay Fehler]")

    async def _ensure_voice(self, ctx) -> bool:
        """Stellt sicher, dass der Bot im Voice-Channel des Users ist.

        Verbindet automatisch wenn nötig. Gibt True zurück wenn verbunden,
        False wenn der User selbst in keinem Channel ist (mit Fehlermeldung).
        """
        if ctx.voice_client is not None:
            return True
        if not ctx.author.voice:
            await ctx.send(t("error.no_voice"))
            return False
        try:
            await ctx.author.voice.channel.connect()
            logger.info(f"[Auto-Join] Verbunden mit: {ctx.author.voice.channel.name}")
        except Exception as e:
            await ctx.send(t("error.connect_failed", err=f"{type(e).__name__}: {str(e)[:100]}"))
            return False
        return True

    @commands.command()
    async def p(self, ctx, *, eingabe: str = None):
        """Spielt eine URL, Playlist oder Suchbegriff. Bei Suche werden 3 Treffer zur Auswahl angezeigt."""
        if not eingabe:
            await ctx.send(t("error.p_usage"))
            return
        logger.info(f"[p] Eingabe erhalten: {eingabe}")

        if not await self._ensure_voice(ctx):
            return

        await self._stop_radio_for_takeover(ctx)

        # Wenn kein http am Anfang → Suchbegriff → ersten Treffer sofort abspielen,
        # Treffer 2 und 3 als Buttons anzeigen falls es der Falsche war.
        # Insert-Semantik ("evict_or_back"): siehe _search_and_enqueue-Docstring.
        if not eingabe.startswith("http"):
            queued = await self._search_and_enqueue(
                ctx, eingabe, log_tag="p", timeout_key="error.search_timeout",
                insert="evict_or_back", with_alts_key="status.playing_with_alts",
                no_alts_key="status.added",
            )
            if not queued:
                return
            if not self.is_playing:
                self.is_playing = True
                await self.play_next(ctx)
            else:
                self._kick_prefetch()   # läuft schon Musik → neuen Titel sofort vorladen
            return

        # --- Ab hier: direkte URL oder Playlist ---

        # Einfache Heuristik: Wenn "playlist?" oder "list=" in der URL steht,
        # ist es eine Playlist. Funktioniert für alle gängigen YouTube-Playlist-URLs.
        is_playlist = "playlist?" in eingabe or "list=" in eingabe

        info = await self._extract_info_or_report(
            ctx, eingabe, "playlist" if is_playlist else "url",
            status_key="status.processing", timeout_key="error.processing_timeout",
            error_key="error.url_error",
            log_msg="[p] Fehler beim Abrufen von yt_dlp-Infos",
        )
        if info is _YTDLP_FAILED:
            return

        if "entries" in info:
            entries = info["entries"]
            await ctx.send(t("status.playlist_detected", title=info.get("title", t("misc.unnamed_playlist")), count=len(entries)))
            added_count = 0
            for entry in entries:
                if added_count >= self.HARD_PLAYLIST_LIMIT:
                    await ctx.send(t("error.playlist_limit", limit=self.HARD_PLAYLIST_LIMIT))
                    break
                # webpage_url ist immer die echte YouTube-URL.
                # url kann bei Suchergebnissen eine direkte Stream-URL sein → zuletzt prüfen.
                url = entry.get("webpage_url") or entry.get("url")
                title = entry.get("title", t("misc.unknown_title"))
                if url:
                    self.queue.append((url, title))
                    added_count += 1
            await ctx.send(t("status.playlist_added", count=added_count))
        else:
            url = info.get("webpage_url")
            title = info.get("title", t("misc.unknown_title"))
            # Warnen wenn der Titel schon in der Queue ist – könnte ein Versehen sein
            dup_pos = next((i + 1 for i, (_, qt) in enumerate(self.queue) if qt == title), None)
            if dup_pos:
                await ctx.send(t("status.duplicate_warning", title=title, pos=dup_pos))
            evicted = self._evict_autoplay_song() if self.autoplay_enabled else None
            if evicted:
                logger.info(f"[p] Autoplay-Song verdrängt: {evicted}")
                self.queue.appendleft((url, title))
            else:
                self.queue.append((url, title))
            if not self.is_playing:
                # Startet gleich → Download schon während des ctx.send anstoßen.
                self.dl.prime_first_hit(url, title)
            await ctx.send(t("status.added", title=title))

        if not self.is_playing:
            self.is_playing = True
            await self.play_next(ctx)
        else:
            self._kick_prefetch()   # läuft schon Musik → neue Titel sofort vorladen

    @commands.command(name="stop")
    @require_same_voice()
    async def stop(self, ctx):
        """Beendet Radio-Modus oder aktuelle Wiedergabe (Queue bleibt erhalten)."""
        if self.is_radio:
            self._stop_radio()
            if ctx.voice_client and ctx.voice_client.is_playing():
                stop_playback(ctx.voice_client)
            await ctx.send(t("status.radio_stopped"))
        elif ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
            self.is_playing = False
            self._stopped_by_user = True   # Watchdog soll hier nicht von selbst neu starten
            self._stop_for_advance(ctx.voice_client)
            await ctx.send(t("status.playback_stopped"))
        else:
            await ctx.send(t("error.nothing_playing"))

    @commands.command(name="next")
    async def next_song(self, ctx, *, eingabe):
        """Fügt einen Song an die erste Stelle der Queue ein (spielt als nächstes).
        Format: !next URL  oder  !next URL||Titel  oder  !next Suchbegriff"""
        if not await self._ensure_voice(ctx):
            return

        await self._stop_radio_for_takeover(ctx)

        # Format: URL||Titel → direkt ohne yt_dlp-Lookup hinzufügen
        if "||" in eingabe:
            parts = eingabe.split("||", 1)
            url = parts[0].strip()
            title = parts[1].strip()
            if not await enforce_url_policy(ctx, url):
                return
            self.queue.appendleft((url, title))
            await ctx.send(t("status.next_added", title=title))
            if not self.is_playing:
                self.is_playing = True
                await self.play_next(ctx)
            else:
                self._kick_prefetch()
            return

        # Suchbegriff → ersten Treffer an erste Stelle, Alternativen als Buttons
        if not eingabe.startswith("http"):
            queued = await self._search_and_enqueue(
                ctx, eingabe, log_tag="next", timeout_key="error.search_timeout_short",
                insert="front", with_alts_key="status.next_with_alts",
                no_alts_key="status.next_added", base_content_key="status.next_added",
            )
            if not queued:
                return
            if not self.is_playing:
                self.is_playing = True
                await self.play_next(ctx)
            else:
                self._kick_prefetch()
            return

        # Direkte URL → yt_dlp-Lookup
        result = await self._fetch_single_track_info(ctx, eingabe, "next")
        if result is None:
            return
        url, title = result
        self.queue.appendleft((url, title))
        await ctx.send(t("status.next_added", title=title))
        if not self.is_playing:
            self.is_playing = True
            await self.play_next(ctx)
        else:
            self._kick_prefetch()

    @commands.command(name="s")
    @require_same_voice()
    async def skip(self, ctx):
        """Überspringt den aktuellen Track. Bei Radio: beendet den Radio-Modus."""
        if self.is_radio:
            self._stop_radio()
            if ctx.voice_client and ctx.voice_client.is_playing():
                stop_playback(ctx.voice_client)
            await ctx.send(t("status.radio_stopped"), delete_after=20)
            if self.queue:
                self.is_playing = True
                await self.play_next(ctx)
            return
        if ctx.voice_client and ctx.voice_client.is_playing():
            self._stop_for_advance(ctx.voice_client)
            await ctx.send(t("status.skipped"), delete_after=20)
        else:
            await ctx.send(t("error.no_song_playing"))

    @commands.command(name="x")
    async def pause(self, ctx):
        """Pausiert die Wiedergabe und aktualisiert is_playing."""
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.pause()
            self._mark_paused()   # Fortschrittsbalken einfrieren
            # is_playing muss hier auf False, damit !resume und !p
            # erkennen, dass gerade nichts aktiv abgespielt wird.
            self.is_playing = False
            self._stopped_by_user = True   # Pause ist gewollt – Watchdog nicht eingreifen
        else:
            await ctx.send(t("error.no_song_playing"))

    @commands.command()
    async def resume(self, ctx):
        """Setzt die Wiedergabe fort. Startet auch, wenn is_playing False aber Queue voll ist."""
        if ctx.voice_client and ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            self._mark_resumed()   # Pausendauer einrechnen, Balken läuft ohne Sprung weiter
            self.is_playing = True
            # Pause hatte _stopped_by_user gesetzt – ohne Reset blieben Autoplay,
            # Progressive-Resume und Watchdog bis zum Songende unterdrückt.
            self._stopped_by_user = False
        elif not self.is_playing and self.queue:
            # Edge-Case: Bot im Kanal, Queue nicht leer, aber nichts läuft.
            # Kann passieren wenn der Bot disconnected und reconnectet wurde.
            logger.info("[Auto-Start] Queue nicht leer, starte nächsten Song.")
            await self.play_next(ctx)
        else:
            await ctx.send(t("error.no_song_to_resume"))

    @commands.command(name="now")
    @require_same_voice()
    async def now_playing(self, ctx, *, eingabe: str = None):
        """Spielt einen Song sofort ab (stoppt den aktuellen). !now <Suche, URL oder Queue-Position>"""
        if not eingabe:
            await ctx.send(t("error.now_usage"))
            return

        # Zahl → Song an dieser Queue-Position sofort abspielen
        try:
            n = int(eingabe)
            if not self.queue:
                await ctx.send(t("error.queue_empty"))
                return
            if not (1 <= n <= len(self.queue)):
                await ctx.send(t("error.invalid_index_range", count=len(self.queue)))
                return
            if not await self._ensure_voice(ctx):
                return
            queue_list = list(self.queue)
            entry = queue_list.pop(n - 1)
            queue_list.insert(0, entry)
            self.queue = deque(queue_list)
            self._evict_autoplay_song()
            await ctx.send(t("status.playing", title=entry[1]))
            if ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
                self._stop_for_advance(ctx.voice_client)
            elif not self.is_playing:
                self.is_playing = True
                await self.play_next(ctx)
            return
        except ValueError:
            pass  # Kein Integer → weiter mit Suche/URL

        if not await self._ensure_voice(ctx):
            return

        await self._stop_radio_for_takeover(ctx)

        if not eingabe.startswith("http"):
            queued = await self._search_and_enqueue(
                ctx, eingabe, log_tag="now", timeout_key="error.search_timeout",
                insert="evict_front", with_alts_key="status.playing_with_alts",
                no_alts_key="status.playing",
            )
            if not queued:
                return
        else:
            result = await self._fetch_single_track_info(ctx, eingabe, "now")
            if result is None:
                return
            url, title = result
            self._evict_autoplay_song()
            self.queue.appendleft((url, title))
            await ctx.send(t("status.playing", title=title))

        if ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
            self._stop_for_advance(ctx.voice_client)  # after_playing-Callback startet den neuen Song
        elif not self.is_playing:
            self.is_playing = True
            await self.play_next(ctx)

    @commands.command(name="text")
    async def lyrics_cmd(self, ctx):
        """Zeigt den Liedtext des aktuell laufenden Songs via lyrics.ovh an."""
        if not self.current_track:
            await ctx.send(t("error.no_song_playing"))
            return

        title_raw = self.current_track[1]

        # YouTube-Titel folgen meist dem Muster "Artist - Title (Remix/Edit/...)"
        if " - " in title_raw:
            artist, song = title_raw.split(" - ", 1)
            song = re.sub(r'\s*\([^)]*\)', '', song).strip()
            artist = artist.strip()
        else:
            artist = ""
            song = re.sub(r'\s*\([^)]*\)', '', title_raw).strip()

        display = f"**{artist} – {song}**" if artist else f"**{song}**"
        await ctx.send(t("status.searching_lyrics", display=display))

        try:
            url = f"https://api.lyrics.ovh/v1/{urllib.parse.quote(artist)}/{urllib.parse.quote(song)}"
            # Geteilte Session (cog_load) statt pro Aufruf eine neue – spart
            # TLS-Handshake und Connection-Aufbau auf schwacher Hardware.
            async with self._http().get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    await ctx.send(t("error.lyrics_not_found"))
                    return
                data = await resp.json(content_type=None)
                lyrics = data.get("lyrics", "").strip()
        except asyncio.TimeoutError:
            await ctx.send(t("error.timeout"))
            return
        except Exception:
            logger.exception("[text] Fehler beim Abrufen des Liedtexts")
            await ctx.send(t("error.lyrics_error"))
            return

        if not lyrics:
            await ctx.send(t("error.lyrics_not_found"))
            return

        # Discord-Limit: 2000 Zeichen pro Nachricht → bei Bedarf aufteilen
        chunks = [lyrics[i:i+1900] for i in range(0, len(lyrics), 1900)]
        for i, chunk in enumerate(chunks):
            header = f"{display}\n" if i == 0 else ""
            await ctx.send(f"{header}```\n{chunk}\n```")

    @commands.command(name="loop")
    async def loop(self, ctx):
        """Schaltet den Loop-Modus durch: aus → Song wiederholen → Queue loopen → aus."""
        modes = [None, "song", "queue"]
        self.loop_mode = modes[(modes.index(self.loop_mode) + 1) % len(modes)]
        loop_keys = {None: "status.loop_off", "song": "status.loop_song", "queue": "status.loop_queue"}
        await ctx.send(t(loop_keys[self.loop_mode]))

    @commands.command(name="q")
    async def queue_list(self, ctx):
        """Zeigt die aktuelle Queue als reine Textliste mit Blätter-Buttons.

        Seitenumbruch folgt der Zeichenlänge (Discord-Nachrichtenlimit), nicht
        einer festen Songanzahl.
        """
        from views.queue_view import QueueView
        # Autoplay-Vorschau: der von _prefetch_autoplay eingereihte Song hängt
        # als letzter Queue-Eintrag – als "🔮 Als Nächstes"-Zeile zeigen statt
        # als nummerierter Eintrag. Nur wenn er wirklich hinten steht, sonst
        # verschöbe das Ausblenden die Nummern für !remove/!move.
        autoplay_next_title = None
        if (self.autoplay_enabled and self._autoplay_queued_url and self.queue
                and self.queue[-1][0] == self._autoplay_queued_url):
            autoplay_next_title = self.queue[-1][1]
        queue_snapshot = [
            (url, title, self.dl._url_cache[url].get("duration") if url in self.dl._url_cache else None)
            for url, title in self.queue
        ]
        if autoplay_next_title is not None:
            queue_snapshot.pop()
        view = QueueView(queue_snapshot, self.current_track, self.loop_mode,
                         autoplay_next_title=autoplay_next_title)
        await ctx.send(content=view.build_content(), view=view)

    @commands.command()
    @require_same_voice()
    async def clear(self, ctx):
        """Leert die Queue, stoppt die Wiedergabe und setzt Loop zurück."""
        self._stop_radio()
        self.queue.clear()
        self.dl.clear_cache()
        self.is_playing = False
        self.current_track = None
        self.loop_mode = None
        self._stopped_by_user = True   # unterdrückt Auto-Advance/Autoplay im after_playing-Callback
        if self.prefetch_task and not self.prefetch_task.done():
            self.prefetch_task.cancel()
        if self._autoplay_prefetch_task and not self._autoplay_prefetch_task.done():
            self._autoplay_prefetch_task.cancel()   # darf die geleerte Queue nicht neu befüllen
        if ctx.voice_client and ctx.voice_client.is_playing():
            self._stop_for_advance(ctx.voice_client)
        await ctx.send(t("status.cleared"))

    @commands.command(usage="!remove <position>")
    @require_same_voice()
    async def remove(self, ctx, index: int):
        """Entfernt einen Track an Position n aus der Queue."""
        # deque unterstützt kein pop(index) – kurzer Umweg über eine Liste.
        if 1 <= index <= len(self.queue):
            queue_list = list(self.queue)
            removed = queue_list.pop(index - 1)
            self.queue = deque(queue_list)
            self._restart_prefetch()   # alter Prefetch zielt auf die alte Reihenfolge
            await ctx.send(t("status.removed", title=removed[1]))
        else:
            await ctx.send(t("error.invalid_index"))

    @commands.command()
    @require_same_voice()
    async def move(self, ctx, *, term: str):
        """Verschiebt einen Song an den Anfang der Queue.

        !move 3          → Song an Position 3 nach vorne schieben
        !move songtitel  → ersten Treffer per Titelsuche nach vorne schieben
        """
        if not self.queue:
            await ctx.send(t("error.queue_empty"))
            return

        queue_list = list(self.queue)

        # Zahl → nach Index suchen
        idx = None
        try:
            n = int(term)
            if 1 <= n <= len(queue_list):
                idx = n - 1
            else:
                await ctx.send(t("error.invalid_index_range", count=len(queue_list)))
                return
        except ValueError:
            # Kein Integer → Titelsuche (case-insensitive, Teilstring)
            term_lower = term.lower()
            for i, (_, qt) in enumerate(queue_list):
                if term_lower in qt.lower():
                    idx = i
                    break
            if idx is None:
                await ctx.send(t("error.song_not_found_in_queue", term=term))
                return

        if idx == 0:
            await ctx.send(t("status.already_first", title=queue_list[0][1]))
            return

        entry = queue_list.pop(idx)
        queue_list.insert(0, entry)
        self.queue = deque(queue_list)
        self._restart_prefetch()   # alter Prefetch zielt auf die alte Reihenfolge
        await ctx.send(t("status.moved_to_front", title=entry[1]))

    @commands.command()
    @require_same_voice()
    async def shuffle(self, ctx):
        """Mischt die Queue zufällig durch."""
        # random.shuffle() arbeitet auf Listen, nicht auf deques – also kurz umwandeln.
        if len(self.queue) < 2:
            await ctx.send(t("error.not_enough_to_shuffle"))
        else:
            queue_list = list(self.queue)
            random.shuffle(queue_list)
            self.queue = deque(queue_list)
            self._restart_prefetch()   # alter Prefetch zielt auf die alte Reihenfolge
            await ctx.send(t("status.shuffled"))

    @commands.command()
    async def replay(self, ctx):
        """Stellt den zuletzt gespielten Song an den Anfang der Queue."""
        if self.last_played:
            lp_url, lp_title, *_ = self.last_played
            self.queue.appendleft((lp_url, lp_title))
            await ctx.send(t("status.replaying", title=lp_title))
            if not self.is_playing:
                self.is_playing = True
                await self.play_next(ctx)
        else:
            await ctx.send(t("error.no_last_song"))

    @commands.command()
    @require_same_voice()
    async def eq(self, ctx, preset: str = None):
        """Setzt einen EQ-Preset oder listet verfügbare Presets auf."""
        if not preset:
            presets = ", ".join(f"**{name}**" if name == self.equalizer else name for name in self.eq_presets)
            await ctx.send(t("status.eq_presets", presets=presets))
            return
        if preset.lower() in self.eq_presets:
            self.equalizer = preset.lower()
            msg = t("status.eq_set", preset=preset)
            if ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()) and self.current_track:
                tr_url, tr_title, *_ = self.current_track
                self.current_track = None  # verhindert Doppel-Insert durch loop-Branch in after_playing
                self.queue.appendleft((tr_url, tr_title))
                self._stop_for_advance(ctx.voice_client)
                msg += t("status.eq_restart")
            await ctx.send(msg)
        else:
            presets = ", ".join(self.eq_presets.keys())
            await ctx.send(t("error.eq_unknown", presets=presets))

    @commands.command(name="seek")
    @require_same_voice()
    async def seek(self, ctx, zeit: str = None):
        """Springt an eine Position im aktuellen Track. !seek 1:23 oder !seek 83"""
        if not zeit:
            await ctx.send(t("error.seek_usage"))
            return
        offset = self._parse_time(zeit)
        if offset is None or offset < 0:
            await ctx.send(t("error.seek_invalid"))
            return
        if self.is_radio:
            await ctx.send(t("error.seek_radio"))
            return
        if not self.current_track:
            await ctx.send(t("error.no_track"))
            return
        if not ctx.voice_client or not (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
            await ctx.send(t("error.no_track"))
            return

        url, title, duration = self.current_track
        mins, secs = divmod(offset, 60)
        self._seek_offset = offset
        self.current_track = None
        self.queue.appendleft((url, title))
        self._stop_for_advance(ctx.voice_client)
        await ctx.send(t("status.seeking", mins=mins, secs=f"{secs:02d}", title=title))

    @commands.command(name="baba")
    async def baba(self, ctx):
        """Spielt Babas Playlist ab. Kein Argument nötig – einfach !baba und los."""
        await ctx.invoke(self.p, eingabe="https://www.youtube.com/playlist?list=PLhqD5zya16QavuozTOLCZ3Jn6gQu66Tvj")

