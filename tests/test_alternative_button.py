"""Alternativ-Button der Suche (views/music_controls.py → SearchAutoplayView).

Läuft der erste Treffer gerade, stoppt der Button ihn. Das muss über
_stop_for_advance laufen: ein roher Stopp einer wachsenden Datei sähe für
after_playing wie ein vorzeitiges Dateiende aus, und der alte Song würde
wieder vor die gewählte Alternative gereiht.
"""
import asyncio
import types

from tests.conftest import FakeCtx, FakeVoiceClient, build_cog
from views.music_controls import SearchAutoplayView


class FakeResponse:
    async def edit_message(self, **kwargs):
        pass


def test_alternative_stoppt_ueber_stop_for_advance():
    async def body():
        mc = build_cog()
        vc = FakeVoiceClient()
        vc._playing = True
        mc.current_track = ("u1", "Erster Treffer", 200)
        view = SearchAutoplayView({"title": "Erster Treffer"},
                                  [{"webpage_url": "u2", "title": "Alternative"}], mc, FakeCtx(vc))
        await view.children[0].callback(types.SimpleNamespace(response=FakeResponse()))
        assert vc.stop_calls == 1
        assert mc._suppress_resume is True, "Stopp lief nicht über _stop_for_advance"
        assert list(mc.queue)[0] == ("u2", "Alternative")
    asyncio.run(body())
