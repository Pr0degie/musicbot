"""Tests für die Race-/Fehlerbehandlung in play_next und den Fortschrittsbalken.

Die Tests bauen MusicCommands ohne __init__ zusammen (kein echter Downloader,
kein FFmpeg, kein Discord-Netzwerk) und simulieren den after_playing-Callback
von Hand – so wie ihn der FFmpeg-Thread bei einem sofort sterbenden Track feuert.
"""

import asyncio
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

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


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
        self.resolve_calls = []
        self.invalidated = []

    async def resolve_track(self, url, title, prefetch_task=None):
        self.resolve_calls.append(url)
        await asyncio.sleep(0)
        return self.result

    def invalidate(self, url):
        self.invalidated.append(url)

    async def prefetch_next(self, queue, idx=0):
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

