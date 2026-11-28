"""Wiedergabe-Kern für MusicCommands.

PlaybackMixin enthält play_next und seine Untermethoden: Gates, Resolve,
FFmpeg-Source, Track-Zustand, Now-Playing-Karte, Hintergrundarbeit und den
Fehlerpfad. Dazu die zwei Stopp-Pfade _stop_for_advance und
_discard_paused_player. Über die Basisklassen kommen das Track-Ende
(music_track_end.py), das Anstoßen des Prefetch (music_prefetch.py) und der
Such-/Einreih-Flow von !p/!next/!now (music_enqueue.py) dazu.

Alle Methoden arbeiten auf dem Instanz-State, den cogs/music.py anlegt
(u. a. self.queue, self.current_track, self._track_generation,
self._playback_done, self.is_playing, self.is_radio, self._seek_offset,
self._suppress_resume, self.now_playing_msg/now_playing_view), auf dem
Downloader (self.dl) und auf den Klassen-Aliasen aus cogs/music.py
(self._ffmpeg_header_opts, _stderr_tail, _classify_ffmpeg_error,
_progress_bar).

Die Reihenfolge ist hier Invariante (ADR 0007): der komplette Track-Zustand
wird synchron vor vc.play() gesetzt, nach jedem await danach prüft play_next
den Generationszähler, und Musik wird nur über _stop_for_advance gestoppt.
"""

import asyncio
import tempfile
import time

import discord

from utils.logger import logger
from utils.tasks import spawn
from utils.voice import PlayerToken, start_clock_at_first_packet, stop_playback
from utils.i18n import t
from utils.text import normalize_title
from utils.ffmpeg import stream_wait_seconds
from views.music_controls import MusicControlView, np_heading
from cogs.music_enqueue import EnqueueMixin
from cogs.music_prefetch import PrefetchMixin, _cancel
from cogs.music_track_end import TrackEndMixin, TrackRun


