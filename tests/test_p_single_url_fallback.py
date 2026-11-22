"""!p mit einzelner URL: fehlt webpage_url im Info-Dict, zählt die Eingabe.

Manche Extraktoren (z. B. direkte Datei-/Stream-URLs) liefern kein
webpage_url. Vorher landete dann (None, Titel) in der Queue; !next und !now
fallen über _fetch_single_track_info schon immer auf die Eingabe zurück.
"""
import asyncio

import discord

from conftest import FakeCtx, FakeDownloader, FakeVoiceClient, build_cog, cleanup_cog, make_fake_source
from cogs.music import MusicCommands

EINGABE = "https://example.com/audio/stream.mp3"


class _UrlYdl:
    def __init__(self, info):
        self.info = info

    def extract_info(self, query, download=False):
        return dict(self.info)


def _play(info, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(discord, "FFmpegOpusAudio", make_fake_source())
    dl = FakeDownloader(({"title": "Titel", "duration": 100}, "https://cdn.example/x", "Titel", 100))
    dl.url_ydl = _UrlYdl(info)

    async def run():
        mc = build_cog(dl)
        ctx = FakeCtx(FakeVoiceClient())
        await MusicCommands.p.callback(mc, ctx, eingabe=EINGABE)
        cleanup_cog(mc)
        return mc

    return asyncio.run(run()), dl


def test_ohne_webpage_url_wird_die_eingabe_eingereiht(monkeypatch, tmp_path):
    mc, dl = _play({"title": "Titel"}, monkeypatch, tmp_path)

    assert dl.primed == [(EINGABE, "Titel")]
    assert dl.resolve_calls == [EINGABE]
    assert mc.current_track[0] == EINGABE


def test_mit_webpage_url_bleibt_alles_wie_bisher(monkeypatch, tmp_path):
    kanonisch = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
    mc, dl = _play({"title": "Titel", "webpage_url": kanonisch}, monkeypatch, tmp_path)

    assert dl.primed == [(kanonisch, "Titel")]
    assert dl.resolve_calls == [kanonisch]
    assert mc.current_track[0] == kanonisch
