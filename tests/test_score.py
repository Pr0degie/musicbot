"""Tests für !score: Was als Play zählt und wie die Top-10 dargestellt wird.

Ein Play ist ein Hördurchgang. Setzt derselbe Track nur neu an (!seek, !eq,
Voice-Reparatur), zählt das nicht noch einmal. Titel sind Fremddaten und
dürfen das Embed nicht umformatieren.
"""

import asyncio
import re
import time
import types

import discord

from conftest import FakeCtx, FakeDownloader, FakeVoiceClient, build_cog, cleanup_cog, make_fake_source
from cogs.music import MusicCommands
from cogs.music_stats import StatsMixin

URL_A = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
URL_B = "https://www.youtube.com/watch?v=bbbbbbbbbbb"

INFO = {"title": "A", "webpage_url": URL_A, "thumbnail": None, "uploader": None, "duration": 200}


async def _wait_for_plays(vc, count):
    for _ in range(200):
        if len(vc.play_calls) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"nur {len(vc.play_calls)} von {count} Wiedergaben gestartet")


async def _playing_cog():
    """Cog, in dem Song A gerade läuft (ein Play gezählt)."""
    dl = FakeDownloader((INFO, "https://cdn.example/a", "A", 200))
    vc = FakeVoiceClient()
    ctx = FakeCtx(vc)
    mc = build_cog(dl)
    mc.queue.append((URL_A, "A"))
    await mc.play_next(ctx)
    assert mc._play_counts == {URL_A: 1}
    return mc, vc, ctx


def _run(monkeypatch, tmp_path, body):
    monkeypatch.chdir(tmp_path)   # last_queue.json landet im Temp-Verzeichnis
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source())
    asyncio.run(body())


# --- Was als Play zählt ------------------------------------------------------

def test_seek_zaehlt_nicht_erneut(monkeypatch, tmp_path):
    async def body():
        mc, vc, ctx = await _playing_cog()

        await MusicCommands.seek.callback(mc, ctx, "1:30")
        vc.end_track()
        await _wait_for_plays(vc, 2)

        assert mc.current_track[0] == URL_A
        assert mc._play_counts == {URL_A: 1}, "!seek ist derselbe Hördurchgang"
        assert mc._restart_url is None, "Markierung ist verbraucht"
        cleanup_cog(mc)

    _run(monkeypatch, tmp_path, body)


def test_eq_wechsel_zaehlt_nicht_erneut(monkeypatch, tmp_path):
    async def body():
        mc, vc, ctx = await _playing_cog()
        other = next(name for name in mc.eq_presets if name != mc.equalizer)

        await MusicCommands.eq.callback(mc, ctx, other)
        vc.end_track()
        await _wait_for_plays(vc, 2)

        assert mc.equalizer == other
        assert mc._play_counts == {URL_A: 1}, "!eq startet denselben Song nur neu"
        cleanup_cog(mc)

    _run(monkeypatch, tmp_path, body)


def test_voice_reparatur_zaehlt_nicht_erneut(monkeypatch, tmp_path):
    async def body():
        mc, vc, ctx = await _playing_cog()
        mc.track_start_time = time.monotonic() - 50

        frozen = mc._freeze_playback(vc)
        assert frozen.restart_track
        vc.end_track()
        await _wait_for_plays(vc, 2)

        assert mc._play_counts == {URL_A: 1}, "Fortsetzung nach der Reparatur ist kein neuer Play"
        cleanup_cog(mc)

    _run(monkeypatch, tmp_path, body)


def test_abgesagte_reparatur_hinterlaesst_keine_markierung(monkeypatch, tmp_path):
    async def body():
        mc, vc, ctx = await _playing_cog()
        frozen = mc._freeze_playback(vc)
        mc._drop_frozen(frozen)
        assert mc._restart_url is None
        cleanup_cog(mc)

    _run(monkeypatch, tmp_path, body)


