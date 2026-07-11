"""Tests für die Race-/Fehlerbehandlung in play_next und den Fortschrittsbalken.

Die Tests bauen MusicCommands ohne __init__ zusammen (kein echter Downloader,
kein FFmpeg, kein Discord-Netzwerk) und simulieren den after_playing-Callback
von Hand – so wie ihn der FFmpeg-Thread bei einem sofort sterbenden Track feuert.
"""

import asyncio
import time
import types
from collections import deque

import discord
import pytest

from cogs.music import MusicCommands
from cogs.presets import EQ_PRESETS


class FakeVoiceClient:
    def __init__(self):
        self.play_calls = []
        self._after = None
        self._playing = False

    def is_connected(self):
        return True

    def is_playing(self):
        return self._playing

    def is_paused(self):
        return False

    def play(self, source, after=None):
        self._playing = True
        self._after = after
        self.play_calls.append(source)

    def stop(self):
        self._playing = False

    def end_track(self, error=None):
        """Simuliert das Track-Ende: FFmpeg-Prozess weg → after_playing feuert."""
        self._playing = False
        after, self._after = self._after, None
        after(error)


class FakeMessage:
    def __init__(self):
        self.edits = []
        self.deleted = False

    async def edit(self, **kwargs):
        self.edits.append(kwargs)

    async def delete(self):
        self.deleted = True


class FakeCtx:
    def __init__(self, voice_client):
        self.voice_client = voice_client
        self.channel = object()
        self.sent = []          # (args, kwargs) aller send-Aufrufe
        self.messages = []      # die zurückgegebenen FakeMessages
        self.send_gate = None   # asyncio.Event → send blockiert bis gesetzt
        self.send_started = asyncio.Event()

    async def send(self, *args, **kwargs):
        self.send_started.set()
        if self.send_gate is not None:
            await self.send_gate.wait()
        self.sent.append((args, kwargs))
        msg = FakeMessage()
        self.messages.append(msg)
        return msg


class FakeDownloader:
    """Liefert immer dasselbe resolve-Ergebnis; zählt Aufrufe."""

    def __init__(self, result):
        self.result = result
        self.force_result = None   # Rückgabe bei force_download=True (None → self.result)
        self.resolve_calls = []
        self.force_calls = []
        self.invalidated = []
        self.incomplete_paths = set()
        self.progressive_tasks = {}
        self.blocked = []

    async def resolve_track(self, url, title, prefetch_task=None, force_download=False,
                            min_buffer_seconds=0):
        self.resolve_calls.append(url)
        self.force_calls.append(force_download)
        await asyncio.sleep(0)
        if force_download and self.force_result is not None:
            return self.force_result
        return self.result

    def invalidate(self, url):
        self.invalidated.append(url)

    async def prefetch_next(self, queue, idx=0):
        pass

    # Progressiver Download: Tests simulieren über incomplete_paths/
    # progressive_tasks eine wachsende Datei bzw. einen laufenden Download.
    def is_incomplete(self, path):
        return path in self.incomplete_paths

    def progressive_task_for(self, url):
        return self.progressive_tasks.get(url)

    def block_progressive(self, url):
        self.blocked.append(url)

    async def wait_progressive_idle(self, timeout=300.0):
        pass


def make_fake_source(stderr_bytes=b"", calls=None):
    """Ersatz für discord.FFmpegOpusAudio: startet keinen Prozess, schreibt
    optional vorgegebene stderr-Zeilen in den übergebenen Puffer und
    protokolliert die Konstruktor-Argumente in `calls`."""

    class _FakeSource:
        def __init__(self, source, *args, **kwargs):
            if calls is not None:
                calls.append((source, kwargs))
            buf = kwargs.get("stderr")
            if buf is not None and stderr_bytes:
                buf.write(stderr_bytes)
                buf.flush()

    return _FakeSource


STREAM_INFO = {
    "title": "Testsong",
    "webpage_url": "https://www.youtube.com/watch?v=test",
    "thumbnail": None,
    "uploader": None,
    "duration": 200,
    "http_headers": {"User-Agent": "UA-Test"},
}


