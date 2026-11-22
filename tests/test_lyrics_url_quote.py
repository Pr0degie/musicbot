"""!text: Künstler und Titel werden vollständig URL-kodiert.

urllib.parse.quote lässt "/" standardmäßig durch – "AC/DC" ergab damit einen
zusätzlichen Pfadabschnitt in der lyrics.ovh-URL und nie einen Treffer.
"""
import asyncio

from conftest import FakeCtx, build_cog
from cogs.music import MusicCommands


class _Resp:
    status = 200

    async def json(self, content_type=None):
        return {"lyrics": "Zeile eins"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self):
        self.urls = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        return _Resp()


def _lyrics_url(title):
    session = _Session()

    async def run():
        mc = build_cog()
        mc.current_track = ("https://www.youtube.com/watch?v=aaaaaaaaaaa", title, 200)
        mc._http = lambda: session
        await MusicCommands.lyrics_cmd.callback(mc, FakeCtx())

    asyncio.run(run())
    assert len(session.urls) == 1
    return session.urls[0]


def test_slash_im_kuenstlernamen_wird_kodiert():
    url = _lyrics_url("AC/DC - Back in Black (Official Video)")
    assert url == "https://api.lyrics.ovh/v1/AC%2FDC/Back%20in%20Black"


def test_slash_im_titel_wird_kodiert():
    url = _lyrics_url("Artist - Either/Or")
    assert url == "https://api.lyrics.ovh/v1/Artist/Either%2FOr"


def test_normale_titel_unveraendert():
    url = _lyrics_url("a-ha - Take on Me")
    assert url == "https://api.lyrics.ovh/v1/a-ha/Take%20on%20Me"
