"""Characterization-Tests: Queue-Operationen (!remove, !move, !shuffle) und
_evict_autoplay_song.

Dokumentiert das IST-Verhalten der Queue-Manipulation, damit die geplanten
Umbauten (Security/Effizienz/Refactoring/UX) Regressionen sofort sichtbar machen.
"""

import asyncio
from collections import deque

import pytest

import cogs.music as music_mod
from cogs.music import MusicCommands
from conftest import FakeCtx, build_cog
from utils.i18n import t

URL_A = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
URL_B = "https://www.youtube.com/watch?v=bbbbbbbbbbb"
URL_C = "https://www.youtube.com/watch?v=ccccccccccc"


def make_cog_with_queue(*tracks):
    mc = build_cog()
    mc.queue.extend(tracks)
    return mc


# ---------------------------------------------------------------------------
# !remove
# ---------------------------------------------------------------------------

def test_remove_valid_index_removes_and_reports():
    mc = make_cog_with_queue((URL_A, "Song A"), (URL_B, "Song B"), (URL_C, "Song C"))
    ctx = FakeCtx()

    asyncio.run(MusicCommands.remove.callback(mc, ctx, 2))

    assert list(mc.queue) == [(URL_A, "Song A"), (URL_C, "Song C")]
    assert ctx.texts == [t("status.removed", title="Song B")]


def test_remove_index_is_one_based():
    mc = make_cog_with_queue((URL_A, "Song A"), (URL_B, "Song B"))
    ctx = FakeCtx()

    asyncio.run(MusicCommands.remove.callback(mc, ctx, 1))

    assert list(mc.queue) == [(URL_B, "Song B")]


@pytest.mark.parametrize("index", [0, -1, 3, 999])
def test_remove_out_of_range_leaves_queue_untouched(index):
    mc = make_cog_with_queue((URL_A, "Song A"), (URL_B, "Song B"))
    ctx = FakeCtx()

    asyncio.run(MusicCommands.remove.callback(mc, ctx, index))

    assert list(mc.queue) == [(URL_A, "Song A"), (URL_B, "Song B")]
    assert ctx.texts == [t("error.invalid_index")]


def test_remove_rebuilds_queue_as_deque():
    """IST: remove ersetzt self.queue durch eine neue deque (kein in-place pop)."""
    mc = make_cog_with_queue((URL_A, "Song A"), (URL_B, "Song B"))
    original = mc.queue
    ctx = FakeCtx()

    asyncio.run(MusicCommands.remove.callback(mc, ctx, 1))

    assert isinstance(mc.queue, deque)
    assert mc.queue is not original


# ---------------------------------------------------------------------------
# !move
# ---------------------------------------------------------------------------

def test_move_by_number_moves_to_front():
    mc = make_cog_with_queue((URL_A, "Song A"), (URL_B, "Song B"), (URL_C, "Song C"))
    ctx = FakeCtx()

    asyncio.run(MusicCommands.move.callback(mc, ctx, term="3"))

    assert list(mc.queue) == [(URL_C, "Song C"), (URL_A, "Song A"), (URL_B, "Song B")]
    assert ctx.texts == [t("status.moved_to_front", title="Song C")]


@pytest.mark.parametrize("term", ["0", "4", "-1"])
def test_move_number_out_of_range(term):
    mc = make_cog_with_queue((URL_A, "Song A"), (URL_B, "Song B"), (URL_C, "Song C"))
    ctx = FakeCtx()

    asyncio.run(MusicCommands.move.callback(mc, ctx, term=term))

    assert list(mc.queue) == [(URL_A, "Song A"), (URL_B, "Song B"), (URL_C, "Song C")]
    assert ctx.texts == [t("error.invalid_index_range", count=3)]


def test_move_by_title_substring_case_insensitive_first_match():
    mc = make_cog_with_queue(
        (URL_A, "Take On Me – a-ha"),
        (URL_B, "Africa – Toto"),
        (URL_C, "Africa Remix"),
    )
    ctx = FakeCtx()

    asyncio.run(MusicCommands.move.callback(mc, ctx, term="AFRICA"))

    # Erster Treffer (Position 2) wird verschoben, nicht der Remix.
    assert list(mc.queue)[0] == (URL_B, "Africa – Toto")
    assert list(mc.queue)[1:] == [(URL_A, "Take On Me – a-ha"), (URL_C, "Africa Remix")]


def test_move_title_not_found():
    mc = make_cog_with_queue((URL_A, "Song A"))
    ctx = FakeCtx()

    asyncio.run(MusicCommands.move.callback(mc, ctx, term="gibtsnicht"))

    assert list(mc.queue) == [(URL_A, "Song A")]
    assert ctx.texts == [t("error.song_not_found_in_queue", term="gibtsnicht")]