def make_cog(dl):
    """MusicCommands ohne __init__ – nur der State, den play_next & Co. anfassen."""
    mc = MusicCommands.__new__(MusicCommands)
    mc.bot = types.SimpleNamespace(loop=asyncio.get_running_loop(), dm_speaking=False)
    mc.queue = deque()
    mc.is_playing = False
    mc.current_track = None
    mc.last_played = None
    mc.prefetch_task = None
    mc._autoplay_prefetch_task = None
    mc._autoplay_queued_url = None
    mc._recently_played = deque(maxlen=15)
    mc._recently_played_titles = deque(maxlen=15)
    mc.auto_leave_task = None
    mc.idle_leave_task = None
    mc._last_ctx = None
    mc._stuck_ticks = 0
    mc._stopped_by_user = False
    mc.text_channel = None
    mc.now_playing_msg = None
    mc.now_playing_embed = None
    mc.track_start_time = None
    mc._np_paused_total = 0.0
    mc._np_paused_at = None
    mc._np_last_desc = None
    mc._skip_resolving = False
    mc._track_generation = 0
    mc._ended_np = None
    mc._stream_retry_url = None
    mc._force_download_url = None
    mc._progressive_resume_url = None
    mc._progressive_resume_count = 0
    mc._suppress_resume = False
    mc.equalizer = "punchy"
    mc.audio_format = "webm"
    mc.loop_mode = None
    mc._songs_played = 0
    mc.eq_presets = EQ_PRESETS
    mc.autoplay_enabled = False
    mc.is_radio = False
    mc._playback_done = asyncio.Event()
    mc._playback_done.set()
    mc._seek_offset = 0
    mc._play_counts = {}
    mc.dl = dl
    # echtes _record_play schreibt play_counts.json – hier nur zählen
    mc._record_play = lambda url, title: mc._play_counts.__setitem__(
        url, mc._play_counts.get(url, 0) + 1
    )
    return mc


def _cleanup(mc):
    """Hintergrund-Tasks (Idle-Timer etc.) abbrechen, damit der Loop sauber schließt."""
    for task in (mc.idle_leave_task, mc.prefetch_task, mc._autoplay_prefetch_task):
        if task is not None and not task.done():
            task.cancel()


def test_race_track_dies_before_now_playing_send(monkeypatch, tmp_path):
    """Stirbt der Track, während ctx.send der Now-Playing-Nachricht noch läuft,
    darf der verspätete Post-Play-Teil den Leere-Queue-Cleanup nicht überschreiben."""
    monkeypatch.chdir(tmp_path)  # last_queue.json landet im Temp-Verzeichnis
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source())

    async def run():
        dl = FakeDownloader((STREAM_INFO, "https://cdn.example/stream", "Testsong", 200))
        vc = FakeVoiceClient()
        ctx = FakeCtx(vc)
        mc = make_cog(dl)
        mc.queue.append(("https://www.youtube.com/watch?v=test", "Testsong"))
        ctx.send_gate = asyncio.Event()

        task = asyncio.create_task(mc.play_next(ctx))
        await asyncio.wait_for(ctx.send_started.wait(), 5)

        # Track stirbt nach 0.09s – der geplante play_next-Cleanup (leere Queue)
        # läuft durch, während ctx.send noch hängt.
        vc.end_track()
        for _ in range(20):
            await asyncio.sleep(0)

        assert mc.current_track is None          # Cleanup ist gelaufen
        assert mc.is_playing is False

        ctx.send_gate.set()                       # ctx.send kehrt jetzt erst zurück
        await asyncio.wait_for(task, 5)

        # Der verspätete Durchlauf darf den toten Track nicht wiederbeleben:
        assert mc.current_track is None
        assert mc.now_playing_msg is None
        assert mc.now_playing_embed is None
        assert mc.track_start_time is None
        assert mc.is_playing is False
        # Die verwaiste Nachricht wurde entschärft (Buttons/Embed entfernt).
        assert ctx.messages, "Now-Playing-Nachricht wurde gesendet"
        assert ctx.messages[-1].edits
        assert ctx.messages[-1].edits[-1].get("view") is None

        _cleanup(mc)

    asyncio.run(run())


