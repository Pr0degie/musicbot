"""Trackwechsel in play_next: die Karte des vorherigen Songs verschwindet.

Ergänzt tests/test_np_card_retire.py um den echten Durchlauf – inklusive des
Falls, dass now_playing_msg UND _ended_np gleichzeitig belegt sind (früher
verfiel dann eine Karte samt Buttons für immer).
"""

import asyncio

import discord

from test_music_playback import (
    STREAM_INFO, FakeCtx, FakeDownloader, FakeVoiceClient, _cleanup, make_cog,
    make_fake_source,
)

URL = "https://www.youtube.com/watch?v=test"


def edits_of(msg):
    return msg.edits[-1] if msg.edits else None


def assert_is_text_line(msg, title):
    edit = edits_of(msg)
    assert edit is not None, "Karte wurde nie zurückgebaut"
    assert edit["embed"] is None and edit["view"] is None
    assert edit["content"] == f"🎶 {title}"


def test_previous_card_becomes_text_line(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source())

    async def run():
        dl = FakeDownloader((STREAM_INFO, "https://cdn.example/stream", "Testsong", 200))
        ctx = FakeCtx(FakeVoiceClient())
        mc = make_cog(dl)

        # Song 1 läuft bereits und hat seine Karte.
        alte_karte = await ctx.send(embed=object())
        mc.now_playing_msg = alte_karte
        mc.now_playing_embed = object()
        mc._np_title = "Song 1"
        mc.current_track = ("https://x/1", "Song 1", 100)

        mc.queue.append((URL, "Testsong"))
        await mc.play_next(ctx)

        assert_is_text_line(alte_karte, "Song 1")
        neue_karte = ctx.messages[-1]
        assert mc.now_playing_msg is neue_karte, "nur der aktuelle Song hat die Karte"
        assert not neue_karte.edits, "die neue Karte bleibt unangetastet"
        assert mc._np_title == "Testsong"

        _cleanup(mc)

    asyncio.run(run())


def test_both_pending_cards_are_retired(monkeypatch, tmp_path):
    """now_playing_msg und _ended_np gleichzeitig belegt (z. B. Queue-Ende und
    danach ein 429): beide müssen zurückgebaut werden."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source())

    async def run():
        dl = FakeDownloader((STREAM_INFO, "https://cdn.example/stream", "Testsong", 200))
        ctx = FakeCtx(FakeVoiceClient())
        mc = make_cog(dl)

        aktuelle_karte = await ctx.send(embed=object())
        beendete_karte = await ctx.send(embed=object())
        mc.now_playing_msg = aktuelle_karte
        mc.now_playing_embed = object()
        mc._np_title = "Song 2"
        mc._ended_np = (beendete_karte, "Song 1")
        mc.current_track = ("https://x/2", "Song 2", 100)

        mc.queue.append((URL, "Testsong"))
        await mc.play_next(ctx)

        assert_is_text_line(aktuelle_karte, "Song 2")
        assert_is_text_line(beendete_karte, "Song 1")
        assert mc._ended_np is None

        _cleanup(mc)

    asyncio.run(run())


def test_card_after_queue_end_is_retired_by_next_song(monkeypatch, tmp_path):
    """Kompletter Weg: Song endet → Queue leer → _ended_np; der nächste Song
    baut die liegengebliebene Karte zurück."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source())

    async def run():
        dl = FakeDownloader((STREAM_INFO, "https://cdn.example/stream", "Testsong", 200))
        vc = FakeVoiceClient()
        ctx = FakeCtx(vc)
        mc = make_cog(dl)
        mc.autoplay_enabled = False

        mc.queue.append((URL, "Testsong"))
        await mc.play_next(ctx)
        erste_karte = mc.now_playing_msg

        vc.end_track()                       # Track endet, Queue ist leer
        for _ in range(20):
            await asyncio.sleep(0)
        assert mc._ended_np is not None and mc._ended_np[0] is erste_karte

        mc.queue.append((URL, "Zweiter Song"))
        mc.is_playing = True
        await mc.play_next(ctx)

        assert_is_text_line(erste_karte, "Testsong")
        assert mc.now_playing_msg is ctx.messages[-1]

        _cleanup(mc)

    asyncio.run(run())