class PlaybackMixin(TrackEndMixin, PrefetchMixin, EnqueueMixin):
    # URL des Tracks, für den _seek_offset gesetzt wurde (!seek, Progressive-
    # Resume, Voice-Reparatur). None = ungebunden. Klassen-Default, damit auch
    # ohne __init__ erzeugte Instanzen (Tests via __new__) definiert sind.
    _seek_offset_url = None
    # URL des Tracks, der nur neu angesetzt wird (!seek, !eq, Voice-Reparatur):
    # derselbe Hördurchgang, kein weiterer Play-Count. One-Shot, gebunden an
    # die URL und beim Entnehmen aus der Queue konsumiert (_take_restart).
    _restart_url = None
    # Token des zuletzt gestarteten Players (Song oder Radio) – siehe
    # utils.voice.PlayerToken und _discard_paused_player.
    _player_token = None
    # URL des Tracks, den play_next gerade auflöst (None = kein Resolve läuft).
    # Die Alternativ-Buttons setzen _skip_resolving nur, wenn genau ihr Treffer
    # hier steht; play_next verbraucht den Wunsch am Ende dieses Resolves.
    _resolving_url = None

    async def _start_or_prefetch(self, ctx):
        """Nach dem Einreihen (!p, !next): Läuft nichts, die Wiedergabe
        starten. Läuft schon Musik, die neuen Titel sofort vorladen."""
        if not self.is_playing:
            self.is_playing = True
            await self.play_next(ctx)
        else:
            self._kick_prefetch()

    async def play_next(self, ctx):
        """Spielt den nächsten Song in der Queue. Wird rekursiv nach jedem Track aufgerufen."""
        reason = self._advance_block_reason()
        if reason:
            logger.info(f"[play_next] {reason}")
            # Hängende Post-Play-awaits des unterbrochenen Durchlaufs dürfen
            # keinen Zustand mehr schreiben (gleicher Race-Typ wie im
            # Leere-Queue-Cleanup).
            self._track_generation += 1
            return
        if not self.queue:
            # Cleanup, _ended_np, Idle-Timer, Autoplay-Handoff.
            await self._handle_queue_empty(ctx)
            return

        # Guard: verhindert die "Already playing audio"-Kaskade bei gleichzeitigen
        # play_next-Aufrufen (z.B. after_playing-Callback + Error-Handler-create_task).
        # Song NICHT aus der Queue nehmen – erst nach dem Check.
        # Kurzes Warten: nach einem Reconnect kann FFmpeg noch im Cleanup sein (exit-code 1)
        # obwohl after_playing bereits ausgelöst wurde – is_playing() wäre dann fälschlicherweise True.
        if ctx.voice_client and ctx.voice_client.is_playing():
            if not await self._wait_playback_done():
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
        # Dasselbe für die Neustart-Markierung (kein zweiter Play-Count).
        is_restart = self._take_restart(url)

        stderr_buf = None
        source = None   # die FFmpeg-Quelle, solange vc.play() sie noch nicht übernommen hat
        try:
            # Dritter Anlauf nach zwei toten Stream-Versuchen → Download erzwingen.
            force_download = url == self._force_download_url
            # Resolve-Fenster: solange es offen ist, kann ein Alternativ-Button
            # diesen Track abwählen (_skip_resolving).
            self._resolving_url = url
            try:
                info, filename, title, duration = await self._resolve_track(
                    url, title, force_download=force_download, seek_offset=seek_offset
                )

                is_stream = isinstance(filename, str)  # Stream-URL statt lokaler Datei (z. B. Track > 20 min)

                # Stream-URL erst ab available_at abrufbar, sonst 403 (utils/ffmpeg.py).
                # Noch im Resolve-Fenster, damit ein Skip im Wartefenster greift.
                wait = stream_wait_seconds(info) if is_stream and not force_download else 0
                if wait > 0:
                    logger.info(f"[Stream] Warte {wait:.1f}s bis YouTube die URL freigibt: {title}")
                    await asyncio.sleep(wait)
            finally:
                # Der Skip-Wunsch gilt nur für diesen Resolve: immer verbrauchen,
                # auch wenn der Resolve scheitert – sonst überspränge der nächste
                # Durchlauf genau die Alternative, die der Nutzer gewählt hat.
                self._resolving_url = None
                skip, self._skip_resolving = self._skip_resolving, False

            if skip:
                await self.play_next(ctx)
                return

            eq_filter = self.eq_presets.get(self.equalizer, "")
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
            token = PlayerToken()
            run = TrackRun(
                ctx=ctx, url=url, title=title, duration=duration, filename=filename,
                seek_offset=seek_offset, is_stream=is_stream, is_growing=is_growing,
                prog_task=prog_task, stderr_buf=stderr_buf, token=token,
            )
            after_playing = self._make_after_playing(run)

            if not ctx.voice_client or not ctx.voice_client.is_connected():
                logger.warning("[play_next] Voice client nicht verbunden – Wiedergabe abgebrochen.")
                self.is_playing = False
                self.queue.appendleft((url, title))
                self._restart_url = url if is_restart else None
                self._discard_source(source, stderr_buf)   # after_playing läuft nie
                return

            # Zweite dm_speaking-Prüfung: Das Auflösen oben (await _resolve_track) kann ein
            # Netzwerk-Call sein – in diesem Fenster könnte die DM-Bridge den Voice-Client
            # übernommen haben. Track zurück an den Anfang der Queue, nicht überspielen.
            if getattr(self.bot, "dm_speaking", False):
                logger.info("[play_next] DM spricht – Track zurückgestellt statt überspielt.")
                self.is_playing = False
                self.queue.appendleft((url, title))
                self._restart_url = url if is_restart else None
                self._track_generation += 1     # hängende Post-Play-awaits entwerten
                self._discard_source(source, stderr_buf)   # after_playing läuft nie
                return

            # Kompletten Track-Zustand synchron VOR vc.play() setzen (ADR 0007).
            generation, is_loop_repeat = self._snapshot_track_state(
                ctx, url, title, duration, count_play=not is_restart
            )
            self._current_seek_offset = seek_offset

            # Pausierter Player (!x, dann neuer Start): discord.py würde ihn in
            # play() nur ersetzen, nicht stoppen. Synchron, direkt vor play().
            if ctx.voice_client.is_paused():
                self._discard_paused_player(ctx.voice_client)
            ctx.voice_client.play(source, after=after_playing)
            source = None                # gehört jetzt dem Player; beendet wird sie über after_playing
            self._player_token = token   # erst nach geglücktem play(): gehört zum laufenden Player
            logger.info(f"[Wiedergabe] Starte: {title}")

            if is_loop_repeat:
                await self._reset_card_for_loop_repeat(duration)
            else:
                await self._retire_previous_cards()
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
            self._discard_source(source)
            await self._skip_broken_track(ctx, url, title, stderr_buf, "error.track_timeout", "TimeoutError")
            return
        except Exception:
            logger.exception("[Fehler bei play_next]")
            self._discard_source(source)
            await self._skip_broken_track(ctx, url, title, stderr_buf, "error.track_error", "Exception")
            return

    def _advance_block_reason(self) -> str | None:
        """Grund, warum play_next gerade keinen Track starten darf, sonst None."""
        if self.is_radio:
            # Radio hat die Musik gestoppt.
            return "Radio aktiv – play_next übersprungen."
        # DM-Bridge spricht gerade → den Voice-Client NICHT mit dem nächsten Track besetzen.
        # Sonst kollidiert unser vc.play() mit dem der Bridge ("Already playing audio."): ein
        # Track-Ende (natürlich oder durch das vc.stop() der Bridge) würde hier sofort den
        # nächsten Song starten. Das Flag setzt/löscht dm_bridge.py um die /speak-Wiedergabe.
        if getattr(self.bot, "dm_speaking", False):
            return "DM spricht – Auto-Advance unterdrückt."
        # Voice-Reparatur baut gerade die Verbindung neu auf (music_voice_ui.py) –
        # das after_playing des alten Clients darf nicht den nächsten Song
        # starten; die Reparatur ruft play_next danach selbst auf.
        if self._voice_healing:
            return "Voice-Reparatur läuft – Auto-Advance unterdrückt."
        return None

    async def _wait_playback_done(self, timeout: float = 3.0) -> bool:
        """Wartet, bis der vorherige Player sein Ende gemeldet hat. True, wenn
        er fertig ist; False bei Timeout. Wie der Aufrufer auf False reagiert
        und ob er das Event danach löscht, entscheidet er selbst."""
        try:
            await asyncio.wait_for(self._playback_done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return True

    async def _handle_queue_empty(self, ctx):
        """Leere-Queue-Fall aus play_next: Cleanup, _ended_np-Übergabe, Idle-Timer,
        Autoplay-Handoff (inkl. Prefetch-Wait).

        Enthält await-Grenzen (_finalize_progress_bar, Prefetch-Wait,
        play_next/autoplay); play_next awaitet die Methode direkt und kehrt
        danach zurück (ADR 0009).
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
        # Nachricht nie wieder anfassen. Die Karte selbst bleibt nach dem
        # Queue-Ende stehen (Nutzerwunsch 28.11.2026) und wird gemerkt, damit der
        # nächste Track sie zurückbaut – bis dahin bleiben Fortsetzen/Autoplay
        # darauf klickbar. !clear/❌ haben die Karte schon selbst zurückgebaut.
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

    def _take_restart(self, url: str) -> bool:
        """Konsumiert die Neustart-Markierung für den gerade aus der Queue
        genommenen Track. True = derselbe Track wird nur neu angesetzt (!seek,
        !eq, Voice-Reparatur) und zählt nicht erneut für !score. Wie der
        Seek-Offset wird die Markierung in jedem Fall gelöscht und gilt nur für
        die URL, für die sie gesetzt wurde."""
        bound_url, self._restart_url = self._restart_url, None
        return bound_url is not None and bound_url == url

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

    def _build_audio_source(self, info, filename, seek_offset, eq_filter, is_stream, is_growing):
        """Baut FFmpeg-Options, legt die stderr-Temp-Datei an und konstruiert die
        Audio-Source. Synchron; gibt (source, stderr_buf) zurück.

        is_stream/is_growing sind der Stand vom Startzeitpunkt; play_next
        ermittelt sie, weil es sie auch für die Giveup-Prüfung und das
        Track-Ende braucht.
        """
        parts = []
        if is_stream:
            parts.append("-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5")
            headers = self._ffmpeg_header_opts(info)
            if headers:
                parts.append(headers)
        if seek_offset:
            parts.append(f"-ss {seek_offset}")

        # FFmpeg-stderr in eine Temp-Datei umleiten: Stirbt der Prozess sofort,
        # sind die letzten Zeilen der einzige Beweis für die Ursache (403 vom
        # CDN? kaputter Filter?). Gelesen und geschlossen wird in after_playing.
        stderr_buf = tempfile.TemporaryFile()

        kwargs = {"before_options": " ".join(parts) or None, "stderr": stderr_buf}
        if eq_filter:
            # Filter aktiv → dekodieren, EQ anwenden, mit 192kbps zu Opus enkodieren.
            # -vn unterdrückt den Video-Stream.
            kwargs.update(bitrate=192, options=f"-vn {eq_filter}")
        elif is_stream or is_growing:
            # HTTP-Stream: codec=copy funktioniert nicht zuverlässig bei Netz-URLs → transkodieren.
            # Wachsende webm: nie codec=copy – der Muxer schreibt Cues/Duration
            # erst am Dateiende, Transkodieren liest tolerant linear. Lokale
            # Datei → keine -reconnect/-headers-Optionen nötig.
            kwargs["bitrate"] = 192
        else:
            # Kein Filter (flat) → Opus-Stream 1:1 durchreichen, kein Qualitätsverlust.
            kwargs["codec"] = "copy"
        source = discord.FFmpegOpusAudio(str(filename), **kwargs)
        # Uhr des Players erst beim ersten Paket: sonst schiebt er FFmpegs
        # Anlaufzeit als Ton ohne Pause nach (Anfang klingt kurz zu schnell).
        return start_clock_at_first_packet(source), stderr_buf

    def _discard_source(self, source, stderr_buf=None):
        """Beendet eine Quelle, die nie an vc.play() ging: FFmpegOpusAudio
        startet den Prozess schon im Konstruktor, und ohne cleanup() liefe er
        weiter, bis der Garbage Collector die Quelle abräumt (unter Windows
        bliebe so lange die Datei offen). Schließt auch den stderr-Puffer,
        falls übergeben – after_playing läuft für diese Quelle nie.
        source=None (noch nicht gebaut oder schon beim Player) ist ein No-Op."""
        cleanup = getattr(source, "cleanup", None)
        if cleanup is not None:
            try:
                cleanup()
            except Exception as e:
                logger.debug(f"[Wiedergabe] cleanup() der verworfenen Quelle scheiterte: {e}")
        if stderr_buf is not None:
            self._stderr_tail(stderr_buf)

    def _snapshot_track_state(self, ctx, url, title, duration, count_play=True):
        """Setzt den kompletten Track-Zustand synchron VOR vc.play() (ADR 0007).

        **Muss synchron bleiben** – kein await zwischen den Zuweisungen. Gibt
        (generation, is_loop_repeat) zurück; play_next braucht sie für die
        Generationschecks nach jedem await und für den Karten-Übergang.
        count_play=False: gewollter Neustart desselben Tracks (!seek, !eq,
        Voice-Reparatur) – kein weiterer Play-Count.
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
        # Vorherigen Song merken, bevor er überschrieben wird – aber nur einen
        # ANDEREN Song. Startet derselbe erneut (Loop, Stream-Retry, Resume) oder
        # ist current_track vor dem Neustart None (!seek, !eq, Voice-Reparatur),
        # bleibt der bisherige Vorgänger stehen. Der laufende Song ist nie sein
        # eigener Vorgänger (!replay nach Queue-Ende): Er geht vom Verlauf, der
        # Song davor wird Vorgänger (last_played ist der Verlauf, siehe music.py).
        if prev_track is not None and prev_track[0] != url:
            self.last_played = prev_track
        elif self.last_played is not None and self.last_played[0] == url:
            self.last_played = None
        self.current_track = (url, title, duration)
        self.track_start_time = time.monotonic()
        self._np_paused_total = 0.0   # Pausen-State für den neuen Song zurücksetzen
        self._np_paused_at = None
        self._np_last_desc = None
        self._recently_played.append(url)
        self._recently_played_titles.append(normalize_title(title))
        # Weiterer Anlauf desselben Tracks (Stream-Retry, Download-Fallback
        # oder progressives Seek-Resume) → Sperren behalten, nicht doppelt zählen.
        if url not in (self._stream_retry_url, self._progressive_resume_url):
            self._clear_retry_markers()     # anderer Track → Retry-Sperren aufheben
            if count_play:
                self._record_play(url, title)
        self.text_channel = ctx.channel
        return generation, is_loop_repeat

    async def _reset_card_for_loop_repeat(self, duration):
        """Loop-"Song": derselbe Song läuft erneut. Die bestehende Karte samt
        Buttons bleibt stehen, nur der Fortschrittsbalken springt auf 0:00."""
        self._skipped_np_msg = None   # Karte lebt weiter – später nicht löschen
        bar = self._progress_bar(0, duration)
        if not bar:
            return
        self.now_playing_view.set_body(bar)
        self.now_playing_view.sync_state(up_next=self._up_next_title())
        self._np_last_desc = bar
        try:
            await self.now_playing_msg.edit(view=self.now_playing_view)
        except Exception:
            pass

    async def _retire_previous_cards(self):
        """Reduziert die Karte(n) früherer Songs auf reinen Text – nur der
        aktuelle Song behält Karte und Buttons. Nach Queue-Ende liegt die
        Nachricht in _ended_np (der Cleanup hat now_playing_msg gekappt);
        beide Slots können gleichzeitig belegt sein – dann müssen auch beide
        zurückgebaut werden, sonst bliebe eine Karte für immer stehen.
        Ohne alte Karte läuft die Methode ohne await durch."""
        old_msg, old_title = self.now_playing_msg, self._np_title
        ended_np, self._ended_np = self._ended_np, None
        if old_msg or ended_np:
            await self._retire_np_message(old_msg, old_title)
            if ended_np:
                await self._retire_np_message(*ended_np)

    def _build_np_view(self, ctx, info, url, title, duration):
        """Now-Playing-Karte (Components V2) eines frisch gestarteten Tracks
        (synchron, ohne Nebenwirkung)."""
        duration_str = f"{duration // 60}:{duration % 60:02d}" if duration else t("misc.unknown")
        page = info.get("webpage_url")
        details = (
            f"**{t('embed.duration')}** {duration_str} · **{t('embed.eq')}** {self.equalizer}"
            f" · **{t('embed.format')}** {self.audio_format}"
        )
        return MusicControlView(
            self, ctx, song=(url, title),
            heading=np_heading(title, info),
            body=self._progress_bar(0, duration),   # Fortschrittsbalken sofort bei 0:00 anzeigen
            details=details,
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
        _cancel(self.prefetch_task)
        if self.queue:
            self.prefetch_task = asyncio.create_task(self._prefetch_upcoming())

        # Autoplay: nächsten verwandten Song suchen+laden während der aktuelle läuft.
        if self.autoplay_enabled and not self.queue:
            _cancel(self._autoplay_prefetch_task)
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