def test_normal_playback_sets_state_before_play(monkeypatch, tmp_path):
    """Normalfall: Zustand steht schon beim vc.play(), Nachricht wird registriert,
    Stream-Pfad reicht die yt_dlp-HTTP-Header an FFmpeg durch."""
    monkeypatch.chdir(tmp_path)
    source_calls = []
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source(calls=source_calls))

    async def run():
        url = "https://www.youtube.com/watch?v=test"
        dl = FakeDownloader((STREAM_INFO, "https://cdn.example/stream", "Testsong", 200))
        vc = FakeVoiceClient()
        ctx = FakeCtx(vc)
        mc = make_cog(dl)
        mc.queue.append((url, "Testsong"))

        await mc.play_next(ctx)

        assert mc.current_track == (url, "Testsong", 200)
        assert mc.is_playing is True
        assert mc.track_start_time is not None
        assert mc.now_playing_msg is ctx.messages[-1]
        assert mc._play_counts == {url: 1}

        # Stream-Pfad: -headers mit den Headern aus dem Info-Dict
        before = source_calls[0][1].get("before_options", "")
        assert "-reconnect 1" in before
        assert "-headers" in before
        assert "UA-Test" in before

        _cleanup(mc)

    asyncio.run(run())


def test_ffmpeg_header_opts_roundtrip():
    """Das Quoting muss shlex.split (so zerlegt discord.py before_options) überleben."""
    import shlex

    opts = MusicCommands._ffmpeg_header_opts(
        {"http_headers": {"User-Agent": "UA Test 1.0", "Accept": "*/*"}}
    )
    parts = shlex.split(opts)
    assert parts[0] == "-headers"
    assert "User-Agent: UA Test 1.0\r\n" in parts[1]
    assert "Accept: */*\r\n" in parts[1]

    assert MusicCommands._ffmpeg_header_opts({}) == ""
    assert MusicCommands._ffmpeg_header_opts(None) == ""
    assert MusicCommands._ffmpeg_header_opts({"http_headers": {}}) == ""


def test_progress_bar_clamps_elapsed_beyond_total():
    """elapsed > total darf weder Label noch Knopf über das Songende hinauslaufen lassen."""
    bar = MusicCommands._progress_bar(400, 300)
    assert bar.endswith("5:00 / 5:00")
    assert bar.startswith("▬" * 20 + "🔘")     # Knopf ganz rechts (length=21)
    # Über die Dauer hinaus bleibt der String stabil → keine endlosen Edits.
    assert MusicCommands._progress_bar(402, 300) == bar
    assert MusicCommands._progress_bar(10_000, 300) == bar


def test_progress_bar_clamps_negative_elapsed():
    bar = MusicCommands._progress_bar(-5, 300)
    assert bar.startswith("🔘")
    assert "0:00 / 5:00" in bar


@pytest.mark.parametrize(
    "stderr_text,expected",
    [
        ("[https @ 0x1] HTTP error 403 Forbidden\nInput/output error", "input"),
        ("Invalid data found when processing input", "input"),
        ("Connection reset by peer", "input"),
        ("Error initializing filter 'equalizer' with args 'f=80'", "filter"),
        ("No such filter: 'equalizzer'", "filter"),
        ("", None),
        ("irgendein harmloses Gebrabbel", None),
    ],
)
def test_classify_ffmpeg_error(stderr_text, expected):
    assert MusicCommands._classify_ffmpeg_error(stderr_text) == expected


