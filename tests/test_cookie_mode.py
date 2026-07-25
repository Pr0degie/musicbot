"""Tests für den cookielosen Normalbetrieb mit Cookie-Fallback (ADR 0010).

Regeln: Ohne Cookies starten (eine angemeldete YouTube-Session bekommt Pre-Roll-
Werbung, deren Skip-Zeit yt_dlp vor dem ersten Byte abwarten muss). Erst wenn
YouTube eine angemeldete Session verlangt – Alterssperre, Bot-Check,
Mitglieder-Video – wird einmal mit Cookies wiederholt und der Modus bleibt an.
"""

import asyncio

import pytest

import cogs.downloader as dlmod
from cogs.downloader import Downloader


def bare_downloader(cookie_mode=False):
    """Downloader ohne __init__ – keine yt_dlp-Instanzen, kein Datei-Load."""
    dl = Downloader.__new__(Downloader)
    dl.audio_format = "webm"
    dl._cookie_mode = cookie_mode
    dl._url_cache = {}
    dl._cache_timestamps = {}
    dl._pending_resolves = {}
    dl._progressive = {}
    dl._progressive_files = {}
    dl._progressive_blocked = {}
    dl._incomplete_files = set()
    dl._inflight = {}
    return dl


class FakeYdl:
    """extract_info liefert ein Ergebnis oder wirft – je nach Instanz."""

    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.calls = []

    def extract_info(self, query, download=False):
        self.calls.append(query)
        if self.exc:
            raise self.exc
        return self.result


# ---------------------------------------------------------------------------
# _cookie_opts / _needs_cookies
# ---------------------------------------------------------------------------

def test_cookie_opts_empty_while_cookieless(monkeypatch):
    monkeypatch.setattr(dlmod, "YDL_COOKIES_FILE", "")
    monkeypatch.setattr(dlmod, "YDL_BROWSER", "firefox")
    assert bare_downloader()._cookie_opts() == {}


def test_cookie_opts_uses_browser_in_cookie_mode(monkeypatch):
    monkeypatch.setattr(dlmod, "YDL_COOKIES_FILE", "")
    monkeypatch.setattr(dlmod, "YDL_BROWSER", "firefox")
    assert bare_downloader(cookie_mode=True)._cookie_opts() == {"cookiesfrombrowser": ("firefox",)}


def test_cookie_file_takes_priority_over_browser(monkeypatch):
    monkeypatch.setattr(dlmod, "YDL_COOKIES_FILE", "C:/cookies.txt")
    monkeypatch.setattr(dlmod, "YDL_BROWSER", "firefox")
    assert bare_downloader(cookie_mode=True)._cookie_opts() == {"cookiefile": "C:/cookies.txt"}


@pytest.mark.parametrize("msg", [
    "ERROR: Sign in to confirm you're not a bot",
    "Sign in to confirm your age. This video may be inappropriate for some users.",
    "ERROR: This video is available to this channel's members",
    "Private video. Sign in if you've been granted access to this video",
    "Use --cookies-from-browser or --cookies for the authentication",
])
def test_needs_cookies_detects_auth_errors(msg):
    assert Downloader._needs_cookies(Exception(msg)) is True


@pytest.mark.parametrize("msg", [
    "HTTP Error 404: Not Found",
    "Requested format is not available",
    "unable to download video data: HTTP Error 403: Forbidden",
    "[Errno 11001] getaddrinfo failed",
])
def test_needs_cookies_ignores_other_errors(msg):
    assert Downloader._needs_cookies(Exception(msg)) is False


# ---------------------------------------------------------------------------
# enable_cookie_mode
# ---------------------------------------------------------------------------

def test_enable_cookie_mode_rebuilds_and_clears_cache(monkeypatch):
    monkeypatch.setattr(dlmod, "YDL_COOKIES_FILE", "")
    monkeypatch.setattr(dlmod, "YDL_BROWSER", "firefox")
    dl = bare_downloader()
    dl._url_cache["u"] = {"title": "T"}
    dl._cache_timestamps["u"] = 1.0
    dl._pending_resolves["u"] = object()
    rebuilt = []
    monkeypatch.setattr(Downloader, "_init_ydl", lambda self: rebuilt.append(True))

    assert dl.enable_cookie_mode("Testgrund") is True
    assert dl._cookie_mode is True
    assert rebuilt == [True], "Instanzen müssen mit Cookies neu gebaut werden"
    assert dl._url_cache == {} and dl._cache_timestamps == {} and dl._pending_resolves == {}, \
        "cookielos aufgelöste Metadaten dürfen nicht weiterverwendet werden"


