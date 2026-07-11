"""Tests für die !loadq-Validierung: ungültige Dateien → i18n-Fehlermeldung
statt Traceback; valide (saveq-Format) laden exakt wie bisher."""

import asyncio
import json

import pytest

import cogs.music_queue_io as qio
from cogs.music import MusicCommands
from cogs.music_queue_io import _is_valid_playlist
from conftest import FakeCtx, build_cog
from utils.i18n import t

URL_A = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
URL_B = "https://www.youtube.com/watch?v=bbbbbbbbbbb"


@pytest.fixture
def playlists_dir(monkeypatch, tmp_path):
    d = tmp_path / "playlists"
    d.mkdir()
    monkeypatch.setattr(qio, "PLAYLISTS_DIR", d)
    return d


def write_playlist(playlists_dir, name, content, raw=False):
    path = playlists_dir / f"{name}.json"
    if raw:
        path.write_text(content, encoding="utf-8")
    else:
        path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    return path


def load(mc, ctx, name):
    asyncio.run(MusicCommands.loadq.callback(mc, ctx, name=name))


# ---------------------------------------------------------------------------
# Roundtrip: eine mit !saveq gespeicherte Datei lädt exakt wie bisher
# ---------------------------------------------------------------------------

def test_saveq_roundtrip_loads_unchanged(playlists_dir):
    saver = build_cog()
    saver.current_track = (URL_A, "Song A", 180)
    saver.queue.append((URL_B, "Song B"))
    asyncio.run(MusicCommands.saveq.callback(saver, FakeCtx(), name="mix"))

    loader = build_cog()
    loader.is_playing = True  # kein play_next-Autostart nötig
    ctx = FakeCtx()
    load(loader, ctx, "mix")

    assert list(loader.queue) == [(URL_A, "Song A"), (URL_B, "Song B")]
    assert ctx.texts == [t("status.queue_loaded", name="mix", count=2)]


# ---------------------------------------------------------------------------
# Ungültige Dateien → Fehlermeldung, Queue unangetastet, kein Traceback
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("content,raw", [
    ("{kein json", True),                          # kaputtes JSON
    ({"url": URL_A, "title": "A"}, False),         # Dict statt Liste
    ([[URL_A, "A", "extra"]], False),              # 3er-Tupel
    ([[URL_A]], False),                            # nur URL
    ([[URL_A, 42]], False),                        # Titel kein String
    ([URL_A, URL_B], False),                       # flache String-Liste
    ([[URL_A, "A"], "kaputt"], False),             # gemischt
])
def test_loadq_rejects_invalid_structure(playlists_dir, content, raw):
    write_playlist(playlists_dir, "kaputt", content, raw=raw)
    mc = build_cog()
    mc.is_playing = True
    ctx = FakeCtx()

    load(mc, ctx, "kaputt")  # darf keinen Traceback werfen

    assert list(mc.queue) == []
    assert ctx.texts == [t("error.playlist_invalid", name="kaputt")]


def test_loadq_rejects_oversized_playlist(playlists_dir):
    limit = MusicCommands.HARD_PLAYLIST_LIMIT
    write_playlist(playlists_dir, "riesig", [[URL_A, f"Song {i}"] for i in range(limit + 1)])
    mc = build_cog()
    mc.is_playing = True
    ctx = FakeCtx()

    load(mc, ctx, "riesig")

    assert list(mc.queue) == []
    assert ctx.texts == [t("error.playlist_too_large", name="riesig", limit=limit)]


def test_loadq_accepts_exactly_limit_entries(playlists_dir):
    limit = MusicCommands.HARD_PLAYLIST_LIMIT
    write_playlist(playlists_dir, "voll", [[URL_A, f"Song {i}"] for i in range(limit)])
    mc = build_cog()
    mc.is_playing = True
    ctx = FakeCtx()

    load(mc, ctx, "voll")

    assert len(mc.queue) == limit
    assert ctx.texts == [t("status.queue_loaded", name="voll", count=limit)]


# ---------------------------------------------------------------------------
# _is_valid_playlist direkt
# ---------------------------------------------------------------------------

def test_is_valid_playlist_accepts_empty_list():
    assert _is_valid_playlist([]) is True


def test_is_valid_playlist_accepts_pairs():
    assert _is_valid_playlist([[URL_A, "A"], [URL_B, "B"]]) is True


@pytest.mark.parametrize("bad", [None, "text", 42, {}, [(1, 2)], [["a", "b"], None]])
def test_is_valid_playlist_rejects(bad):
    assert _is_valid_playlist(bad) is False