def test_stream_retry_once_then_give_up(monkeypatch, tmp_path):
    """Sofortiger Track-Tod + Input-Fehler → genau EIN Retry mit frischer URL,
    danach EIN Download-Fallback-Anlauf; liefert der keine lokale Datei
    (hier: resolve gibt weiter einen Stream zurück), Aufgeben mit i18n-Meldung
    und ohne doppelten Play-Count."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        discord,
        "FFmpegOpusAudio",
        make_fake_source(b"[https @ 0x1] HTTP error 403 Forbidden\n"),
    )
    from utils.i18n import t

    async def run():
        url = "https://www.youtube.com/watch?v=test"
        dl = FakeDownloader((STREAM_INFO, "https://cdn.example/stream", "Testsong", 200))
        vc = FakeVoiceClient()
        ctx = FakeCtx(vc)
        mc = make_cog(dl)
        mc.queue.append((url, "Testsong"))

        await mc.play_next(ctx)
        assert len(vc.play_calls) == 1
        vc.end_track()   # stirbt sofort mit 403

        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(vc.play_calls) >= 2:
                break
        assert len(vc.play_calls) == 2, "genau ein Retry muss gestartet werden"
        assert dl.invalidated == [url], "Cache-Eintrag muss vor dem Retry invalidiert werden"

        vc.end_track()   # zweiter Versuch stirbt genauso → Download-Fallback → gibt Stream → aufgeben
        for _ in range(200):
            await asyncio.sleep(0.01)
            if mc.is_playing is False and mc._force_download_url is None and ctx.sent:
                break

        assert len(vc.play_calls) == 2, "kein dritter Stream-Versuch"
        assert not mc.queue
        assert mc._stream_retry_url is None
        assert mc._force_download_url is None
        assert dl.force_calls == [False, False, True], "dritter resolve muss den Download erzwingen"
        texts = [args[0] for args, kwargs in ctx.sent if args]
        fallback = t("error.stream_download_fallback", title="Testsong")
        assert fallback in texts, f"Download-Fallback-Meldung fehlt, gesendet wurde: {texts}"
        giveup = t("error.stream_giveup", title="Testsong")
        assert giveup in texts, f"Aufgeben-Meldung fehlt, gesendet wurde: {texts}"
        assert mc._play_counts == {url: 1}, "Retry darf den Play-Count nicht doppelt zählen"

        _cleanup(mc)

    asyncio.run(run())


def test_stream_retry_download_fallback_plays_local_file(monkeypatch, tmp_path):
    """Zwei tote Stream-Versuche → dritter Anlauf spielt die lokal
    heruntergeladene Datei; Play-Count bleibt bei 1, Marker werden nach
    normalem Track-Ende aufgeräumt."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        discord,
        "FFmpegOpusAudio",
        make_fake_source(b"[https @ 0x1] HTTP error 403 Forbidden\n"),
    )
    from utils.i18n import t

    async def run():
        url = "https://www.youtube.com/watch?v=test"
        local_file = tmp_path / "Testsong.webm"
        dl = FakeDownloader((STREAM_INFO, "https://cdn.example/stream", "Testsong", 200))
        dl.force_result = (STREAM_INFO, local_file, "Testsong", 200)
        vc = FakeVoiceClient()
        ctx = FakeCtx(vc)
        mc = make_cog(dl)
        mc.queue.append((url, "Testsong"))

        await mc.play_next(ctx)
        vc.end_track()   # Versuch 1 stirbt mit 403
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(vc.play_calls) >= 2:
                break
        vc.end_track()   # Versuch 2 stirbt genauso → Download-Fallback
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(vc.play_calls) >= 3:
                break

        assert len(vc.play_calls) == 3, "Download-Fallback muss einen dritten Versuch starten"
        assert dl.force_calls == [False, False, True]
        texts = [args[0] for args, kwargs in ctx.sent if args]
        fallback = t("error.stream_download_fallback", title="Testsong")
        assert fallback in texts, f"Download-Fallback-Meldung fehlt, gesendet wurde: {texts}"
        assert mc._play_counts == {url: 1}, "Fallback darf den Play-Count nicht mehrfach zählen"

        # Track läuft "lange" und endet normal → Marker müssen aufgeräumt werden.
        mc.track_start_time = time.monotonic() - 10
        vc.end_track()
        for _ in range(200):
            await asyncio.sleep(0.01)
            if mc._stream_retry_url is None:
                break

        assert mc._stream_retry_url is None
        assert mc._force_download_url is None
        giveup = t("error.stream_giveup", title="Testsong")
        texts = [args[0] for args, kwargs in ctx.sent if args]
        assert giveup not in texts, "erfolgreicher Fallback darf keine Aufgeben-Meldung senden"

        _cleanup(mc)

    asyncio.run(run())


