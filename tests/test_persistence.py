"""Tests für die gedebouncte Persistenz (play_counts.json).

Soll-Verhalten der Dauerbetrieb-Optimierung: _record_play schreibt nicht mehr
sofort auf Disk, sondern setzt nur ein Dirty-Flag. Geschrieben wird gebündelt
vom 30-s-Flush-Loop (asyncio.to_thread) – und garantiert beim Entladen des
Cogs (cog_unload) sowie vor !restart.
"""

import asyncio
import json

from cogs.downloader import Downloader
from cogs.music import MusicCommands
from conftest import build_cog

URL = "https://www.youtube.com/watch?v=aaaaaaaaaaa"


def _make_cog_with_real_dl():
    """Cog mit echtem (aber leerem) Downloader – cog_unload fasst self.dl an."""
    dl = Downloader.__new__(Downloader)
    dl._url_cache = {}
    dl._cache_timestamps = {}
    dl._pending_resolves = {}
    return build_cog(dl=dl)


def test_record_play_does_not_write_immediately(tmp_path, monkeypatch):
    score_file = tmp_path / "play_counts.json"
    monkeypatch.setattr("cogs.music.SCORE_FILE", score_file)

    async def run():
        mc = _make_cog_with_real_dl()
        MusicCommands._record_play(mc, URL, "Song A")
        assert mc._score_dirty is True
        assert not score_file.exists(), "Debounce: kein sofortiger Disk-Write mehr"

    asyncio.run(run())


def test_flush_loop_writes_and_clears_dirty(tmp_path, monkeypatch):
    score_file = tmp_path / "play_counts.json"
    monkeypatch.setattr("cogs.music.SCORE_FILE", score_file)

    async def run():
        mc = _make_cog_with_real_dl()
        MusicCommands._record_play(mc, URL, "Song A")
        MusicCommands._record_play(mc, URL, "Song A")
        # Loop-Body direkt aufrufen (der echte Loop feuert alle 30 s)
        await MusicCommands._persist_flush_loop.coro(mc)
        assert mc._score_dirty is False
        return json.loads(score_file.read_text(encoding="utf-8"))

    data = asyncio.run(run())
    assert data == {URL: {"title": "Song A", "count": 2}}


def test_flush_loop_skips_when_not_dirty(tmp_path, monkeypatch):
    score_file = tmp_path / "play_counts.json"
    monkeypatch.setattr("cogs.music.SCORE_FILE", score_file)

    async def run():
        mc = _make_cog_with_real_dl()
        await MusicCommands._persist_flush_loop.coro(mc)

    asyncio.run(run())
    assert not score_file.exists()


def test_cog_unload_flushes_dirty_play_counts(tmp_path, monkeypatch):
    score_file = tmp_path / "play_counts.json"
    monkeypatch.setattr("cogs.music.SCORE_FILE", score_file)

    async def run():
        mc = _make_cog_with_real_dl()
        MusicCommands._record_play(mc, URL, "Song A")
        await mc.cog_unload()

    asyncio.run(run())
    data = json.loads(score_file.read_text(encoding="utf-8"))
    assert data == {URL: {"title": "Song A", "count": 1}}