def test_move_already_first_is_noop_with_message():
    mc = make_cog_with_queue((URL_A, "Song A"), (URL_B, "Song B"))
    ctx = FakeCtx()

    asyncio.run(MusicCommands.move.callback(mc, ctx, term="1"))

    assert list(mc.queue) == [(URL_A, "Song A"), (URL_B, "Song B")]
    assert ctx.texts == [t("status.already_first", title="Song A")]


def test_move_on_empty_queue():
    mc = build_cog()
    ctx = FakeCtx()

    asyncio.run(MusicCommands.move.callback(mc, ctx, term="1"))

    assert ctx.texts == [t("error.queue_empty")]


def test_move_numeric_title_is_treated_as_index():
    """IST: int(term) hat Vorrang – ein Song mit rein numerischem Titel ist
    per Titelsuche nicht erreichbar, die Zahl wird immer als Index gelesen."""
    mc = make_cog_with_queue((URL_A, "99 Luftballons"), (URL_B, "42"))
    ctx = FakeCtx()

    asyncio.run(MusicCommands.move.callback(mc, ctx, term="2"))

    # "2" greift als Index (Song B nach vorn), nicht als Titelsuche.
    assert list(mc.queue) == [(URL_B, "42"), (URL_A, "99 Luftballons")]


# ---------------------------------------------------------------------------
# !shuffle
# ---------------------------------------------------------------------------

def test_shuffle_too_few_tracks():
    mc = make_cog_with_queue((URL_A, "Song A"))
    ctx = FakeCtx()

    asyncio.run(MusicCommands.shuffle.callback(mc, ctx))

    assert list(mc.queue) == [(URL_A, "Song A")]
    assert ctx.texts == [t("error.not_enough_to_shuffle")]


def test_shuffle_converts_to_list_and_back(monkeypatch):
    """IST: shuffle wandelt in Liste um, mischt via random.shuffle und baut
    eine neue deque – kein Element geht verloren."""
    mc = make_cog_with_queue((URL_A, "Song A"), (URL_B, "Song B"), (URL_C, "Song C"))
    ctx = FakeCtx()
    monkeypatch.setattr(music_mod.random, "shuffle", lambda lst: lst.reverse())

    asyncio.run(MusicCommands.shuffle.callback(mc, ctx))

    assert isinstance(mc.queue, deque)
    assert list(mc.queue) == [(URL_C, "Song C"), (URL_B, "Song B"), (URL_A, "Song A")]
    assert ctx.texts == [t("status.shuffled")]


# ---------------------------------------------------------------------------
# _evict_autoplay_song
# ---------------------------------------------------------------------------

def test_evict_removes_exactly_the_autoplay_entry():
    mc = make_cog_with_queue((URL_A, "Song A"), (URL_B, "Autoplay-Song"), (URL_C, "Song C"))
    mc._autoplay_queued_url = URL_B

    removed = mc._evict_autoplay_song()

    assert removed == "Autoplay-Song"
    assert list(mc.queue) == [(URL_A, "Song A"), (URL_C, "Song C")]
    assert mc._autoplay_queued_url is None


def test_evict_without_marker_returns_none():
    mc = make_cog_with_queue((URL_A, "Song A"))

    assert mc._evict_autoplay_song() is None
    assert list(mc.queue) == [(URL_A, "Song A")]


def test_evict_marker_not_in_queue_keeps_marker():
    """IST: Zeigt _autoplay_queued_url auf eine URL, die nicht (mehr) in der
    Queue liegt, bleibt der Marker gesetzt – er wird nur beim Treffer gelöscht."""
    mc = make_cog_with_queue((URL_A, "Song A"))
    mc._autoplay_queued_url = URL_B

    removed = mc._evict_autoplay_song()

    assert removed is None
    assert mc._autoplay_queued_url == URL_B


def test_evict_cancels_running_prefetch_task():
    async def run():
        mc = build_cog()
        started = asyncio.Event()

        async def hang():
            started.set()
            await asyncio.sleep(60)

        task = asyncio.create_task(hang())
        mc._autoplay_prefetch_task = task
        await started.wait()

        mc._evict_autoplay_song()

        assert mc._autoplay_prefetch_task is None
        await asyncio.sleep(0)
        assert task.cancelled()

    asyncio.run(run())


def test_evict_removes_only_first_match_of_duplicate_urls():
    """IST: Bei doppelt eingereihter Autoplay-URL wird nur der erste Eintrag entfernt."""
    mc = make_cog_with_queue((URL_B, "Autoplay-Song"), (URL_B, "Autoplay-Song"))
    mc._autoplay_queued_url = URL_B

    mc._evict_autoplay_song()

    assert list(mc.queue) == [(URL_B, "Autoplay-Song")]