def test_no_retry_on_filter_error(monkeypatch, tmp_path):
    """Echter Filterfehler → kein Retry, Track wird normal übersprungen."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        discord,
        "FFmpegOpusAudio",
        make_fake_source(b"Error initializing filter 'equalizer' with args 'f=80'\n"),
    )

    async def run():
        url = "https://www.youtube.com/watch?v=test"
        dl = FakeDownloader((STREAM_INFO, "https://cdn.example/stream", "Testsong", 200))
        vc = FakeVoiceClient()
        ctx = FakeCtx(vc)
        mc = make_cog(dl)
        mc.queue.append((url, "Testsong"))

        await mc.play_next(ctx)
        vc.end_track()
        for _ in range(50):
            await asyncio.sleep(0.01)

        assert len(vc.play_calls) == 1
        assert dl.invalidated == []
        assert not mc.queue
        assert mc.is_playing is False

        _cleanup(mc)

    asyncio.run(run())


def test_growing_file_transcodes_without_reconnect_options(monkeypatch, tmp_path):
    """Wachsende Datei (progressiver Download): nie codec=copy (Cues fehlen bis
    Dateiende), lokale Quelle → keine -reconnect/-headers-Optionen."""
    monkeypatch.chdir(tmp_path)
    source_calls = []
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source(calls=source_calls))

    async def run():
        url = "https://www.youtube.com/watch?v=test"
        growing = tmp_path / "Testsong.webm"
        dl = FakeDownloader((STREAM_INFO, growing, "Testsong", 200))
        dl.incomplete_paths.add(growing)
        vc = FakeVoiceClient()
        ctx = FakeCtx(vc)
        mc = make_cog(dl)
        mc.equalizer = "flat"   # sonst greift der EQ-Zweig (transkodiert sowieso)
        mc.queue.append((url, "Testsong"))

        await mc.play_next(ctx)

        _, kwargs = source_calls[0]
        assert "codec" not in kwargs, "wachsende Datei darf nie codec=copy bekommen"
        assert kwargs.get("bitrate") == 192
        before = kwargs.get("before_options") or ""
        assert "-reconnect" not in before
        assert "-headers" not in before

        _cleanup(mc)

    asyncio.run(run())


def test_growing_file_early_eof_resumes_with_seek(monkeypatch, tmp_path):
    """FFmpeg überholt den laufenden Download → Track wird mit -ss an der
    Hörposition wieder vorn eingereiht, ohne doppelten Play-Count; nach
    normalem Ende werden die Marker aufgeräumt."""
    monkeypatch.chdir(tmp_path)
    source_calls = []
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source(calls=source_calls))

    async def run():
        url = "https://www.youtube.com/watch?v=test"
        growing = tmp_path / "Testsong.webm"
        dl = FakeDownloader((STREAM_INFO, growing, "Testsong", 200))
        dl.incomplete_paths.add(growing)
        prog_task = asyncio.create_task(asyncio.sleep(30))   # Download "läuft noch"
        dl.progressive_tasks[url] = prog_task
        vc = FakeVoiceClient()
        ctx = FakeCtx(vc)
        mc = make_cog(dl)
        mc.queue.append((url, "Testsong"))

        await mc.play_next(ctx)
        # Track "spielte" 30 s und endet dann vorzeitig (Datei-EOF, kein Fehler).
        mc.track_start_time = time.monotonic() - 30
        vc.end_track()
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(vc.play_calls) >= 2:
                break

        assert len(vc.play_calls) == 2, "Resume muss einen zweiten Versuch starten"
        assert mc._progressive_resume_count == 1
        assert mc._play_counts == {url: 1}, "Resume darf den Play-Count nicht doppelt zählen"
        before = source_calls[1][1].get("before_options") or ""
        assert "-ss 29" in before, "Resume muss 1 s vor der Hörposition einsteigen"

        # Download wird fertig, Track endet diesmal normal → Marker aufgeräumt.
        prog_task.cancel()
        dl.progressive_tasks.clear()
        dl.incomplete_paths.clear()
        mc.track_start_time = time.monotonic() - 300
        vc.end_track()
        for _ in range(200):
            await asyncio.sleep(0.01)
            if mc._progressive_resume_url is None:
                break

        assert mc._progressive_resume_url is None
        assert mc._progressive_resume_count == 0
        assert mc._play_counts == {url: 1}

        _cleanup(mc)

    asyncio.run(run())


def test_growing_file_dead_download_blocks_and_requeues(monkeypatch, tmp_path):
    """Download tot (kein laufender Task, Datei weiter unvollständig) → progressiv
    wird für die URL gesperrt und der Track vorn eingereiht (Stream-Kaskade)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source())

    async def run():
        url = "https://www.youtube.com/watch?v=test"
        growing = tmp_path / "Testsong.webm"
        dl = FakeDownloader((STREAM_INFO, growing, "Testsong", 200))
        dl.incomplete_paths.add(growing)   # kein Task in progressive_tasks → Download tot
        vc = FakeVoiceClient()
        ctx = FakeCtx(vc)
        mc = make_cog(dl)
        mc.loop_mode = "song"              # darf trotz Requeue nicht doppelt einreihen
        mc.queue.append((url, "Testsong"))

        await mc.play_next(ctx)
        mc.track_start_time = time.monotonic() - 30
        vc.end_track()
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(vc.play_calls) >= 2:
                break

        assert dl.blocked == [url], "tote wachsende Datei muss die URL progressiv sperren"
        assert len(vc.play_calls) == 2
        assert mc._play_counts == {url: 1}
        # Loop-Mode + Resume-Requeue dürfen den Song nicht doppelt in die Queue legen:
        assert list(mc.queue).count((url, "Testsong")) == 0, "Song spielt gerade, Queue muss leer sein"

        _cleanup(mc)

    asyncio.run(run())


