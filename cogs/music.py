# Hier lebt die gesamte Musik-Logik: Queue, Downloads, Wiedergabe, EQ und Autoplay.
# Kurz gesagt: die wichtigste Datei im ganzen Bot. Treat her well.

import asyncio
import json
import random
import re
import shlex
import tempfile
import time
import urllib.parse
from collections import deque
from pathlib import Path

import aiohttp

import discord
import psutil
from utils.logger import logger
from utils.i18n import t
from utils.url_check import enforce_url_policy
from utils.checks import require_same_voice, require_admin
from discord.ext import commands, tasks
from cogs.downloader import (
    Downloader, entry_url, normalize_title,
    select_autoplay_candidates, yt_video_id,
)
from cogs.presets import EQ_PRESETS
from views.music_controls import MusicControlView, SearchAutoplayView
from cogs.music_radio import RadioMixin
from cogs.music_stats import StatsMixin
from cogs.music_queue_io import QueuePersistenceMixin

SCORE_FILE = Path("play_counts.json")

# Max. Seek-Resumes pro Track, wenn FFmpeg die wachsende Datei eines progressiven
# Downloads überholt hat – danach Fallback auf die Stream-/Retry-Kaskade.
PROGRESSIVE_MAX_RESUMES = 2

# Sentinel für _extract_info_or_report: unterscheidet "Fehler wurde bereits
# gemeldet" von einem echten None-Ergebnis aus yt_dlp (das die Aufrufer wie
# bisher selbst krachen lassen).
_YTDLP_FAILED = object()


