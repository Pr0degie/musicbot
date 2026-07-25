"""Gemeinsame Fakes und Factories für die Characterization-Tests.

Kein Produktionscode wird angefasst – die Fakes bilden nur die minimalen
Oberflächen von Discord-Objekten und dem Downloader nach, die die getesteten
Methoden tatsächlich berühren. Die Tests dokumentieren das IST-Verhalten
(Stand vor den geplanten Umbauten), nicht das Soll-Verhalten.
"""

import asyncio
import types
from collections import deque

import pytest

from cogs.music import MusicCommands
from cogs.presets import EQ_PRESETS


class FakeMessage:
    def __init__(self):
        self.edits = []
        self.deleted = False

    async def edit(self, **kwargs):
        self.edits.append(kwargs)

    async def delete(self):
        self.deleted = True


class FakeVoiceClient:
    def __init__(self):
        self.play_calls = []
        self.stop_calls = 0
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
        self.stop_calls += 1

    def end_track(self, error=None):
        """Simuliert das Track-Ende: FFmpeg-Prozess weg → after_playing feuert."""
        self._playing = False
        after, self._after = self._after, None
        after(error)


class FakeCtx:
    def __init__(self, voice_client=None):
        self.voice_client = voice_client
        self.channel = object()
        self.sent = []      # (args, kwargs) aller send-Aufrufe
        self.messages = []  # die zurückgegebenen FakeMessages

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        msg = FakeMessage()
        self.messages.append(msg)
        return msg

    @property
    def texts(self):
        """Nur die Text-Argumente aller send-Aufrufe (ohne Embeds)."""
        return [args[0] for args, _ in self.sent if args]


class FakeDownloader:
    """Liefert immer dasselbe resolve-Ergebnis; zählt Aufrufe."""

    def __init__(self, result=None):
        self.result = result
        self.force_result = None   # Rückgabe bei force_download=True (None → self.result)
        self.resolve_calls = []
        self.force_calls = []
        self.min_buffer_calls = []
        self.invalidated = []
        self.cleared = 0
        # Progressiver Download: Tests setzen incomplete_paths/progressive_tasks,
        # um eine wachsende Datei bzw. einen laufenden Download zu simulieren.
        self.incomplete_paths = set()
        self.progressive_tasks = {}
        self.blocked = []
        self.extract_calls = []    # (query, kind) je extract_info_async
        self.primed = []           # (url, title) je prime_first_hit
        self.resolved_only = []    # url je _start_resolve (nur Metadaten)

    async def resolve_track(self, url, title, force_download=False,
                            min_buffer_seconds=0):
        self.resolve_calls.append(url)
        self.force_calls.append(force_download)
        self.min_buffer_calls.append(min_buffer_seconds)
        await asyncio.sleep(0)
        if force_download and self.force_result is not None:
            return self.force_result
        return self.result

    def invalidate(self, url):
        self.invalidated.append(url)

    def clear_cache(self):
        self.cleared += 1

    async def prefetch_next(self, queue, idx=0):
        pass

    def is_incomplete(self, path):
        return path in self.incomplete_paths

    def progressive_task_for(self, url):
        return self.progressive_tasks.get(url)

    def block_progressive(self, url):
        self.blocked.append(url)

    async def wait_progressive_idle(self, timeout=300.0):
        pass

    async def extract_info_async(self, query, kind="main", *, timeout=30.0):
        """Ruft die passende Fake-Instanz auf – wie der echte Downloader, nur
        ohne Worker-Thread und ohne Cookie-Fallback."""
        self.extract_calls.append((query, kind))
        return getattr(self, self._YDL_KINDS[kind]).extract_info(query, download=False)

    _YDL_KINDS = {"main": "ydl", "search": "search_ydl", "url": "url_ydl",
                  "playlist": "playlist_ydl", "autoplay": "autoplay_ydl"}

    def prime_first_hit(self, url, title):
        self.primed.append((url, title))

    async def _start_resolve(self, url):
        self.resolved_only.append(url)


def build_cog(dl=None):
    """MusicCommands ohne __init__ – nur der State, den die Cog-Methoden anfassen.

    Muss innerhalb eines laufenden Event-Loops aufgerufen werden, wenn
    play_next/after_playing getestet werden (bot.loop wird dann gebraucht).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    mc = MusicCommands.__new__(MusicCommands)
    mc.bot = types.SimpleNamespace(loop=loop, dm_speaking=False)
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
    mc._np_title = None
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
    mc._playback_done = asyncio.Event() if loop else None
    if mc._playback_done:
        mc._playback_done.set()
    mc._seek_offset = 0
    mc._play_counts = {}
    mc.dl = dl if dl is not None else FakeDownloader()
    # echtes _record_play schreibt play_counts.json – hier nur zählen
    mc._record_play = lambda url, title: mc._play_counts.__setitem__(
        url, mc._play_counts.get(url, 0) + 1
    )
    return mc


def cleanup_cog(mc):
    """Hintergrund-Tasks (Idle-Timer, Prefetch) abbrechen, damit der Loop sauber schließt."""
    for task in (mc.idle_leave_task, mc.prefetch_task, mc._autoplay_prefetch_task):
        if task is not None and not task.done():
            task.cancel()


def make_fake_source(calls=None):
    """Ersatz für discord.FFmpegOpusAudio: startet keinen Prozess."""

    class _FakeSource:
        def __init__(self, source, *args, **kwargs):
            if calls is not None:
                calls.append((source, kwargs))

    return _FakeSource


@pytest.fixture
def make_cog():
    return build_cog


@pytest.fixture
def fake_ctx():
    return FakeCtx()