def test_enable_cookie_mode_is_idempotent(monkeypatch):
    monkeypatch.setattr(dlmod, "YDL_COOKIES_FILE", "")
    monkeypatch.setattr(dlmod, "YDL_BROWSER", "firefox")
    dl = bare_downloader(cookie_mode=True)
    monkeypatch.setattr(Downloader, "_init_ydl", lambda self: pytest.fail("kein zweiter Rebuild"))
    assert dl.enable_cookie_mode("nochmal") is False


def test_enable_cookie_mode_without_cookie_source(monkeypatch):
    monkeypatch.setattr(dlmod, "YDL_COOKIES_FILE", "")
    monkeypatch.setattr(dlmod, "YDL_BROWSER", "")
    dl = bare_downloader()
    monkeypatch.setattr(Downloader, "_init_ydl", lambda self: pytest.fail("nichts umzuschalten"))
    assert dl.enable_cookie_mode("egal") is False
    assert dl._cookie_mode is False


# ---------------------------------------------------------------------------
# extract_info_async
# ---------------------------------------------------------------------------

def test_extract_info_async_no_retry_on_success(monkeypatch):
    monkeypatch.setattr(dlmod, "YDL_BROWSER", "firefox")
    monkeypatch.setattr(dlmod, "YDL_COOKIES_FILE", "")
    dl = bare_downloader()
    dl.search_ydl = FakeYdl(result={"entries": []})

    out = asyncio.run(dl.extract_info_async("ytsearch3:x", "search"))

    assert out == {"entries": []}
    assert dl._cookie_mode is False, "ohne Fehler kein Cookie-Modus"


def test_extract_info_async_retries_with_cookies(monkeypatch):
    monkeypatch.setattr(dlmod, "YDL_BROWSER", "firefox")
    monkeypatch.setattr(dlmod, "YDL_COOKIES_FILE", "")
    dl = bare_downloader()
    dl.ydl = FakeYdl(exc=Exception("ERROR: Sign in to confirm your age"))

    # _init_ydl tauscht die Instanz gegen eine, die liefert – wie im echten Rebuild.
    def fake_init(self):
        self.ydl = FakeYdl(result={"title": "Gesperrt"})
    monkeypatch.setattr(Downloader, "_init_ydl", fake_init)

    out = asyncio.run(dl.extract_info_async("https://x/1", "main"))

    assert out == {"title": "Gesperrt"}
    assert dl._cookie_mode is True


def test_extract_info_async_reraises_non_auth_errors(monkeypatch):
    monkeypatch.setattr(dlmod, "YDL_BROWSER", "firefox")
    monkeypatch.setattr(dlmod, "YDL_COOKIES_FILE", "")
    dl = bare_downloader()
    dl.url_ydl = FakeYdl(exc=Exception("HTTP Error 404: Not Found"))
    monkeypatch.setattr(Downloader, "_init_ydl", lambda self: pytest.fail("kein Umschalten"))

    with pytest.raises(Exception, match="404"):
        asyncio.run(dl.extract_info_async("https://x/1", "url"))
    assert dl._cookie_mode is False


def test_extract_info_async_reraises_auth_error_without_cookie_source(monkeypatch):
    """Keine Cookie-Quelle konfiguriert → der Fehler muss beim Nutzer landen."""
    monkeypatch.setattr(dlmod, "YDL_BROWSER", "")
    monkeypatch.setattr(dlmod, "YDL_COOKIES_FILE", "")
    dl = bare_downloader()
    dl.ydl = FakeYdl(exc=Exception("Sign in to confirm you're not a bot"))

    with pytest.raises(Exception, match="Sign in"):
        asyncio.run(dl.extract_info_async("https://x/1", "main"))


def test_extract_info_async_timeout_is_not_retried(monkeypatch):
    monkeypatch.setattr(dlmod, "YDL_BROWSER", "firefox")
    monkeypatch.setattr(dlmod, "YDL_COOKIES_FILE", "")
    dl = bare_downloader()

    class SlowYdl:
        calls = 0

        def extract_info(self, query, download=False):
            SlowYdl.calls += 1
            import time
            time.sleep(0.3)
            return {}

    dl.ydl = SlowYdl()
    monkeypatch.setattr(Downloader, "_init_ydl", lambda self: pytest.fail("kein Umschalten"))

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(dl.extract_info_async("https://x/1", "main", timeout=0.05))
    assert dl._cookie_mode is False
