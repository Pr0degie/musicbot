"""Trackwechsel in play_next: die Karte des vorherigen Songs verschwindet.

Ergänzt tests/test_np_card_retire.py um den echten Durchlauf – inklusive des
Falls, dass now_playing_msg UND _ended_np gleichzeitig belegt sind (früher
verfiel dann eine Karte samt Buttons für immer).
"""

import asyncio

import discord

from conftest import retired_text

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
    assert retired_text(edit) == f"🎶 {title}"


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
        mc.now_playing_view = object()
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
        mc.now_playing_view = object()
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


def test_card_stays_at_queue_end_and_is_retired_by_next_song(monkeypatch, tmp_path):
    """Kompletter Weg: Song endet → Queue leer → Karte bleibt stehen
    (_ended_np); der nächste Song baut sie zur Textzeile zurück."""
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
        karten_view = mc.now_playing_view

        vc.end_track()                       # Track endet, Queue ist leer
        for _ in range(20):
            await asyncio.sleep(0)
        letzte = edits_of(erste_karte)    # höchstens der Balken auf 100 %, kein Rückbau
        assert letzte is None or letzte["view"] is karten_view, "Karte bleibt stehen"
        assert not erste_karte.deleted
        assert mc._ended_np is not None and mc._ended_np[0] is erste_karte
        assert mc.now_playing_msg is None

        mc.queue.append((URL, "Zweiter Song"))
        mc.is_playing = True
        await mc.play_next(ctx)

        assert_is_text_line(erste_karte, "Testsong")
        assert mc._ended_np is None
        assert mc.now_playing_msg is ctx.messages[-1]

        _cleanup(mc)

    asyncio.run(run())


def test_skipped_card_is_deleted_on_next_song(monkeypatch, tmp_path):
    """!s/⏭ markiert die aktuelle Karte; der nächste Song löscht sie, statt
    eine Textzeile zu hinterlassen."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source())

    async def run():
        dl = FakeDownloader((STREAM_INFO, "https://cdn.example/stream", "Testsong", 200))
        ctx = FakeCtx(FakeVoiceClient())
        mc = make_cog(dl)

        alte_karte = await ctx.send(embed=object())
        mc.now_playing_msg = alte_karte
        mc.now_playing_view = object()
        mc._np_title = "Song 1"
        mc.current_track = ("https://x/1", "Song 1", 100)
        mc._skipped_np_msg = alte_karte          # wie von !s/⏭ gesetzt

        mc.queue.append((URL, "Testsong"))
        await mc.play_next(ctx)

        assert alte_karte.deleted
        assert not alte_karte.edits
        assert mc._skipped_np_msg is None
        assert mc.now_playing_msg is ctx.messages[-1]

        _cleanup(mc)

    asyncio.run(run())


def test_skipping_last_song_keeps_card_until_next_song(monkeypatch, tmp_path):
    """Skip des letzten Songs → Queue leer → Karte bleibt stehen; erst der
    nächste Song löscht die übersprungene Karte."""
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
        karte = mc.now_playing_msg

        mc._skipped_np_msg = karte               # !s auf den letzten Song
        vc.end_track()
        for _ in range(20):
            await asyncio.sleep(0)
        assert not karte.deleted, "Queue leer → Karte bleibt"
        assert mc._ended_np[0] is karte

        mc.queue.append((URL, "Zweiter Song"))
        mc.is_playing = True
        await mc.play_next(ctx)

        assert karte.deleted
        assert mc.now_playing_msg is ctx.messages[-1]

        _cleanup(mc)

    asyncio.run(run())
