"""Characterization-Tests: Loop-Logik im after_playing-Callback.

Der Callback legt bei loop_mode="song"/"queue" den gerade beendeten Song
zurück in die Queue – immer als 2-Tuple (url, title), obwohl current_track
ein 3-Tuple (url, title, duration) ist.
"""

import asyncio
import json
from pathlib import Path

import discord

from conftest import FakeCtx, FakeDownloader, FakeVoiceClient, build_cog, cleanup_cog, make_fake_source

URL_A = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
URL_B = "https://www.youtube.com/watch?v=bbbbbbbbbbb"

STREAM_INFO = {
    "title": "Song A",
    "webpage_url": URL_A,
    "thumbnail": None,
    "uploader": None,
    "duration": 200,
}


async def _start_track(monkeypatch):
    """Spielt Song A an und friert danach play_next ein, damit der
    after_playing-Callback die Queue nicht sofort wieder konsumiert."""
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source())
    dl = FakeDownloader((STREAM_INFO, "https://cdn.example/stream", "Song A", 200))
    vc = FakeVoiceClient()
    ctx = FakeCtx(vc)
    mc = build_cog(dl)
    mc.queue.append((URL_A, "Song A"))

    await mc.play_next(ctx)
    assert mc.current_track == (URL_A, "Song A", 200)

    async def frozen_play_next(_ctx):
        pass

    mc.play_next = frozen_play_next
    return mc, vc, ctx


def test_loop_song_appendleft_as_2tuple(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    async def run():
        mc, vc, ctx = await _start_track(monkeypatch)
        mc.queue.append((URL_B, "Song B"))
        mc.loop_mode = "song"

        vc.end_track()
        await asyncio.sleep(0)

        # Song A landet VORNE, als 2-Tuple – Duration aus dem 3-Tuple fällt weg.
        assert list(mc.queue) == [(URL_A, "Song A"), (URL_B, "Song B")]
        assert all(len(entry) == 2 for entry in mc.queue)
        cleanup_cog(mc)

    asyncio.run(run())


def test_loop_queue_appends_at_end(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    async def run():
        mc, vc, ctx = await _start_track(monkeypatch)
        mc.queue.append((URL_B, "Song B"))
        mc.loop_mode = "queue"

        vc.end_track()
        await asyncio.sleep(0)

        assert list(mc.queue) == [(URL_B, "Song B"), (URL_A, "Song A")]
        cleanup_cog(mc)

    asyncio.run(run())


def test_loop_none_requeues_nothing(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    async def run():
        mc, vc, ctx = await _start_track(monkeypatch)
        mc.queue.append((URL_B, "Song B"))
        mc.loop_mode = None

        vc.end_track()
        await asyncio.sleep(0)

        assert list(mc.queue) == [(URL_B, "Song B")]
        cleanup_cog(mc)

    asyncio.run(run())


def test_loop_song_without_current_track_requeues_nothing(monkeypatch, tmp_path):
    """IST: Ist current_track beim Track-Ende None (z.B. von !eq/!seek genullt),
    legt die Loop-Logik nichts zurück – genau das verhindert den Doppel-Insert."""
    monkeypatch.chdir(tmp_path)

    async def run():
        mc, vc, ctx = await _start_track(monkeypatch)
        mc.loop_mode = "song"
        mc.current_track = None  # wie im !eq-Restart-Pfad

        vc.end_track()
        await asyncio.sleep(0)

        assert list(mc.queue) == []
        cleanup_cog(mc)

    asyncio.run(run())


def test_last_queue_json_snapshot_excludes_loop_requeue(monkeypatch, tmp_path):
    """IST: last_queue.json wird VOR der Loop-Logik geschrieben – der per
    loop_mode zurückgelegte Song fehlt im Snapshot."""
    monkeypatch.chdir(tmp_path)

    async def run():
        mc, vc, ctx = await _start_track(monkeypatch)
        mc.queue.append((URL_B, "Song B"))
        mc.loop_mode = "song"

        vc.end_track()
        await asyncio.sleep(0)

        data = json.loads(Path("last_queue.json").read_text(encoding="utf-8"))
        assert data == [[URL_B, "Song B"]]          # Song A (Loop) fehlt
        assert list(mc.queue)[0] == (URL_A, "Song A")  # liegt aber in der Queue
        cleanup_cog(mc)

    asyncio.run(run())
