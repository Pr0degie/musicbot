"""Tests für das Auswahlmenü der !q-Ansicht (Entfernen / Als Nächstes).

Die Ansicht ist ein Schnappschuss – jede Aktion muss den gewählten Song in
der echten Queue wiederfinden, auch wenn die sich inzwischen verschoben hat.
"""

import asyncio
import types
from collections import deque

from conftest import FakeCtx, build_cog
from utils.i18n import t
from views.queue_view import PAGE_MAX_ITEMS, QueueView


class FakeInteraction:
    def __init__(self):
        self.edited = []
        self.followups = []
        self.sent = []
        self.user = types.SimpleNamespace(voice=None)
        self.response = types.SimpleNamespace(edit_message=self._edit, send_message=self._send)
        self.followup = types.SimpleNamespace(send=self._followup)

        async def _is_owner(_user):
            return False

        self.client = types.SimpleNamespace(is_owner=_is_owner)

    async def _edit(self, **kwargs):
        self.edited.append(kwargs)

    async def _send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))

    async def _followup(self, content=None, **kwargs):
        self.followups.append((content, kwargs))


def _make(*titles):
    cog = build_cog()
    cog.dl = types.SimpleNamespace(_url_cache={})
    cog._autoplay_queued_url = None
    restarts = []
    cog._restart_prefetch = lambda: restarts.append(True)
    cog.queue.extend((f"u{i}", title) for i, title in enumerate(titles))
    snapshot, autoplay_next = cog._queue_snapshot()
    view = QueueView(snapshot, None, None, autoplay_next_title=autoplay_next,
                     music_cog=cog, ctx=FakeCtx())
    return view, cog, restarts


async def _pick(view, index):
    view.select._values = [str(index)]   # was Discord nach der Auswahl liefert
    await view.select.callback(FakeInteraction())


def test_no_controls_without_cog_or_on_empty_queue():
    async def run():
        plain = QueueView([("u1", "A", 100)], None, None)
        assert plain.select not in plain.children

        empty, _cog, _ = _make()
        assert empty.select not in empty.children

    asyncio.run(run())


def test_actions_disabled_until_a_song_is_picked():
    async def run():
        view, _cog, _ = _make("A", "B", "C")
        assert [o.label for o in view.select.options] == ["1. A", "2. B", "3. C"]
        assert view.remove_btn.disabled and view.move_btn.disabled

        await _pick(view, 1)

        assert not view.remove_btn.disabled and not view.move_btn.disabled
        assert [o.default for o in view.select.options] == [False, True, False]

    asyncio.run(run())


def test_remove_selected_song():
    async def run():
        view, cog, restarts = _make("A", "B", "C")
        await _pick(view, 1)
        interaction = FakeInteraction()

        await view.remove_btn.callback(interaction)

        assert list(cog.queue) == [("u0", "A"), ("u2", "C")]
        assert isinstance(cog.queue, deque)
        assert restarts == [True], "Prefetch zielt sonst auf die alte Reihenfolge"
        assert interaction.followups == [(t("status.removed", title="B"), {"ephemeral": True})]
        assert "1. A\n2. C" in interaction.edited[0]["content"]
        assert view.selected is None and view.remove_btn.disabled

    asyncio.run(run())


def test_move_selected_song_to_front():
    async def run():
        view, cog, _ = _make("A", "B", "C")
        await _pick(view, 2)
        interaction = FakeInteraction()

        await view.move_btn.callback(interaction)

        assert list(cog.queue) == [("u2", "C"), ("u0", "A"), ("u1", "B")]
        assert interaction.followups == [(t("status.moved_to_front", title="C"), {"ephemeral": True})]

    asyncio.run(run())


def test_action_finds_song_after_queue_shifted():
    """Inzwischen wurde A gespielt – B steht jetzt an Position 1, nicht 2."""
    async def run():
        view, cog, _ = _make("A", "B", "C")
        await _pick(view, 1)
        cog.queue.popleft()

        await view.remove_btn.callback(FakeInteraction())

        assert list(cog.queue) == [("u2", "C")]

    asyncio.run(run())


def test_action_on_vanished_song_only_reloads():
    async def run():
        view, cog, restarts = _make("A", "B")
        await _pick(view, 1)
        cog.queue.pop()   # B ist weg
        interaction = FakeInteraction()

        await view.remove_btn.callback(interaction)

        assert list(cog.queue) == [("u0", "A")]
        assert restarts == []
        assert interaction.followups == [(t("error.queue_item_gone"), {"ephemeral": True})]
        assert [o.label for o in view.select.options] == ["1. A"]

    asyncio.run(run())


def test_page_never_exceeds_select_limit():
    async def run():
        view, _cog, _ = _make(*[f"S{i}" for i in range(60)])
        assert [len(p) for p in view._pages] == [PAGE_MAX_ITEMS, PAGE_MAX_ITEMS, 10]
        assert len(view.select.options) == PAGE_MAX_ITEMS

        view.page = 2
        view._update_buttons()
        assert [o.value for o in view.select.options] == [str(i) for i in range(50, 60)]

    asyncio.run(run())