class MusicCommands(RadioMixin, StatsMixin, QueuePersistenceMixin, commands.Cog):
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

    def __init__(self, bot):
        self.bot = bot

        # deque statt list, weil popleft() in O(1) läuft – bei langen Queues
        # ist das deutlich schneller als list.pop(0).
        self.queue = deque()

        self.is_playing = False
        self.current_track = None   # Aktuell spielender Song (url, title) – für !now
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
            info = await asyncio.wait_for(
                asyncio.to_thread(self.dl.autoplay_ydl.extract_info, fetch_url, download=False),
                timeout=30.0,
            )
            entries = (info.get("entries") or []) if info else []

            candidates = select_autoplay_candidates(
                entries, ref_url, self._recently_played, self._recently_played_titles
            )

            if not candidates:
                await ctx.send(t("error.autoplay_no_results"))
                logger.warning("[Autoplay] Keine nutzbaren Einträge im Mix")
                return

            pool = candidates[1:] if len(candidates) > 1 else candidates
            chosen = random.choice(pool)
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

    async def _prefetch_next(self, idx: int = 0):
        """Lädt Song an Position idx der Queue still im Hintergrund herunter."""
        await self.dl.prefetch_next(self.queue, idx)

    async def _prefetch_upcoming(self):
        """Lädt bis zu 2 Queue-Songs sequenziell vor – yt_dlp ist nicht
        thread-safe, daher kein paralleles gather. Sequenziell reicht: während
        Song N spielt, werden N+1 und N+2 nacheinander heruntergeladen."""
        # Bandbreiten-Rücksicht: erst warten, bis kein progressiver Download
        # mehr läuft – der füttert die laufende Wiedergabe.
        await self.dl.wait_progressive_idle()
        await self._prefetch_next(0)
        if len(self.queue) >= 2:
            await self._prefetch_next(1)

    def _kick_prefetch(self):
        """Startet den Hintergrund-Download der nächsten Queue-Songs, wenn
        gerade ein Song läuft und noch kein Prefetch aktiv ist. Nötig für
        Titel, die MITTEN im Song eingereiht werden (!p/!next): play_next
        erstellt den Prefetch-Task nur beim Trackstart – da war die Queue
        oft noch leer, und der neue Song würde erst beim Übergang (progressiv,
        inkl. yt_dlp-Extraktion) geladen → hörbare Lücke."""
        if not self.queue or not self.is_playing:
            return
        if self.prefetch_task and not self.prefetch_task.done():
            # Läuft bereits – _prefetch_upcoming peekt die Queue bei jedem
            # Schritt frisch, neue Einträge werden also noch mitgenommen.
            return
        self.prefetch_task = asyncio.create_task(self._prefetch_upcoming())

    async def _prefetch_autoplay(self, ctx):
        """Sucht und lädt nächsten Autoplay-Song im Hintergrund während der aktuelle läuft."""
        ref_url = ref_title = None
        if self.current_track:
            ref_url, ref_title, *_ = self.current_track
        result = await self.dl.prefetch_autoplay(ref_url, ref_title, self._recently_played, self._recently_played_titles)
        if result and self.autoplay_enabled:
            url, title = result
            self.queue.append((url, title))
            self._autoplay_queued_url = url
            logger.info(f"[Autoplay Prefetch] In Queue eingereiht: {title}")

    async def _auto_leave(self, voice_client):
        """Verlässt den Channel nach AUTO_LEAVE_SECONDS wenn kein User mehr drin ist."""
        await asyncio.sleep(self.AUTO_LEAVE_SECONDS)
        if voice_client.is_connected():
            self.queue.clear()
            self.is_playing = False
            self.current_track = None
            if voice_client.is_playing() or voice_client.is_paused():
                self._stop_for_advance(voice_client)
            await voice_client.disconnect()
            if self.text_channel:
                await self.text_channel.send(t("misc.auto_leave"))
            logger.info("[Auto-Leave] Channel verlassen – keine User mehr.")

    def _cancel_idle_timer(self):
        """Stoppt den Idle-Timer – aufgerufen sobald wieder Audio läuft."""
        if self.idle_leave_task and not self.idle_leave_task.done():
            self.idle_leave_task.cancel()
        self.idle_leave_task = None

    def _start_idle_timer(self, voice_client):
        """(Re)startet den Idle-Timer. Bei anhaltender Stille verlässt der Bot
        später den Channel, damit Discord die Verbindung nicht mit 1006 dropt."""
        self._cancel_idle_timer()
        self.idle_leave_task = asyncio.create_task(self._idle_leave(voice_client))

    async def _idle_leave(self, voice_client):
        """Verlässt den Channel nach IDLE_LEAVE_SECONDS ohne Wiedergabe."""
        await asyncio.sleep(self.IDLE_LEAVE_SECONDS)
        # Nur gehen wenn wirklich noch nichts läuft (Autoplay/neuer Song hätte
        # den Timer längst via _cancel_idle_timer abgeräumt – doppelte Sicherung).
        if (
            voice_client.is_connected()
            and not self.is_radio
            and not voice_client.is_playing()
            and not voice_client.is_paused()
        ):
            self.queue.clear()
            self.is_playing = False
            self.current_track = None
            await voice_client.disconnect()
            if self.text_channel:
                await self.text_channel.send(t("misc.idle_leave"))
            logger.info("[Auto-Leave] Channel verlassen – zu lange inaktiv (Idle).")

    @tasks.loop(seconds=30)
    async def _voice_watchdog(self):
        """Sicherheitsnetz gegen den 1006-Reconnect-Bug.

        Wenn Discord die Voice-Verbindung während eines Songs dropt (Code 1006),
        reconnectet discord.py automatisch – aber die Wiedergabe bleibt manchmal
        stehen (Queue voll, doch is_playing False). Hängt dieser Zustand über
        zwei Ticks (~60 s) an, wird der nächste Song neu gestartet. Zwei Ticks,
        damit normale Track-Übergänge (Sekundenbereich) nicht fälschlich greifen.
        """
        ctx = self._last_ctx
        vc = ctx.voice_client if ctx else None
        stuck = (
            vc is not None
            and vc.is_connected()
            and not self.is_radio
            and bool(self.queue)
            and not self.is_playing
            and not vc.is_playing()
            and not vc.is_paused()
            and not self._stopped_by_user
            and self._playback_done.is_set()
        )
        if not stuck:
            self._stuck_ticks = 0
            return
        self._stuck_ticks += 1
        if self._stuck_ticks >= 2:
            self._stuck_ticks = 0
            logger.info("[Voice] Wiedergabe nach Reconnect hängen geblieben – setze fort.")
            try:
                await self.play_next(ctx)
            except Exception as e:
                logger.warning(f"[Voice] Watchdog-Restart fehlgeschlagen: {e}")

    @_voice_watchdog.before_loop
    async def _before_voice_watchdog(self):
        await self.bot.wait_until_ready()

    def _elapsed_seconds(self):
        """Abgespielte Sekunden des aktuellen Songs, Pausen herausgerechnet."""
        if self.track_start_time is None:
            return None
        now = time.monotonic()
        paused = self._np_paused_total
        if self._np_paused_at is not None:
            paused += now - self._np_paused_at
        return max(0.0, now - self.track_start_time - paused)

    def _mark_paused(self):
        """Pause-Beginn merken (idempotent), damit der Balken einfriert."""
        if self._np_paused_at is None:
            self._np_paused_at = time.monotonic()

    def _mark_resumed(self):
        """Pausendauer aufsummieren, damit der Balken nicht vorspringt."""
        if self._np_paused_at is not None:
            self._np_paused_total += time.monotonic() - self._np_paused_at
            self._np_paused_at = None

    @staticmethod
    def _progress_bar(elapsed, total, length=21):
        """'▬▬▬🔘▬▬▬▬ m:ss / m:ss' – None wenn Dauer unbekannt."""
        if not total or total <= 0:
            return None
        # elapsed auch fürs Zeitlabel clampen – sonst läuft die Anzeige über die
        # Songdauer hinaus und der Bar-String ändert sich endlos weiter (jeder
        # Tick ein neuer String → die _np_last_desc-Dedupe greift nie).
        elapsed = min(max(0.0, elapsed), total)
        frac = elapsed / total
        pos = int(frac * (length - 1))
        bar = "▬" * pos + "🔘" + "▬" * (length - 1 - pos)
        fmt = lambda s: f"{int(s) // 60}:{int(s) % 60:02d}"
        return f"{bar} {fmt(elapsed)} / {fmt(total)}"

    async def _finalize_progress_bar(self):
        """Setzt Balken + Dauer der aktuellen Nachricht ans Ende (100 %).

        Wird beim natürlichen Songende aufgerufen, damit die Anzeige nicht bei
        z. B. '3:43 / 3:45' einfriert, sondern auf '🔘 ganz rechts, 3:45 / 3:45' springt.
        """
        msg, embed = self.now_playing_msg, self.now_playing_embed
        if not (msg and embed and self.current_track):
            return
        duration = self.current_track[2]
        bar = self._progress_bar(duration, duration)   # elapsed == total → 🔘 ganz rechts
        if not bar or bar == self._np_last_desc:
            return
        embed.description = bar
        try:
            await msg.edit(embed=embed)
            self._np_last_desc = bar
        except Exception:
            pass

    @tasks.loop(seconds=2)
    async def _progress_loop(self):
        """Aktualisiert den Fortschrittsbalken in der aktuellen Now-Playing-Nachricht."""
        msg, embed = self.now_playing_msg, self.now_playing_embed
        if not (msg and embed and self.current_track and self.is_radio is False):
            return
        # Nur editieren, wenn wirklich etwas läuft: is_playing ODER pausiert
        # (der Pause-Button setzt is_playing=False, der Balken soll aber das
        # ⏸-Präfix noch bekommen). Zusätzlich muss der Voice-Client den Track
        # tatsächlich spielen/pausiert haben – tote Tracks editieren wir nie.
        if not self.is_playing and self._np_paused_at is None:
            return
        vc = self._last_ctx.voice_client if self._last_ctx else None
        if not vc or not (vc.is_playing() or vc.is_paused()):
            return
        duration = self.current_track[2]
        elapsed = self._elapsed_seconds()
        if elapsed is None:
            return
        bar = self._progress_bar(elapsed, duration)
        if not bar:
            return
        if self._np_paused_at is not None:   # pausiert
            bar = "⏸ " + bar
        if bar == self._np_last_desc:        # nichts Neues → kein API-Call
            return
        embed.description = bar
        try:
            await msg.edit(embed=embed)      # view-Param weglassen → Buttons bleiben
            self._np_last_desc = bar
        except discord.RateLimited:
            self._disable_np_edits(msg)
        except discord.HTTPException as e:
            if e.status == 429:
                self._disable_np_edits(msg)
        except Exception:
            pass

    def _disable_np_edits(self, msg):
        """Rate-Limit (429): Live-Edits für diese Nachricht dauerhaft einstellen –
        einmal loggen statt still weiterzuhämmern."""
        logger.warning(
            "[Progress] 429 beim Edit der Now-Playing-Nachricht – "
            "Live-Updates für diese Nachricht deaktiviert."
        )
        if self.now_playing_msg is msg:
            self.now_playing_msg = None
            self.now_playing_embed = None

    @_progress_loop.before_loop
    async def _before_progress_loop(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """Startet den Auto-Leave-Timer wenn alle User den Channel verlassen haben."""
        if member.bot:
            return
        voice_client = member.guild.voice_client
        if not voice_client:
            return

        humans = [m for m in voice_client.channel.members if not m.bot]
        if len(humans) == 0:
            # Alle weg – Timer starten (alten zuerst abbrechen falls noch einer läuft)
            if self.auto_leave_task:
                self.auto_leave_task.cancel()
            self.auto_leave_task = asyncio.create_task(self._auto_leave(voice_client))
            logger.info("[Auto-Leave] Channel leer – starte 5-Minuten-Timer.")
        else:
            # Jemand ist (wieder) da – Timer abbrechen
            if self.auto_leave_task and not self.auto_leave_task.done():
                self.auto_leave_task.cancel()
                self.auto_leave_task = None

    def _stop_for_advance(self, voice_client):
        """vc.stop() für absichtliche Wiedergabe-Wechsel (Skip, !now, !eq, !seek,
        !stop, !clear, Auto-Leave). Setzt das One-Shot-Flag _suppress_resume,
        damit after_playing den Stopp nicht als vorzeitiges Dateiende einer
        wachsenden Datei missdeutet und den Track wieder vorn einreiht."""
        self._suppress_resume = True
        voice_client.stop()

    async def _resolve_track(self, url: str, title: str, force_download: bool = False):
        """Löst URL auf, stellt sicher dass Audiodatei lokal vorliegt.

        Returns: (info, filename, title, duration)
        """
        # _seek_offset wird erst NACH diesem Aufruf in play_next konsumiert
        # (Swap auf 0) – hier lesen ist also der Wert des anstehenden -ss.
        # Der progressive Pfad puffert entsprechend mehr, damit die Bytes bis
        # zum Seek-Ziel in der wachsenden Datei existieren.
        return await self.dl.resolve_track(
            url, title,
            force_download=force_download, min_buffer_seconds=self._seek_offset,
        )

    @staticmethod
    def _ffmpeg_header_opts(info) -> str:
        """-headers-Option für FFmpeg aus dem yt_dlp-Info-Dict.

        yt_dlp hat die Stream-URL mit genau diesen Headern (User-Agent & Co.)
        angefragt – ohne sie lehnen googlevideo-CDN-Server Anfragen sporadisch
        mit 403 ab. shlex.quote reicht als Quoting, weil discord.py
        before_options mit shlex.split zerlegt (kein Shell-Aufruf); die
        \\r\\n-getrennte Header-Liste ist dasselbe Format, das yt_dlp selbst
        an FFmpeg übergibt.
        """
        headers = (info.get("http_headers") or {}) if info else {}
        if not headers:
            return ""
        blob = "".join(f"{key}: {value}\r\n" for key, value in headers.items())
        return f"-headers {shlex.quote(blob)}"

    @staticmethod
    def _stderr_tail(buf, max_lines: int = 20) -> str:
        """Liest die letzten Zeilen aus dem FFmpeg-stderr-Puffer und schließt ihn."""
        if buf is None:
            return ""
        try:
            buf.seek(0)
            data = buf.read()
        except Exception:
            return ""
        finally:
            try:
                buf.close()
            except Exception:
                pass
        lines = data.decode("utf-8", errors="replace").strip().splitlines()
        return "\n".join(lines[-max_lines:])

    @staticmethod
    def _classify_ffmpeg_error(stderr_text: str):
        """Ordnet FFmpeg-stderr grob ein: 'input' (Netz/HTTP-Quelle), 'filter' (EQ-Kette)
        oder None (keine verwertbare Ausgabe). Filter-Marker gewinnen, weil sie
        eindeutig sind – Input-Marker sind breiter gefasst."""
        if not stderr_text:
            return None
        low = stderr_text.lower()
        filter_markers = (
            "error initializing filter",
            "error reinitializing filters",
            "no such filter",
            "invalid filter",
            "error applying option",
        )
        input_markers = (
            "403 forbidden",
            "404 not found",
            "http error",
            "invalid data found",
            "connection reset",
            "connection refused",
            "connection timed out",
            "server returned",
            "input/output error",
            "end of file",
            "error in the pull function",
            "failed to resolve",
        )
        if any(m in low for m in filter_markers):
            return "filter"
        if any(m in low for m in input_markers):
            return "input"
        return None

    async def play_next(self, ctx):
        """Spielt den nächsten Song in der Queue. Wird rekursiv nach jedem Track aufgerufen."""
        if self.is_radio:
            logger.info("[play_next] Radio aktiv – play_next übersprungen.")
            # Radio hat die Musik gestoppt → ausstehende Post-Play-Zuweisungen des
            # unterbrochenen Tracks entwerten (gleicher Race-Typ wie unten im Cleanup).
            self._track_generation += 1
            return
        # DM-Bridge spricht gerade → den Voice-Client NICHT mit dem nächsten Track besetzen.
        # Sonst kollidiert unser vc.play() mit dem der Bridge ("Already playing audio."): ein
        # Track-Ende (natürlich oder durch das vc.stop() der Bridge) würde hier sofort den
        # nächsten Song starten. Das Flag setzt/löscht dm_bridge.py um die /speak-Wiedergabe.
        if getattr(self.bot, "dm_speaking", False):
            logger.info("[play_next] DM spricht – Auto-Advance unterdrückt.")
            # Auch hier: Die Bridge hat die Musik gestoppt – hängende awaits des
            # unterbrochenen Durchlaufs dürfen keinen Zustand mehr schreiben.
            self._track_generation += 1
            return
        if not self.queue:
            logger.info("[Queue] Leere Warteschlange. Wiedergabe gestoppt.")
            self.is_playing = False
            # Cleanup entwertet alle noch hängenden Post-Play-awaits des gerade
            # gestorbenen Tracks – sonst überschreiben sie die Aufräumarbeit unten.
            self._track_generation += 1
            # current_track als last_played sichern bevor es gecleant wird,
            # damit autoplay() noch weiß was zuletzt lief.
            if self.current_track:
                self.last_played = self.current_track
                # Song lief natürlich zu Ende (kein !stop/!clear) → Fortschritt ans Ende setzen,
                # damit Balken und Dauer nicht unvollständig einfrieren.
                if not self._stopped_by_user:
                    await self._finalize_progress_bar()
            self.current_track = None
            # Referenzen kappen, damit _progress_loop/_finalize_progress_bar die tote
            # Nachricht nie wieder anfassen. Die Nachricht selbst wird gemerkt, damit
            # der nächste Track ihre Buttons entfernen kann (nur der aktuelle Song
            # behält Buttons) – bis dahin bleiben Resume/Autoplay darauf klickbar.
            if self.now_playing_msg is not None:
                self._ended_np = (
                    self.now_playing_msg,
                    self.last_played[1] if self.last_played else None,
                )
            self.now_playing_msg = None
            self.now_playing_embed = None
            self.track_start_time = None
            # Idle-Timer starten: Bleibt es still (Autoplay aus oder Autoplay
            # schlägt fehl), verlässt der Bot später den Channel, bevor Discord
            # die stille Verbindung mit Code 1006 wegwirft. Startet ein neuer
            # Song (auch via Autoplay), räumt _cancel_idle_timer den Timer ab.
            if ctx.voice_client:
                self._start_idle_timer(ctx.voice_client)
            # Autoplay rettet die Stille – aber nur wenn gewünscht und nicht
            # gerade vom User gestoppt (!stop/!clear). Sonst würde der after_playing-
            # Callback nach einem !clear sofort den nächsten Song starten.
            if self.autoplay_enabled and not self._stopped_by_user:
                # Wenn der Prefetch-Task noch läuft, kurz warten – er hat den Song
                # bereits heruntergeladen und hängt ihn gleich in die Queue.
                if self._autoplay_prefetch_task and not self._autoplay_prefetch_task.done():
                    logger.info("[Autoplay] Warte auf laufenden Prefetch-Task...")
                    try:
                        await asyncio.wait_for(asyncio.shield(self._autoplay_prefetch_task), timeout=60.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        logger.warning("[Autoplay] Prefetch-Task nicht rechtzeitig fertig – Fallback")
                # Queue prüfen: Prefetch hat ggf. schon etwas eingereiht
                if self.queue:
                    self.is_playing = True
                    await self.play_next(ctx)
                else:
                    await self.autoplay(ctx)
            return

        # Guard: verhindert die "Already playing audio"-Kaskade bei gleichzeitigen
        # play_next-Aufrufen (z.B. after_playing-Callback + Error-Handler-create_task).
        # Song NICHT aus der Queue nehmen – erst nach dem Check.
        # Kurzes Warten: nach einem Reconnect kann FFmpeg noch im Cleanup sein (exit-code 1)
        # obwohl after_playing bereits ausgelöst wurde – is_playing() wäre dann fälschlicherweise True.
        if ctx.voice_client and ctx.voice_client.is_playing():
            try:
                await asyncio.wait_for(self._playback_done.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                if ctx.voice_client.is_playing():
                    logger.warning("[play_next] Voice client noch am spielen – konkurrenten Aufruf ignoriert.")
                    return
                # Event stuck obwohl Wiedergabe bereits beendet → zurücksetzen und fortfahren.
                logger.warning("[play_next] _playback_done blockiert, Wiedergabe aber beendet – Event zurückgesetzt.")
                self._playback_done.set()
            self._playback_done.clear()

        url, title = self.queue.popleft()
        if url == self._autoplay_queued_url:
            self._autoplay_queued_url = None
        logger.info(f"[Nächster Track] {title} ({url})")

        stderr_buf = None
        try:
            # Dritter Anlauf nach zwei toten Stream-Versuchen → Download erzwingen.
            force_download = url == self._force_download_url
            info, filename, title, duration = await self._resolve_track(url, title, force_download=force_download)

            if self._skip_resolving:
                self._skip_resolving = False
                await self.play_next(ctx)
                return

            eq_filter = self.eq_presets.get(self.equalizer, "")
            seek_offset, self._seek_offset = self._seek_offset, 0
            is_stream = isinstance(filename, str)  # True wenn > 20 min → direkter HTTP-Stream
            # Wachsende Datei eines progressiven Downloads? Snapshot zum Start-
            # zeitpunkt – der Download kann während der Wiedergabe fertig werden.
            is_growing = (not is_stream) and self.dl.is_incomplete(filename)
            prog_task = None if is_stream else self.dl.progressive_task_for(url)

            if is_stream and force_download:
                # Der Download-Fallback konnte keine lokale Datei liefern (>20-min-
                # Tracks werden nie heruntergeladen) → aufgeben statt denselben
                # Stream ein drittes Mal identisch scheitern zu lassen.
                self._force_download_url = None
                self._stream_retry_url = None
                logger.warning(f"[Stream-Retry] Download-Fallback lieferte keine lokale Datei – gebe auf: {title}")
                try:
                    await ctx.send(t("error.stream_giveup", title=title))
                except Exception:
                    pass
                asyncio.create_task(self.play_next(ctx))
                return

            if is_stream:
                _parts = ["-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"]
                _headers = self._ffmpeg_header_opts(info)
                if _headers:
                    _parts.append(_headers)
                if seek_offset:
                    _parts.append(f"-ss {seek_offset}")
                before_opts = " ".join(_parts)
            else:
                before_opts = f"-ss {seek_offset}" if seek_offset else None

            # FFmpeg-stderr in eine Temp-Datei umleiten: Stirbt der Prozess sofort,
            # sind die letzten Zeilen der einzige Beweis für die Ursache (403 vom
            # CDN? kaputter Filter?). Gelesen und geschlossen wird in after_playing.
            stderr_buf = tempfile.TemporaryFile()

            if eq_filter:
                # Filter aktiv → dekodieren, EQ anwenden, mit 192kbps zu Opus enkodieren.
                # -vn unterdrückt den Video-Stream.
                source = discord.FFmpegOpusAudio(
                    str(filename),
                    bitrate=192,
                    before_options=before_opts,
                    options=f"-vn {eq_filter}",
                    stderr=stderr_buf,
                )
            elif is_stream:
                # HTTP-Stream: codec=copy funktioniert nicht zuverlässig bei Netz-URLs → transkodieren.
                source = discord.FFmpegOpusAudio(
                    str(filename), bitrate=192, before_options=before_opts, stderr=stderr_buf
                )
            elif is_growing:
                # Wachsende webm: nie codec=copy – der Muxer schreibt Cues/Duration
                # erst am Dateiende, Transkodieren liest tolerant linear. Lokale
                # Datei → keine -reconnect/-headers-Optionen nötig.
                source = discord.FFmpegOpusAudio(
                    str(filename), bitrate=192, before_options=before_opts, stderr=stderr_buf
                )
            else:
                # Kein Filter (flat) → Opus-Stream 1:1 durchreichen, kein Qualitätsverlust.
                source = discord.FFmpegOpusAudio(
                    str(filename), codec="copy", before_options=before_opts, stderr=stderr_buf
                )

            def after_playing(error):
                """Callback, der nach jedem Track von FFmpeg aufgerufen wird.

                Läuft in einem separaten Thread – daher run_coroutine_threadsafe
                statt await. Direkt awaiten würde hier crashen.
                """
                self.bot.loop.call_soon_threadsafe(self._playback_done.set)
                # One-Shot: Flag immer konsumieren, damit ein absichtlicher Stopp
                # nicht in den after_playing-Lauf eines späteren Tracks leakt.
                suppress_resume, self._suppress_resume = self._suppress_resume, False
                if error:
                    logger.warning(f"[Fehler beim Abspielen] {error}")
                elapsed = time.monotonic() - self.track_start_time if self.track_start_time else 999
                # stderr immer auslesen (schließt den Puffer), loggen nur im Fehlerfall.
                stderr_tail = self._stderr_tail(stderr_buf)
                verdict = self._classify_ffmpeg_error(stderr_tail)
                if error or elapsed < 2.0:
                    if stderr_tail:
                        logger.warning(f"[FFmpeg] stderr (letzte Zeilen):\n{stderr_tail}")
                    if elapsed < 2.0:
                        _verdicts = {
                            "input": "Input-/Netzwerkfehler (Quelle lieferte keine Daten)",
                            "filter": "Filterfehler (EQ-Kette)",
                        }
                        logger.warning(
                            f"[FFmpeg] Track lief nur {elapsed:.2f}s – Einstufung: "
                            f"{_verdicts.get(verdict, 'unbekannt (keine verwertbare stderr-Ausgabe)')}. "
                            f"Aktives Preset: '{self.equalizer}'"
                        )

                retry_scheduled = False

                # Progressiver Download: Endete die wachsende Datei vorzeitig
                # (FFmpeg hat den Download überholt oder der Download starb),
                # wird der Track mit -ss an der Hörposition wieder vorn eingereiht.
                # Läuft VOR dem Marker-Reset unten: auch ein Resume-Durchlauf, der
                # ≥2 s spielte und erneut früh endete, muss den Zähler behalten –
                # sonst griffe die Kappe nie. Nie bei absichtlichen Stopps
                # (_suppress_resume via _stop_for_advance, !stop/!restart via
                # _stopped_by_user, Radio-Takeover, DM-Bridge-Übernahme).
                if (is_growing and not error and verdict != "filter"
                        and not suppress_resume and not self._stopped_by_user
                        and not self.is_radio
                        and not getattr(self.bot, "dm_speaking", False)):
                    played = seek_offset + max(0.0, elapsed - self._np_paused_total)
                    still_incomplete = (
                        (prog_task is not None and not prog_task.done())
                        or self.dl.is_incomplete(filename)
                    )
                    if still_incomplete and duration and played < duration - 5:
                        self._seek_offset = max(0, int(played) - 1)  # 1 s Überlappung gegen Schnittkante
                        self._progressive_resume_url = url           # kein doppelter Play-Count
                        if (self._progressive_resume_count < PROGRESSIVE_MAX_RESUMES
                                and prog_task is not None and not prog_task.done()):
                            # Download läuft noch → wieder einsteigen.
                            self._progressive_resume_count += 1
                            self.queue.appendleft((url, title))
                            retry_scheduled = True
                            logger.warning(
                                f"[Progressiv] Vorzeitiges Dateiende nach {played:.0f}s/{duration}s – "
                                f"setze bei {int(played) // 60}:{int(played) % 60:02d} fort: {title}"
                            )
                        else:
                            # Download tot oder Resume-Kappe erreicht → progressiv für
                            # diese URL sperren; resolve_track liefert dann den CDN-
                            # Stream, dessen bestehende Retry-Kette übernimmt.
                            self.dl.block_progressive(url)
                            self.queue.appendleft((url, title))
                            retry_scheduled = True
                            logger.warning(f"[Progressiv] Resume aufgegeben (Download tot/Kappe) – Fallback auf Stream: {title}")

                # Selbstheilung: Stream starb sofort an einem Input-Fehler (typisch:
                # sporadisches 403 vom CDN) → genau EIN Neuversuch mit frisch
                # extrahierter URL, danach EIN letzter Anlauf als lokaler Download.
                # Echte Filterfehler werden NICHT wiederholt.
                if not retry_scheduled and is_stream and elapsed < 1.0 and verdict == "input":
                    if self._stream_retry_url != url:
                        self._stream_retry_url = url
                        self.dl.invalidate(url)   # gecachte (Prefetch-)Metadaten wegwerfen
                        self.queue.appendleft((url, title))
                        retry_scheduled = True
                        logger.warning(
                            f"[Stream-Retry] Input-Fehler nach {elapsed:.2f}s – "
                            f"einmaliger Neuversuch mit frischer URL: {title}"
                        )
                    else:
                        # Zweiter Stream-Versuch tot → letzter Anlauf: Download statt
                        # Stream. yt_dlp lädt über den eigenen HTTP-Client – der ist
                        # von CDN-403s gegen FFmpeg meist nicht betroffen.
                        # _stream_retry_url bleibt gesetzt (kein doppelter Play-Count).
                        self._force_download_url = url
                        self.dl.invalidate(url)
                        self.queue.appendleft((url, title))
                        retry_scheduled = True
                        logger.warning(f"[Stream-Retry] Auch der zweite Versuch schlug fehl – letzter Anlauf als Download: {title}")
                        asyncio.run_coroutine_threadsafe(
                            ctx.send(t("error.stream_download_fallback", title=title)), self.bot.loop
                        )
                elif url == self._force_download_url and (error or elapsed < 2.0):
                    # Auch die heruntergeladene Datei stirbt sofort → endgültig aufgeben.
                    self._force_download_url = None
                    self._stream_retry_url = None
                    logger.warning(f"[Stream-Retry] Download-Fallback spielte ebenfalls nicht – gebe auf: {title}")
                    asyncio.run_coroutine_threadsafe(
                        ctx.send(t("error.stream_giveup", title=title)), self.bot.loop
                    )

                if (not retry_scheduled and not error and elapsed >= 2.0
                        and url in (self._stream_retry_url, self._progressive_resume_url)):
                    # Der Neuversuch/Resume lief normal zu Ende → Sperren aufheben.
                    self._stream_retry_url = None
                    self._force_download_url = None
                    self._progressive_resume_url = None
                    self._progressive_resume_count = 0

                # Queue-Stand nach jedem Track in Datei sichern – nicht als
                # Restore-Point gedacht, nur als Protokoll der letzten Session.
                queue_data = list(self.queue)
                try:
                    with open("last_queue.json", "w", encoding="utf-8") as f:
                        json.dump(queue_data, f, ensure_ascii=False, indent=2)
                    logger.info("[SAVE] Queue automatisch gespeichert nach Track-Ende.")
                except Exception as e:
                    logger.warning(f"[SAVE] Fehler beim Speichern der Queue: {e}")

                # Loop-Logik: Song zurück in die Queue legen bevor play_next aufgerufen wird.
                # Queue speichert 2-Tuples (url, title), current_track ist ein 3-Tuple.
                # Steht ein Stream-Retry an, liegt der Song schon vorne in der Queue –
                # die Loop-Logik würde ihn sonst doppelt einreihen.
                if not retry_scheduled:
                    if self.loop_mode == "song" and self.current_track:
                        ct_url, ct_title, *_ = self.current_track
                        self.queue.appendleft((ct_url, ct_title))
                    elif self.loop_mode == "queue" and self.current_track:
                        ct_url, ct_title, *_ = self.current_track
                        self.queue.append((ct_url, ct_title))

                if ctx.voice_client:
                    # Immer play_next aufrufen – die Queue-leer+Autoplay-Logik liegt dort.
                    fut = asyncio.run_coroutine_threadsafe(
                        self.play_next(ctx), self.bot.loop
                    )

                    def log_exception(f):
                        # Stille Fehler sind gefährlich – lieber laut loggen.
                        if not f.cancelled() and f.exception():
                            logger.error(
                                f"[after_playing] Fehler beim Starten des nächsten Tracks: {f.exception()}"
                            )

                    fut.add_done_callback(log_exception)
                else:
                    logger.info("[KEINE VERBINDUNG] Keine Verbindung. Warte auf !j ...")

            if not ctx.voice_client or not ctx.voice_client.is_connected():
                logger.warning("[play_next] Voice client nicht verbunden – Wiedergabe abgebrochen.")
                self.is_playing = False
                self.queue.appendleft((url, title))
                self._stderr_tail(stderr_buf)   # after_playing läuft nie → Puffer schließen
                return

            # Zweite dm_speaking-Prüfung: Das Auflösen oben (await _resolve_track) kann ein
            # Netzwerk-Call sein – in diesem Fenster könnte die DM-Bridge den Voice-Client
            # übernommen haben. Track zurück an den Anfang der Queue, nicht überspielen.
            if getattr(self.bot, "dm_speaking", False):
                logger.info("[play_next] DM spricht – Track zurückgestellt statt überspielt.")
                self.is_playing = False
                self.queue.appendleft((url, title))
                self._track_generation += 1     # hängende Post-Play-awaits entwerten
                self._stderr_tail(stderr_buf)   # after_playing läuft nie → Puffer schließen
                return

            self._playback_done.clear()
            self._cancel_idle_timer()   # Audio läuft wieder → Idle-Timer weg
            self._last_ctx = ctx        # Kontext für den Reconnect-Watchdog merken
            self._stuck_ticks = 0
            self._stopped_by_user = False

            # Loop-"Song": Derselbe Song läuft erneut → keine neue "Jetzt läuft"-Nachricht
            # posten. Die bestehende Nachricht (samt Buttons) bleibt stehen, wir setzen nur
            # den Fortschrittsbalken auf 0:00 zurück – der Song hat sich ja nicht geändert.
            prev_track = self.current_track
            is_loop_repeat = (
                self.loop_mode == "song"
                and self.now_playing_msg is not None
                and self.now_playing_embed is not None
                and prev_track is not None
                and prev_track[0] == url
            )

            # Kompletten Track-Zustand VOR play() setzen – synchron, ohne await dazwischen.
            # after_playing kann bei einem sofort sterbenden FFmpeg-Prozess feuern, bevor
            # die awaits unten fertig sind; der Leere-Queue-Cleanup liefe dann zuerst und
            # späte Zuweisungen hier würden ihn überschreiben (toter current_track →
            # _progress_loop editiert die Nachricht endlos → Discord-429).
            self._track_generation += 1
            generation = self._track_generation
            self.is_playing = True
            self.last_played = prev_track  # vorherigen Song merken, bevor er überschrieben wird
            self.current_track = (url, title, duration)
            self.track_start_time = time.monotonic()
            self._np_paused_total = 0.0   # Pausen-State für den neuen Song zurücksetzen
            self._np_paused_at = None
            self._np_last_desc = None
            self._recently_played.append(url)
            self._recently_played_titles.append(normalize_title(title))
            if url in (self._stream_retry_url, self._progressive_resume_url):
                # Weiterer Anlauf desselben Tracks (Stream-Retry, Download-
                # Fallback oder progressives Seek-Resume) → nicht doppelt zählen.
                pass
            else:
                self._stream_retry_url = None   # anderer Track → Retry-Sperren aufheben
                self._force_download_url = None
                self._progressive_resume_url = None
                self._progressive_resume_count = 0
                self._record_play(url, title)
            self.text_channel = ctx.channel

            ctx.voice_client.play(source, after=after_playing)
            logger.info(f"[Wiedergabe] Starte: {title}")

            if is_loop_repeat:
                _bar = self._progress_bar(0, duration)
                if _bar and _bar != self._np_last_desc:
                    self.now_playing_embed.description = _bar
                    self._np_last_desc = _bar
                    try:
                        await self.now_playing_msg.edit(embed=self.now_playing_embed)
                    except Exception:
                        pass
            else:
                # Alte Now-Playing-Nachricht auf reinen Text reduzieren – nur der aktuelle
                # Song behält Buttons. Nach Queue-Ende liegt die Nachricht in _ended_np
                # (der Cleanup hat now_playing_msg bereits gekappt).
                old_msg, old_title = None, None
                if self.now_playing_msg:
                    old_msg = self.now_playing_msg
                    old_title = prev_track[1] if prev_track else None
                elif self._ended_np:
                    old_msg, old_title = self._ended_np
                self._ended_np = None
                if old_msg:
                    try:
                        prev_text = f"🎶 {old_title}" if old_title else None
                        await old_msg.edit(content=prev_text, embed=None, view=None)
                    except Exception:
                        pass
                    if generation != self._track_generation:
                        # Track ist während des awaits gestorben, der Cleanup lief schon –
                        # keinen Zustand des toten Durchlaufs mehr anfassen.
                        return
                duration_str = f"{duration // 60}:{duration % 60:02d}" if duration else t("misc.unknown")
                if " - " in title:
                    _artist, _song = title.split(" - ", 1)
                    embed_title = f"🎵 {_song.strip()} – {_artist.strip()}"
                elif info.get("uploader"):
                    embed_title = f"🎵 {title} – {info.get('uploader')}"
                else:
                    embed_title = f"🎵 {title}"
                embed = discord.Embed(title=embed_title, url=info.get("webpage_url"), color=0x1db954)
                _bar = self._progress_bar(0, duration)   # Fortschrittsbalken sofort bei 0:00 anzeigen
                if _bar:
                    embed.description = _bar
                embed.set_thumbnail(url=info.get("thumbnail"))
                embed.add_field(name=t("embed.duration"), value=duration_str, inline=True)
                embed.add_field(name=t("embed.eq"), value=self.equalizer, inline=True)
                embed.add_field(name=t("embed.format"), value=self.audio_format, inline=True)
                if info.get("uploader"):
                    embed.set_footer(text=info.get("uploader"))
                new_msg = await ctx.send(embed=embed, view=MusicControlView(self, ctx, song=(url, title)))
                if generation != self._track_generation:
                    # Track ist während des ctx.send gestorben und der Cleanup lief bereits:
                    # Die frisch gesendete Nachricht NICHT als aktiv registrieren (sonst
                    # editiert der _progress_loop sie endlos), nur ihre Buttons entfernen.
                    try:
                        await new_msg.edit(content=f"🎶 {title}", embed=None, view=None)
                    except Exception:
                        pass
                    return
                self.now_playing_msg = new_msg
                self.now_playing_embed = embed   # Referenz für den Live-Edit im _progress_loop

            if generation != self._track_generation:
                return

            # Alle 50 Songs yt_dlp-Instanzen neu erstellen, damit interne Caches
            # (JS-Signatur-Parser, Format-Metadaten, HTTP-Pool) nicht unbegrenzt wachsen.
            # Im Hintergrund-Thread, damit der Event-Loop nicht blockiert wird.
            self._songs_played += 1
            if self._songs_played % 50 == 0:
                logger.info(f"[Maintenance] {self._songs_played} Songs gespielt – yt_dlp-Instanzen werden neu erstellt.")
                _keep = {url for url, _ in self.queue}
                if self.current_track:
                    _keep.add(self.current_track[0])
                _fmt = self.audio_format
                asyncio.create_task(asyncio.to_thread(self.dl.rebuild, _fmt, _keep))

            # Nächsten Song direkt im Hintergrund vorladen, damit er ohne Wartezeit startet.
            # Alten Prefetch-Task erst abbrechen – sonst laufen mehrere parallel.
            if self.prefetch_task and not self.prefetch_task.done():
                self.prefetch_task.cancel()
            if self.queue:
                self.prefetch_task = asyncio.create_task(self._prefetch_upcoming())

            # Autoplay: nächsten verwandten Song suchen+laden während der aktuelle läuft.
            if self.autoplay_enabled and not self.queue:
                if self._autoplay_prefetch_task and not self._autoplay_prefetch_task.done():
                    self._autoplay_prefetch_task.cancel()
                self._autoplay_prefetch_task = asyncio.create_task(self._prefetch_autoplay(ctx))

        except asyncio.TimeoutError:
            self._stderr_tail(stderr_buf)   # Puffer schließen falls schon angelegt
            self._recently_played.append(url)
            self._recently_played_titles.append(normalize_title(title))
            if ctx.voice_client and ctx.voice_client.is_connected():
                try:
                    await ctx.send(t("error.track_timeout", title=title))
                except Exception:
                    pass
                asyncio.create_task(self.play_next(ctx))
            else:
                logger.warning("[play_next] TimeoutError – Voice client weg, kein Retry.")
                self.is_playing = False
            return
        except Exception:
            logger.exception("[Fehler bei play_next]")
            self._stderr_tail(stderr_buf)   # Puffer schließen falls schon angelegt
            self._recently_played.append(url)
            self._recently_played_titles.append(normalize_title(title))
            if ctx.voice_client and ctx.voice_client.is_connected():
                try:
                    await ctx.send(t("error.track_error", title=title))
                except Exception:
                    pass
                # Fehlerhaften Track überspringen – create_task statt direkter Rekursion,
                # damit bei vielen schlechten URLs der Call-Stack nicht überfüllt wird.
                asyncio.create_task(self.play_next(ctx))
            else:
                logger.warning("[play_next] Exception – Voice client weg, kein Retry.")
                self.is_playing = False
            return

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

    def _evict_autoplay_song(self) -> str | None:
        """Entfernt den von Autoplay vorgereihten Song aus der Queue (falls vorhanden).

        Bricht außerdem einen laufenden Prefetch-Task ab, damit kein weiterer
        Autoplay-Song nachgeschoben wird bevor der manuelle !p-Song gespielt hat.
        Gibt den Titel des entfernten Songs zurück (für Logging), sonst None.
        """
        # Laufenden Prefetch sofort stoppen
        if self._autoplay_prefetch_task and not self._autoplay_prefetch_task.done():
            self._autoplay_prefetch_task.cancel()
            self._autoplay_prefetch_task = None

        if not self._autoplay_queued_url:
            return None

        removed_title = None
        for i, (queued_url, queued_title) in enumerate(self.queue):
            if queued_url == self._autoplay_queued_url:
                del self.queue[i]
                removed_title = queued_title
                self._autoplay_queued_url = None
                break
        return removed_title

    async def _stop_radio_for_takeover(self, ctx):
        """Beendet einen laufenden Radio-Stream, bevor ein Song übernimmt.

        Gemeinsamer Vorspann von !p, !next und !now: Radio stoppen und auf das
        Ende der laufenden Wiedergabe warten (max. 3 s), damit der neue Song
        nicht mit dem Radio-Callback kollidiert.
        """
        if not self.is_radio:
            return
        self._stop_radio()
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.stop()
            try:
                await asyncio.wait_for(self._playback_done.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass
            self._playback_done.clear()

    async def _extract_info_or_report(self, ctx, query, ydl_instance, *,
                                      status_key, timeout_key, error_key, log_msg):
        """Ruft yt_dlp-Infos ab und meldet Timeout/Fehler direkt im Channel.

        Gemeinsamer Fetch-Teil der Such- und URL-Zweige von !p, !next und !now.
        Gibt das Info-Dict zurück (kann auch None sein, wenn yt_dlp nichts
        liefert – damit gehen die Aufrufer wie bisher selbst um), oder
        _YTDLP_FAILED wenn bereits eine Fehlermeldung gesendet wurde.

        Die Status-Nachricht ("Suche läuft..." bzw. "Verarbeite...") wird nach
        Abschluss wieder gelöscht – bei Erfolg wie im Fehlerfall (dann steht
        die Fehlermeldung im Channel).
        """
        status_msg = None
        try:
            status_msg = await ctx.send(t(status_key))
            return await asyncio.wait_for(
                asyncio.to_thread(ydl_instance.extract_info, query, download=False),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            await ctx.send(t(timeout_key))
            return _YTDLP_FAILED
        except Exception:
            logger.exception(log_msg)
            await ctx.send(t(error_key))
            return _YTDLP_FAILED
        finally:
            if status_msg is not None:
                try:
                    await status_msg.delete()
                except Exception:
                    pass   # schon weg oder keine Berechtigung – egal

    async def _search_and_enqueue(self, ctx, eingabe, *, log_tag, timeout_key,
                                  insert, with_alts_key, no_alts_key,
                                  base_content_key=None):
        """Gemeinsamer Suchbegriff-Zweig von !p, !next und !now.

        ytsearch3 → ersten Treffer vorab auflösen und einreihen, Treffer 2
        und 3 als Buttons anzeigen (SearchAutoplayView) falls es der Falsche
        war. Die drei Befehle unterscheiden sich bewusst – die Unterschiede
        stecken in den Parametern:

        - !p:    insert="evict_or_back" – wenn Autoplay einen Song vorgemerkt
                 hat, fliegt der raus (nur bei aktiviertem Autoplay) und der
                 manuelle Wunsch landet VORN in der Queue, sonst hinten.
                 Die Eviction wird geloggt.
        - !next: insert="front" – keine Eviction, Treffer immer vorn;
                 eigene Meldungs-Keys (next_added/next_with_alts) und
                 base_content_key für die View.
        - !now:  insert="evict_front" – Eviction immer (Rückgabe ignoriert),
                 Treffer vorn; die laufende Wiedergabe stoppt der Aufrufer
                 danach selbst.

        Gibt True zurück wenn ein Treffer eingereiht wurde, sonst False
        (die Fehlermeldung wurde dann bereits gesendet).
        """
        results = await self._extract_info_or_report(
            ctx, f"ytsearch3:{eingabe}", self.dl.search_ydl,
            status_key="status.searching", timeout_key=timeout_key,
            error_key="error.search_error",
            log_msg=f"[{log_tag}] Fehler bei Suche",
        )
        if results is _YTDLP_FAILED:
            return False

        entries = (results.get("entries") or [])[:3]
        if not entries:
            await ctx.send(t("error.no_results"))
            return False

        first = entries[0]
        url = first.get("webpage_url") or first.get("url")
        title = first.get("title", t("misc.unknown_title"))
        asyncio.create_task(self.dl._start_resolve(url))

        if insert == "evict_or_back":
            evicted = self._evict_autoplay_song() if self.autoplay_enabled else None
            if evicted:
                logger.info(f"[{log_tag}] Autoplay-Song verdrängt: {evicted}")
                self.queue.appendleft((url, title))
            else:
                self.queue.append((url, title))
        elif insert == "evict_front":
            self._evict_autoplay_song()
            self.queue.appendleft((url, title))
        else:  # "front"
            self.queue.appendleft((url, title))

        # Alternativen (Treffer 2 und 3) als Buttons anzeigen
        alternatives = entries[1:]
        if alternatives:
            view = SearchAutoplayView(
                first, alternatives, self, ctx,
                base_content=t(base_content_key, title=title) if base_content_key else None,
            )
            letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            alt_lines = "\n".join(
                t("misc.option_line", letter=letters[i], title=e.get("title", t("misc.unknown_title")))
                for i, e in enumerate(alternatives)
            )
            msg = await ctx.send(
                t(with_alts_key, title=title, alts=alt_lines),
                view=view,
            )
            view.message = msg
        else:
            await ctx.send(t(no_alts_key, title=title))
        return True

    async def _fetch_single_track_info(self, ctx, eingabe, log_tag):
        """Gemeinsamer Direkt-URL-Zweig von !next und !now (ohne Playlists).

        Löst die URL via url_ydl auf; Playlists werden abgelehnt
        (error.next_no_playlist). Gibt (url, title) zurück oder None, wenn
        bereits eine Fehlermeldung gesendet wurde. !p behält seinen eigenen
        URL-Zweig: Playlist-Unterstützung, Duplikat-Warnung und andere
        Meldungs-Keys.
        """
        info = await self._extract_info_or_report(
            ctx, eingabe, self.dl.url_ydl,
            status_key="status.processing_url", timeout_key="error.timeout",
            error_key="error.fetch_error",
            log_msg=f"[{log_tag}] Fehler beim Abrufen von yt_dlp-Infos",
        )
        if info is _YTDLP_FAILED:
            return None

        if "entries" in info:
            await ctx.send(t("error.next_no_playlist"))
            return None

        url = info.get("webpage_url") or eingabe
        title = info.get("title", t("misc.unknown_title"))
        return (url, title)

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
        ydl_instance = self.dl.playlist_ydl if is_playlist else self.dl.url_ydl

        info = await self._extract_info_or_report(
            ctx, eingabe, ydl_instance,
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
                ctx.voice_client.stop()
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
                ctx.voice_client.stop()
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
            await ctx.send(t("status.paused"))
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
            await ctx.send(t("status.resumed"))
            self.is_playing = True
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
        """Zeigt die aktuelle Queue als Embed mit Blätter-Buttons (15 Tracks pro Seite)."""
        from views.queue_view import QueueView
        queue_snapshot = [
            (url, title, self.dl._url_cache[url].get("duration") if url in self.dl._url_cache else None)
            for url, title in self.queue
        ]
        view = QueueView(queue_snapshot, self.current_track, self.loop_mode)
        await ctx.send(embed=view.build_embed(), view=view)

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
            presets = ", ".join(self.eq_presets.keys())
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

    @staticmethod
    def _parse_time(s: str):
        """Parst '1:23' → 83 oder '83' → 83. Gibt None bei ungültiger Eingabe zurück."""
        s = s.strip()
        if ":" in s:
            parts = s.split(":", 1)
            try:
                return int(parts[0]) * 60 + int(parts[1])
            except ValueError:
                return None
        try:
            return int(s)
        except ValueError:
            return None

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

