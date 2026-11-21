"""Schneller YouTube-Client (visionos) mit Rückfall auf die yt_dlp-Standard-Clients.

Hintergrund (gemessen 22.11.2026): Mit Node-Runtime wählt yt_dlp die TV-URL,
deren available_at den Abruf ~4–5 s sperrt – das war der Großteil der
Startverzögerung neuer Songs. visionos liefert dieselbe Opus-Spur ohne Sperre.
"""

import asyncio

import pytest

import cogs.downloader as dlmod
from cogs.downloader import FAST_PLAYER_CLIENTS, Downloader
from tests.test_cookie_mode import FakeYdl, bare_downloader


def _clients(ydl):
    return ydl.params.get("extractor_args", {}).get("youtube", {}).get("player_client")


def test_cookieless_instances_use_fast_client():
    dl = bare_downloader()
    for kind in ("main", "url", "playlist"):
        assert _clients(dl._build_ydl(kind)) == list(FAST_PLAYER_CLIENTS), kind
    assert _clients(dl._make_progressive_ydl()) == list(FAST_PLAYER_CLIENTS)


def test_cookie_mode_and_fallback_keep_ytdlp_defaults():
    assert _clients(bare_downloader(cookie_mode=True)._build_ydl("main")) is None
    assert _clients(bare_downloader()._build_ydl("url", fast=False)) is None


def test_format_error_retries_once_with_default_clients(monkeypatch):
    monkeypatch.setattr(dlmod, "YDL_BROWSER", "")
    monkeypatch.setattr(dlmod, "YDL_COOKIES_FILE", "")
    dl = bare_downloader()
    dl.ydl = FakeYdl(exc=Exception("ERROR: [youtube] abc: Requested format is not available"))
    built = []

    def fake_build(self, kind, fast=True):
        built.append((kind, fast))
        return FakeYdl(result={"title": "Rückfall"})

    monkeypatch.setattr(Downloader, "_build_ydl", fake_build)

    out = asyncio.run(dl.extract_info_async("https://youtu.be/abcdefghijk", "main"))

    assert out == {"title": "Rückfall"}
    assert built == [("main", False)], "genau ein Zweitversuch ohne schnellen Client"
    assert dl._cookie_mode is False


def test_other_errors_are_not_retried(monkeypatch):
    monkeypatch.setattr(dlmod, "YDL_BROWSER", "")
    monkeypatch.setattr(dlmod, "YDL_COOKIES_FILE", "")
    dl = bare_downloader()
    dl.url_ydl = FakeYdl(exc=Exception("HTTP Error 404: Not Found"))
    monkeypatch.setattr(Downloader, "_build_ydl", lambda *a, **k: pytest.fail("kein Rückfall"))

    with pytest.raises(Exception, match="404"):
        asyncio.run(dl.extract_info_async("https://x/1", "url"))


def test_no_client_fallback_in_cookie_mode(monkeypatch):
    dl = bare_downloader(cookie_mode=True)
    dl.ydl = FakeYdl(exc=Exception("Requested format is not available"))
    monkeypatch.setattr(Downloader, "_build_ydl", lambda *a, **k: pytest.fail("kein Rückfall"))

    with pytest.raises(Exception, match="Requested format"):
        asyncio.run(dl.extract_info_async("https://x/1", "main"))