def test_erneutes_abspielen_nach_seek_zaehlt_wieder(monkeypatch, tmp_path):
    """Die Markierung ist ein One-Shot: derselbe Song später noch einmal aus
    der Queue ist ein neuer Play."""
    async def body():
        mc, vc, ctx = await _playing_cog()
        await MusicCommands.seek.callback(mc, ctx, "0:30")
        vc.end_track()
        await _wait_for_plays(vc, 2)

        mc.queue.append((URL_A, "A"))
        vc.end_track()
        await _wait_for_plays(vc, 3)

        assert mc._play_counts == {URL_A: 2}
        cleanup_cog(mc)

    _run(monkeypatch, tmp_path, body)


def test_markierung_gilt_nicht_fuer_dazwischengeschobenen_track(monkeypatch, tmp_path):
    """Schiebt sich nach dem !seek ein anderer Song nach vorn, wird dessen Play
    gezählt – die Markierung gehört nur zu Song A."""
    async def body():
        mc, vc, ctx = await _playing_cog()
        await MusicCommands.seek.callback(mc, ctx, "0:30")
        mc.queue.appendleft((URL_B, "B"))
        vc.end_track()
        await _wait_for_plays(vc, 2)

        assert mc.current_track[0] == URL_B
        assert mc._play_counts[URL_B] == 1
        assert mc._restart_url is None
        cleanup_cog(mc)

    _run(monkeypatch, tmp_path, body)


# --- Darstellung -------------------------------------------------------------

def _score(play_counts):
    ctx = FakeCtx()
    mc = types.SimpleNamespace(_play_counts=play_counts)
    asyncio.run(StatsMixin.score.callback(mc, ctx))
    return ctx


def _unescaped(text, token):
    """Kommt `token` irgendwo ohne Backslash davor vor?"""
    return re.search(r"(?<!\\)" + re.escape(token), text) is not None


def test_score_ohne_plays_meldet_das():
    ctx = _score({})
    assert len(ctx.sent) == 1
    assert ctx.texts and "embed" not in ctx.sent[0][1]


def test_score_zeigt_top_10_absteigend():
    counts = {f"u{i}": {"title": f"Song {i}", "count": i} for i in range(1, 13)}
    ctx = _score(counts)
    lines = ctx.sent[0][1]["embed"].description.split("\n")

    assert len(lines) == 10
    assert lines[0] == "🥇 **Song 12** — 12x"
    assert lines[1].startswith("🥈 ") and lines[2].startswith("🥉 ")
    assert lines[3] == "`4.` **Song 9** — 9x"
    assert lines[9] == "`10.` **Song 3** — 3x"


def test_score_escaped_markdown_in_titeln():
    """Hässliche Echtdaten: Sternchen, Unterstriche, Spoiler, Backticks,
    Klammern, Pipes und Emojis dürfen die Zeile nicht umformatieren."""
    titles = [
        "A*STAR - **Bold** _kursiv_",
        "Geheim || Spoiler || Ende",
        "Song `code` ~~weg~~",
        "Fake [Live](https://evil.example/x)",
        "CHiTO - SPRITE N SODA [Snipessoundboothcontest]",
        "Artist | Titel (Official Video) 🔥🎶",
        "悲しみがとまらない　I CAN'T STOP THE LONELINESS",
    ]
    counts = {f"u{i}": {"title": title, "count": 20 - i} for i, title in enumerate(titles)}
    lines = _score(counts).sent[0][1]["embed"].description.split("\n")
    assert len(lines) == len(titles)

    for line, title in zip(lines, titles):
        prefix, rest = line.split(" **", 1)
        body, count = rest.rsplit("** — ", 1)
        assert count.endswith("x")
        for token in ("*", "_", "`", "~", "||"):
            assert not _unescaped(body, token), f"{token!r} unescaped in {line!r}"
        assert not re.search(r"(?<!\\)\[[^\]]*\]\(", body), f"Masked Link in {line!r}"
        # Der sichtbare Titel bleibt derselbe.
        assert body.replace("\\", "") == title

    # Harmlose Titel bleiben unverändert lesbar.
    assert lines[4] == "`5.` **CHiTO - SPRITE N SODA [Snipessoundboothcontest]** — 16x"
    assert "🔥🎶" in lines[5]
