"""Track-Ende der Wiedergabe – Mixin für PlaybackMixin.

Der after-Callback, den discord.py im Player-Thread aufruft, reicht die
Track-Ende-Logik an den Event-Loop weiter (_run_on_loop, ADR 0009). Im
Thread bleibt nur Datei-I/O: der FFmpeg-stderr-Puffer davor und der
last_queue.json-Write danach (ADR 0005).
"""

import asyncio
import concurrent.futures
import json
import threading
import time

from utils.files import atomic_write_text
from utils.logger import logger
from utils.i18n import t

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


class TrackEndMixin:
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
