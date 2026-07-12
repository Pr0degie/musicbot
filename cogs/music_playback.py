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
self.now_playing_msg/now_playing_embed, self.prefetch_task,
self._autoplay_prefetch_task, self._autoplay_queued_url) sowie auf den
Downloader-Cog (self.dl) inklusive dessen In-Flight-Registry und auf die in
cogs/music.py verbliebenen Klassen-Aliase self._ffmpeg_header_opts/
_stderr_tail/_classify_ffmpeg_error/_progress_bar.

Die ADR-0007-Sequenzierung (Track-Zustand vor vc.play(), Generationscheck nach
jedem Post-Play-await, _stop_for_advance als einziger vc.stop()-Pfad für Musik,
Retry-Reset NACH dem Progressive-Resume-Block in after_playing) ist hier
unverändert erhalten – nichts an der Reihenfolge wurde umgestellt.
"""

import asyncio
import json
import tempfile
import time

import discord

from utils.logger import logger
from utils.i18n import t
from cogs.downloader import normalize_title
from views.music_controls import MusicControlView, SearchAutoplayView

# Max. Seek-Resumes pro Track, wenn FFmpeg die wachsende Datei eines progressiven
# Downloads überholt hat – danach Fallback auf die Stream-/Retry-Kaskade.
PROGRESSIVE_MAX_RESUMES = 2

# Sentinel für _extract_info_or_report: unterscheidet "Fehler wurde bereits
# gemeldet" von einem echten None-Ergebnis aus yt_dlp (das die Aufrufer wie
# bisher selbst krachen lassen).
_YTDLP_FAILED = object()


class PlaybackMixin:
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

            # FFmpeg-Options, stderr-Temp-Datei und Source-Konstruktion (synchron).
            source, stderr_buf = self._build_audio_source(
                info, filename, seek_offset, eq_filter, is_stream, is_growing
            )
            # after_playing-Callback bauen: die vormaligen Closure-Variablen sind
            # jetzt Factory-Parameter, die interne Reihenfolge bleibt unverändert.
            after_playing = self._make_after_playing(
                ctx, stderr_buf, seek_offset, is_stream, is_growing, prog_task, filename, duration, url, title
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
        return source, stderr_buf

    def _make_after_playing(self, ctx, stderr_buf, seek_offset, is_stream, is_growing, prog_task, filename, duration, url, title):
        """Factory für den after_playing-Callback (ADR 0009).

        Der Callback-Body ist verbatim aus play_next übernommen; die vormaligen
        Closure-Variablen (stderr_buf, seek_offset, is_stream, is_growing,
        prog_task, filename, duration, url, title, ctx) sind jetzt Factory-
        Parameter. Die
        interne Reihenfolge – Progressive-Resume-Block VOR dem Retry-/Resume-
        Marker-Reset – bleibt unangetastet.
        """
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

        return after_playing

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
        return generation, is_loop_repeat, prev_track

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
