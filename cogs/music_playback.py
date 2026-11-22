"""Playback-Pipeline für MusicCommands.

PlaybackMixin — das Herzstück der Wiedergabe, per Mehrfachvererbung in
MusicCommands eingebunden: der progressive Download-/FFmpeg-Playback-Kern
(play_next samt nested after_playing-Callback), das Prefetch-Subsystem
(_prefetch_next/_prefetch_upcoming/_kick_prefetch/_prefetch_autoplay), der
gemeinsame Such-/URL-Resolve-Flow von !p/!next/!now (_extract_info_or_report/
_search_and_enqueue/_fetch_single_track_info) sowie die Advance-/Resolve-Helfer
_stop_for_advance/_resolve_track und der Autoplay-Evictor _evict_autoplay_song.

Alle Methoden greifen auf denselben Instanz-State zu, der in cogs/music.py
angelegt und verwaltet wird (u. a. self.queue, self.current_track,
self._track_generation, self._playback_done, self.is_playing, self.is_radio,
self._seek_offset, self._suppress_resume, self._progressive_resume_count,
self.now_playing_msg/now_playing_view, self.prefetch_task,
self._autoplay_prefetch_task, self._autoplay_queued_url) sowie auf den
Downloader-Cog (self.dl) inklusive dessen In-Flight-Registry und auf die in
cogs/music.py verbliebenen Klassen-Aliase self._ffmpeg_header_opts/
_stderr_tail/_classify_ffmpeg_error/_progress_bar.

Die ADR-0007-Sequenzierung (Track-Zustand vor vc.play(), Generationscheck nach
jedem Post-Play-await, _stop_for_advance als einziger Stopp-Pfad für Musik,
Retry-Reset NACH dem Progressive-Resume-Block in after_playing) ist hier
unverändert erhalten – nichts an der Reihenfolge wurde umgestellt.
"""

import asyncio
import concurrent.futures
import json
import tempfile
import threading
import time

import discord

from utils.files import atomic_write_text
from utils.logger import logger
from utils.tasks import spawn
from utils.voice import PlayerToken, start_clock_at_first_packet, stop_playback
from utils.i18n import t
from utils.text import link_label, normalize_title
from utils.ffmpeg import stream_wait_seconds
from views.music_controls import MusicControlView
from cogs.music_enqueue import EnqueueMixin
from cogs.music_prefetch import PrefetchMixin

# Max. Seek-Resumes pro Track, wenn FFmpeg die wachsende Datei eines progressiven
# Downloads überholt hat – danach Fallback auf die Stream-/Retry-Kaskade.
PROGRESSIVE_MAX_RESUMES = 2

# So lange wartet der Player-Thread höchstens darauf, dass der Event-Loop die
# Track-Ende-Logik übernimmt (normal: Millisekunden).
_LOOP_HANDOFF_TIMEOUT = 10.0

# Serialisiert die last_queue.json-Writes der Player-Threads (_write_last_queue).
_LAST_QUEUE_LOCK = threading.Lock()


def _run_on_loop(loop, fn, *args):
    """Führt fn(*args) auf dem Event-Loop `loop` aus und gibt das Ergebnis zurück.

    Aus einem fremden Thread (Player-Thread von discord.py): an den Loop
    übergeben und auf das Ergebnis warten – eine Exception aus fn kommt beim
    Aufrufer an, wie wenn er fn selbst ausgeführt hätte. Nach
    _LOOP_HANDOFF_TIMEOUT Sekunden concurrent.futures.TimeoutError.
    Wer schon auf dem Loop ist, ruft fn direkt auf (Warten wäre ein Deadlock).
    """
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        return fn(*args)
    done = concurrent.futures.Future()

    def run():
        try:
            done.set_result(fn(*args))
        except BaseException as exc:
            done.set_exception(exc)

    loop.call_soon_threadsafe(run)
    return done.result(timeout=_LOOP_HANDOFF_TIMEOUT)