def test_skip_on_growing_file_does_not_resume(monkeypatch, tmp_path):
    """!s während eine wachsende Datei spielt: der absichtliche Stopp darf
    NICHT als vorzeitiges Dateiende gewertet werden – kein Requeue, kein
    _seek_offset, der nächste Song aus der Queue spielt."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source())
    from cogs.music import MusicCommands as MC

    async def run():
        url1 = "https://www.youtube.com/watch?v=test"
        url2 = "https://www.youtube.com/watch?v=zwei"
        growing = tmp_path / "Testsong.webm"
        dl = FakeDownloader((STREAM_INFO, growing, "Testsong", 200))
        dl.incomplete_paths.add(growing)
        prog_task = asyncio.create_task(asyncio.sleep(30))
        dl.progressive_tasks[url1] = prog_task
        vc = FakeVoiceClient()
        ctx = FakeCtx(vc)
        mc = make_cog(dl)
        mc.queue.append((url1, "Testsong"))
        mc.queue.append((url2, "Zweiter"))

        await mc.play_next(ctx)
        mc.track_start_time = time.monotonic() - 30   # Track lief 30 s

        # User skippt: Kommando stoppt via _stop_for_advance, dann feuert
        # der after_playing-Callback (im echten Betrieb durch vc.stop()).
        await MC.skip.callback(mc, ctx)
        assert mc._suppress_resume is True
        vc.end_track()
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(vc.play_calls) >= 2:
                break

        assert len(vc.play_calls) == 2
        assert mc.current_track[0] == url2, "nach dem Skip muss der zweite Song laufen"
        assert not mc.queue, "der geskippte Song darf nicht wieder eingereiht werden"
        assert mc._seek_offset == 0
        assert mc._progressive_resume_url is None
        assert mc._suppress_resume is False, "One-Shot-Flag muss konsumiert sein"
        assert dl.blocked == []

        prog_task.cancel()
        _cleanup(mc)

    asyncio.run(run())


@pytest.mark.parametrize("lang", ["de", "en"])
def test_stream_giveup_message_in_both_locales(lang):
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "locales" / f"{lang}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "error.stream_giveup" in data
    assert "{title}" in data["error.stream_giveup"]
    assert "Testsong" in data["error.stream_giveup"].format(title="Testsong")


def test_stderr_tail_reads_and_closes():
    import tempfile

    buf = tempfile.TemporaryFile()
    buf.write(b"\n".join(f"zeile {i}".encode() for i in range(30)))
    tail = MusicCommands._stderr_tail(buf)
    lines = tail.splitlines()
    assert len(lines) == 20
    assert lines[-1] == "zeile 29"
    assert buf.closed
    # Doppelt lesen (z.B. Fehlerpfad nach after_playing) darf nicht crashen.
    assert MusicCommands._stderr_tail(buf) == ""