class PlaybackMixin(PrefetchMixin, EnqueueMixin):
    # URL des Tracks, für den _seek_offset gesetzt wurde (!seek, Progressive-
    # Resume, Voice-Reparatur). None = ungebunden. Klassen-Default, damit auch
    # ohne __init__ erzeugte Instanzen (Tests via __new__) definiert sind.
    _seek_offset_url = None
    # Token des zuletzt gestarteten Players (Song oder Radio) – siehe
    # utils.voice.PlayerToken und _discard_paused_player.
    _player_token = None

    def _take_seek_offset(self, url: str) -> int:
        """Konsumiert den anstehenden Seek-Offset für den gerade aus der Queue
        genommenen Track. Der Offset gilt nur für den Track, für den er gesetzt
        wurde: er wird hier in jedem Fall gelöscht (auch wenn der Resolve danach
        scheitert oder der Track übersprungen wird) und nur zurückgegeben, wenn
        er zu dieser URL gehört."""
        offset, self._seek_offset = self._seek_offset, 0
        bound_url, self._seek_offset_url = self._seek_offset_url, None
        if bound_url is not None and bound_url != url:
            return 0
        return offset

    def _stop_for_advance(self, voice_client):
        """Wiedergabe-Stopp für absichtliche Wechsel (Skip, !now, !eq, !seek,
        !stop, !clear, Auto-Leave). Setzt das One-Shot-Flag _suppress_resume,
        damit after_playing den Stopp nicht als vorzeitiges Dateiende einer
        wachsenden Datei missdeutet und den Track wieder vorn einreiht.

        Stoppt über utils.voice.stop_playback, nicht über vc.stop(): bei
        aktiver Sprachsteuerung würde ein rohes stop() auch den Audio-Empfang
        beenden."""
        self._suppress_resume = True
        stop_playback(voice_client)

    def _discard_paused_player(self, voice_client):
        """Stoppt einen pausierten Player, unmittelbar bevor play_next oder
        _play_radio_stream einen neuen startet (!x, dann
        !p/!replay/!loadq/!radio/Buttons). Ohne diesen Stopp
        ersetzt discord.py den pausierten Player nur – Thread und FFmpeg-Prozess
        leaken.

        Der Player wird vorher als verworfen markiert: sein after-Callback
        reiht den alten Track nicht wieder ein, löst keinen zweiten play_next
        aus und meldet kein Wiedergabe-Ende. Bewusst nicht über
        _stop_for_advance – das One-Shot-Flag _suppress_resume würde sonst
        vom falschen Callback konsumiert oder in den neuen Track leaken."""
        token = self._player_token
        if token is not None:
            token.discarded = True
        stop_playback(voice_client)

    async def _resolve_track(self, url: str, title: str, force_download: bool = False, seek_offset: int = 0):
        """Löst URL auf, stellt sicher dass Audiodatei lokal vorliegt.

        Returns: (info, filename, title, duration)
        """
        # seek_offset ist das anstehende -ss dieses Tracks (play_next hat es
        # beim Entnehmen aus der Queue konsumiert). Der progressive Pfad
        # puffert entsprechend mehr, damit die Bytes bis zum Seek-Ziel in der
        # wachsenden Datei existieren.
        return await self.dl.resolve_track(
            url, title,
            force_download=force_download, min_buffer_seconds=seek_offset,
        )

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
        # Voice-Reparatur baut gerade die Verbindung neu auf (music_voice_ui.py) –
        # das after_playing des alten Clients darf nicht den nächsten Song
        # starten; die Reparatur ruft play_next danach selbst auf.
        if self._voice_healing:
            logger.info("[play_next] Voice-Reparatur läuft – Auto-Advance unterdrückt.")
            self._track_generation += 1
            return
        if not self.queue:
            # Kompletter Leere-Queue-Fall (Cleanup, _ended_np, Idle-Timer,
            # Autoplay-Handoff) → _handle_queue_empty. Die dortigen await-Grenzen
            # liegen an exakt denselben Stellen wie zuvor inline (ADR 0009).
            await self._handle_queue_empty(ctx)
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
        # Seek-Offset sofort konsumieren: er gehört genau diesem Track. Bliebe
        # er bis nach dem Resolve stehen, erbte ihn bei einem Resolve-Fehler
        # oder im _skip_resolving-Pfad der nächste Track.
        seek_offset = self._take_seek_offset(url)

        stderr_buf = None
        try:
            # Dritter Anlauf nach zwei toten Stream-Versuchen → Download erzwingen.
            force_download = url == self._force_download_url
            info, filename, title, duration = await self._resolve_track(
                url, title, force_download=force_download, seek_offset=seek_offset
            )

            # Stream-URL erst ab available_at abrufbar, sonst 403 (utils/ffmpeg.py).
            # Vor dem _skip_resolving-Check, damit ein Skip im Wartefenster greift.
            wait = stream_wait_seconds(info) if isinstance(filename, str) and not force_download else 0
            if wait > 0:
                logger.info(f"[Stream] Warte {wait:.1f}s bis YouTube die URL freigibt: {title}")
                await asyncio.sleep(wait)

            if self._skip_resolving:
                self._skip_resolving = False
                await self.play_next(ctx)
                return

            eq_filter = self.eq_presets.get(self.equalizer, "")
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
                spawn(self.play_next(ctx), "play_next")
                return

            # FFmpeg-Options, stderr-Temp-Datei und Source-Konstruktion (synchron).
            source, stderr_buf = self._build_audio_source(
                info, filename, seek_offset, eq_filter, is_stream, is_growing
            )
            # after_playing-Callback bauen: die vormaligen Closure-Variablen sind
            # jetzt Factory-Parameter, die interne Reihenfolge bleibt unverändert.
            token = PlayerToken()
            after_playing = self._make_after_playing(
                ctx, stderr_buf, seek_offset, is_stream, is_growing, prog_task, filename, duration, url, title,
                token,
            )

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

            # Kompletten Track-Zustand synchron VOR vc.play() setzen (ADR 0007);
            # gibt Generationszähler + Loop-Repeat-Flag + vorherigen Track zurück.
            generation, is_loop_repeat, prev_track = self._snapshot_track_state(ctx, url, title, duration)
            self._current_seek_offset = seek_offset

            # Pausierter Player (!x, dann neuer Start): discord.py würde ihn in
            # play() nur ersetzen, nicht stoppen. Synchron, direkt vor play().
            if ctx.voice_client.is_paused():
                self._discard_paused_player(ctx.voice_client)
            ctx.voice_client.play(source, after=after_playing)
            self._player_token = token   # erst nach geglücktem play(): gehört zum laufenden Player
            logger.info(f"[Wiedergabe] Starte: {title}")

            if is_loop_repeat:
                _bar = self._progress_bar(0, duration)
                if _bar and _bar != self._np_last_desc:
                    self.now_playing_view.set_body(_bar)
                    self.now_playing_view.sync_state(up_next=self._up_next_title())
                    self._np_last_desc = _bar
                    try:
                        await self.now_playing_msg.edit(view=self.now_playing_view)
                    except Exception:
                        pass
            else:
                # Alte Now-Playing-Nachricht(en) auf reinen Text reduzieren – nur der
                # aktuelle Song behält Karte und Buttons. Nach Queue-Ende liegt die
                # Nachricht in _ended_np (der Cleanup hat now_playing_msg gekappt);
                # beide Slots können gleichzeitig belegt sein – dann müssen auch beide
                # zurückgebaut werden, sonst bliebe eine Karte für immer stehen.
                old_msg, old_title = self.now_playing_msg, self._np_title
                ended_np, self._ended_np = self._ended_np, None
                if old_msg or ended_np:
                    await self._retire_np_message(old_msg, old_title)
                    if ended_np:
                        await self._retire_np_message(*ended_np)
                    if generation != self._track_generation:
                        # Track ist während des awaits gestorben, der Cleanup lief schon –
                        # keinen Zustand des toten Durchlaufs mehr anfassen.
                        return
                view = self._build_np_view(ctx, info, url, title, duration)
                new_msg = await ctx.send(view=view)
                if generation != self._track_generation:
                    # Track ist während des ctx.send gestorben und der Cleanup lief bereits:
                    # Die frisch gesendete Nachricht NICHT als aktiv registrieren (sonst
                    # editiert der _progress_loop sie endlos), nur ihre Buttons entfernen.
                    await self._retire_np_message(new_msg, title)
                    return
                self.now_playing_msg = new_msg
                self.now_playing_view = view    # Referenz für den Live-Edit im _progress_loop
                self._np_title = title           # Titel für den späteren Rückbau der Karte

            if generation != self._track_generation:
                return

            self._schedule_background_work(ctx)

        except asyncio.TimeoutError:
            await self._skip_broken_track(ctx, url, title, stderr_buf, "error.track_timeout", "TimeoutError")
            return
        except Exception:
            logger.exception("[Fehler bei play_next]")
            await self._skip_broken_track(ctx, url, title, stderr_buf, "error.track_error", "Exception")
            return

    def _build_np_view(self, ctx, info, url, title, duration):
        """Now-Playing-Karte (Components V2) eines frisch gestarteten Tracks
        (synchron, ohne Nebenwirkung)."""
        duration_str = f"{duration // 60}:{duration % 60:02d}" if duration else t("misc.unknown")
        if " - " in title:
            _artist, _song = title.split(" - ", 1)
            card_title = f"{_song.strip()} – {_artist.strip()}"
        elif info.get("uploader"):
            card_title = f"{title} – {info.get('uploader')}"
        else:
            card_title = title
        page = info.get("webpage_url")
        # Link-Text ohne Emojis: sonst zeigt Discord [Titel](<url>) als Rohtext
        # statt als Link (utils/text.link_label). Nur-Emoji-Titel → ohne Link.
        label = link_label(card_title)
        if page and label:
            heading = f"### 🎵 [{label}](<{page}>)"
        else:
            heading = f"### 🎵 {discord.utils.escape_markdown(card_title)}"
        details = (
            f"**{t('embed.duration')}** {duration_str} · **{t('embed.eq')}** {self.equalizer}"
            f" · **{t('embed.format')}** {self.audio_format}"
        )
        uploader = info.get("uploader")
        return MusicControlView(
            self, ctx, song=(url, title),
            heading=heading,
            body=self._progress_bar(0, duration),   # Fortschrittsbalken sofort bei 0:00 anzeigen
            details=details,
            footer=discord.utils.escape_markdown(uploader) if uploader else None,
            up_next=self._up_next_title(),
            thumbnail=info.get("thumbnail"),
            link=page or url,   # 🔗-Button: derselbe Link wie am Kartentitel
        )

    def _schedule_background_work(self, ctx):
        """Nach einem erfolgreichen Start: Wartung, Prefetch, Autoplay-Prefetch.
        Synchron – nur Tasks anstoßen; der Generationscheck davor bleibt im
        Orchestrator (ADR 0007/0009)."""
        # Alle 50 Songs yt_dlp-Instanzen neu erstellen, damit interne Caches
        # (JS-Signatur-Parser, Format-Metadaten, HTTP-Pool) nicht unbegrenzt wachsen.
        # Gebaut wird im Hintergrund-Thread, damit der Event-Loop nicht blockiert;
        # Cache-Bereinigung und Instanz-Tausch macht rebuild_async auf dem Loop.
        self._songs_played += 1
        if self._songs_played % 50 == 0:
            logger.info(f"[Maintenance] {self._songs_played} Songs gespielt – yt_dlp-Instanzen werden neu erstellt.")
            _keep = {url for url, _ in self.queue}
            if self.current_track:
                _keep.add(self.current_track[0])
            _fmt = self.audio_format
            spawn(self.dl.rebuild_async(_fmt, _keep), "yt_dlp-Rebuild")

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

    async def _skip_broken_track(self, ctx, url, title, stderr_buf, error_key, kind):
        """Track ließ sich nicht starten: merken (kein Autoplay-Rückfall darauf),
        melden und den nächsten anstoßen – create_task statt Rekursion, damit
        viele kaputte URLs den Call-Stack nicht füllen."""
        self._stderr_tail(stderr_buf)   # Puffer schließen falls schon angelegt
        self._recently_played.append(url)
        self._recently_played_titles.append(normalize_title(title))
        if ctx.voice_client and ctx.voice_client.is_connected():
            try:
                await ctx.send(t(error_key, title=title))
            except Exception:
                pass
            spawn(self.play_next(ctx), "play_next")
        else:
            logger.warning(f"[play_next] {kind} – Voice client weg, kein Retry.")
            self.is_playing = False

    async def _handle_queue_empty(self, ctx):
        """Leere-Queue-Fall aus play_next: Cleanup, _ended_np-Übergabe, Idle-Timer,
        Autoplay-Handoff (inkl. Prefetch-Wait).

        Verbatim aus play_next herausgezogen (ADR 0009). Enthält Post-Play-await-
        Grenzen (_finalize_progress_bar, Prefetch-Wait, play_next/autoplay) an
        exakt denselben Stellen wie zuvor inline – der Aufrufer awaitet diese
        Methode direkt und returnt danach.
        """
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
            self._ended_np = (self.now_playing_msg, self._np_title)
        self.now_playing_msg = None
        self.now_playing_view = None
        self._np_title = None
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

    def _build_audio_source(self, info, filename, seek_offset, eq_filter, is_stream, is_growing):
        """Baut FFmpeg-Options, legt die stderr-Temp-Datei an und konstruiert die
        Audio-Source. Synchron; gibt (source, stderr_buf) zurück.

        Verbatim aus play_next herausgezogen (ADR 0009). is_stream/is_growing sind
        der Wachsende-Datei-Snapshot vom Startzeitpunkt und werden vom Orchestrator
        gereicht (er braucht sie auch für die Giveup-Prüfung und after_playing).
        """
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
        # Uhr des Players erst beim ersten Paket: sonst schiebt er FFmpegs
        # Anlaufzeit als Ton ohne Pause nach (Anfang klingt kurz zu schnell).
        return start_clock_at_first_packet(source), stderr_buf

    def _make_after_playing(self, ctx, stderr_buf, seek_offset, is_stream, is_growing, prog_task, filename, duration, url, title,
                            token=None):
        """Factory für den after_playing-Callback (ADR 0009).

        Der Callback-Body ist verbatim aus play_next übernommen; die vormaligen
        Closure-Variablen (stderr_buf, seek_offset, is_stream, is_growing,
        prog_task, filename, duration, url, title, ctx) sind jetzt Factory-
        Parameter. Die
        interne Reihenfolge – Progressive-Resume-Block VOR dem Retry-/Resume-
        Marker-Reset – bleibt unangetastet. token: PlayerToken dieses Players
        (siehe _discard_paused_player).
        """
        def on_track_end(error, ended_at, stderr_tail):
            """Die Track-Ende-Logik. Läuft auf dem Event-Loop (after_playing
            unten reicht sie aus dem Player-Thread dorthin weiter): Queue,
            _seek_offset und die Retry-Marker werden nur noch dort verändert,
            wo auch !remove/!move/!shuffle die Queue umbauen.

            Reihenfolge wie zuvor im Thread-Callback (ADR 0007/0009). Die
            run_coroutine_threadsafe-Aufrufe sind bewusst geblieben – sie
            funktionieren auch vom Loop aus und verhalten sich exakt wie bisher.
            Gibt (Folgenummer, Queue-Snapshot) für den last_queue.json-Write
            zurück, den der Aufrufer außerhalb des Loops erledigt.
            """
            self._playback_done.set()
            # One-Shot: Flag immer konsumieren, damit ein absichtlicher Stopp
            # nicht in den after_playing-Lauf eines späteren Tracks leakt.
            suppress_resume, self._suppress_resume = self._suppress_resume, False
            if error:
                logger.warning(f"[Fehler beim Abspielen] {error}")
            # ended_at: Zeitpunkt des Callbacks im Player-Thread – die Laufzeit
            # hängt damit nicht davon ab, wie schnell der Loop übernimmt.
            elapsed = ended_at - self.track_start_time if self.track_start_time else 999
            # stderr_tail hat der Thread-Callback bereits ausgelesen (schließt
            # den Puffer); geloggt wird nur im Fehlerfall.
            verdict = self._classify_ffmpeg_error(stderr_tail)
            # Gewollte Abbrüche (Skip/!stop/!x in den ersten 2 s) sind kein Fehler –
            # keine gelbe stderr-Warnung für etwas, das der Nutzer selbst ausgelöst hat.
            user_abort = suppress_resume or self._stopped_by_user
            if error or (elapsed < 2.0 and not user_abort):
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
                    self._seek_offset_url = url
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
            # Hier nur der Snapshot (an derselben Stelle wie bisher, vor der
            # Loop-Logik); geschrieben wird außerhalb des Event-Loops.
            queue_data = list(self.queue)
            self._last_queue_seq += 1
            queue_seq = self._last_queue_seq

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
            return queue_seq, queue_data

        def after_playing(error):
            """Callback, den discord.py nach jedem Track aufruft – im
            Player-Thread. Er reicht nur noch weiter: die gesamte Logik
            (on_track_end) läuft auf dem Event-Loop.

            Im Thread bleibt ausschließlich Datei-I/O, das nicht auf den Loop
            gehört (ADR 0005): der FFmpeg-stderr-Puffer davor, der
            last_queue.json-Write danach.
            """
            if token is not None and token.discarded:
                # Dieser (pausierte) Player wurde von play_next durch einen neuen
                # Track ersetzt: der Wiedergabe-Zustand gehört längst dem neuen
                # Track. Nur den eigenen stderr-Puffer schließen – kein
                # _playback_done, kein Requeue, kein play_next.
                self._stderr_tail(stderr_buf)
                logger.info(f"[Wiedergabe] Pausierter Song durch neuen Start ersetzt: {title}")
                return
            ended_at = time.monotonic()
            # stderr immer auslesen (schließt den Puffer).
            stderr_tail = self._stderr_tail(stderr_buf)
            try:
                queue_seq, queue_data = _run_on_loop(self.bot.loop, on_track_end, error, ended_at, stderr_tail)
            except concurrent.futures.TimeoutError:
                # Der Loop hat nicht übernommen (blockiert oder beim Beenden
                # gestoppt). Die Logik läuft nach, sobald er wieder dran ist –
                # hier nur den Player-Thread nicht länger festhalten.
                logger.warning(
                    f"[after_playing] Event-Loop hat das Track-Ende nicht innerhalb von "
                    f"{_LOOP_HANDOFF_TIMEOUT:.0f}s übernommen: {title}"
                )
                return
            self._write_last_queue(queue_seq, queue_data)

        return after_playing

    # Folgenummern für last_queue.json (Klassen-Defaults für Tests via __new__):
    # vergeben auf dem Loop beim Snapshot, geprüft beim Schreiben im Thread.
    _last_queue_seq = 0
    _last_queue_written = 0

    def _write_last_queue(self, seq, queue_data):
        """Schreibt den Queue-Snapshot eines Track-Endes nach last_queue.json.

        Blockierendes Datei-I/O – läuft im Player-Thread (aus after_playing),
        nicht auf dem Event-Loop. Der Write passiert jetzt nach dem Anstoßen
        des nächsten Tracks; stirbt der sofort, könnten sich zwei Writes
        überholen. Lock + Folgenummer sorgen dafür, dass nie ein älterer
        Snapshot einen neueren überschreibt."""
        with _LAST_QUEUE_LOCK:
            if seq < self._last_queue_written:
                return
            try:
                atomic_write_text("last_queue.json", json.dumps(queue_data, ensure_ascii=False, indent=2))
                self._last_queue_written = seq
                logger.info("[SAVE] Queue automatisch gespeichert nach Track-Ende.")
            except Exception as e:
                logger.warning(f"[SAVE] Fehler beim Speichern der Queue: {e}")

    def _snapshot_track_state(self, ctx, url, title, duration):
        """Setzt den kompletten Track-Zustand synchron VOR vc.play() (ADR 0007).

        Verbatim aus play_next herausgezogen (ADR 0009). **Muss synchron bleiben**
        – kein await zwischen den Zuweisungen. Gibt (generation, is_loop_repeat,
        prev_track) zurück; der Orchestrator braucht sie für die Post-Play-
        Generationschecks und den Now-Playing-Nachrichtenbau.
        """
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
            and self.now_playing_view is not None
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
        return generation, is_loop_repeat, prev_track
