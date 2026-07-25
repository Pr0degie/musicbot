"""Tests für prime_first_hit: den Download des ersten !p-Treffers sofort
anstoßen, damit er parallel zu den Discord-Nachrichten läuft.

Regeln: Nur wenn der progressive Pfad überhaupt zulässig ist (dieselben Gates
wie resolve_track) und noch niemand für die URL schreibt – die In-Flight-
Registry bleibt die einzige Wahrheit über lebende Downloads (ADR 0002).
"""

import asyncio

import pytest

from cogs.downloader import STREAM_THRESHOLD_SECONDS, Downloader

URL = "https://www.youtube.com/watch?v=bbbbbbbbbbb"


def make_dl(tmp_path, audio_format="webm"):
    dl = Downloader.__new__(Downloader)
    dl.audio_format = audio_format
    dl._cookie_mode = False
    dl._url_cache = {}
    dl._cache_timestamps = {}
    dl._pending_resolves = {}
    dl._progressive = {}
    dl._progressive_files = {}
    dl._progressive_blocked = {}
    dl._incomplete_files = set()
    dl._inflight = {}
    dl.started = []
    dl._target = tmp_path / "Song.webm"

    class Ydl:
        def prepare_filename(_self, info):
            return str(dl._target)

    dl.ydl = Ydl()
    # _start_progressive nur protokollieren – der echte Task würde yt_dlp starten.
    dl._start_progressive = lambda url, info, filename, title: dl.started.append((url, filename))
    return dl


def info(ext="webm", duration=200):
    return {"title": "Song", "ext": ext, "duration": duration}


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

def test_starts_download_for_cached_info(tmp_path):
    dl = make_dl(tmp_path)
    dl._url_cache[URL] = info()

    dl.prime_first_hit(URL, "Song")

    assert dl.started == [(URL, dl._target)]
    assert dl._pending_resolves == {}, "gecachte Metadaten brauchen keinen Resolve-Task"


@pytest.mark.parametrize("kwargs,grund", [
    ({"ext": "m4a"}, "m4a ist wachsend nicht lesbar (moov am Dateiende)"),
    ({"duration": STREAM_THRESHOLD_SECONDS + 1}, "langer Track wird gestreamt"),
])
def test_gates_block_download(tmp_path, kwargs, grund):
    dl = make_dl(tmp_path)
    dl._url_cache[URL] = info(**kwargs)

    dl.prime_first_hit(URL, "Song")

    assert dl.started == [], grund


def test_mp3_mode_blocks_download(tmp_path):
    dl = make_dl(tmp_path, audio_format="mp3")
    dl._url_cache[URL] = info()

    dl.prime_first_hit(URL, "Song")

    assert dl.started == [], "mp3 konvertiert erst nach dem Download"


def test_blocked_url_is_not_restarted(tmp_path):
    dl = make_dl(tmp_path)
    dl._url_cache[URL] = info()
    dl.block_progressive(URL)

    dl.prime_first_hit(URL, "Song")

    assert dl.started == []


def test_existing_file_is_left_to_resolve_track(tmp_path):
    dl = make_dl(tmp_path)
    dl._url_cache[URL] = info()
    dl._target.write_bytes(b"x" * 100)

    dl.prime_first_hit(URL, "Song")

    assert dl.started == [], "vorhandene Datei (fertig oder Leiche) klärt resolve_track"


def test_running_download_is_not_duplicated(tmp_path):
    """Invariante: pro URL maximal ein schreibender Download."""
    async def run():
        dl = make_dl(tmp_path)
        dl._url_cache[URL] = info()

        async def _noop():
            await asyncio.sleep(0.05)

        task = asyncio.create_task(_noop())
        dl._inflight[URL] = (task, "Queue-Prefetch")

        dl.prime_first_hit(URL, "Song")
        assert dl.started == []
        await task

    asyncio.run(run())


def test_pending_resolve_is_not_duplicated(tmp_path):
    dl = make_dl(tmp_path)
    dl._pending_resolves[URL] = object()

    dl.prime_first_hit(URL, "Song")

    assert dl.started == []


# ---------------------------------------------------------------------------
# Ungecachte URL: erst auflösen, dann laden
# ---------------------------------------------------------------------------

def test_resolves_then_starts_download(tmp_path):
    async def run():
        dl = make_dl(tmp_path)

        async def fake_fetch(url):
            dl._url_cache[url] = info()
            return dl._url_cache[url]

        dl._fetch_info = fake_fetch

        dl.prime_first_hit(URL, "Song")
        assert URL in dl._pending_resolves, "resolve_track muss darauf warten können"
        await dl._pending_resolves[URL]

        assert dl.started == [(URL, dl._target)]
        assert URL not in dl._pending_resolves, "Task muss sich wieder abmelden"

    asyncio.run(run())


def test_failed_resolve_stays_silent(tmp_path):
    """Fehler meldet play_next dem Nutzer – prime_first_hit schluckt ihn."""
    async def run():
        dl = make_dl(tmp_path)

        async def failing_fetch(url):
            raise RuntimeError("yt_dlp kaputt")

        dl._fetch_info = failing_fetch

        dl.prime_first_hit(URL, "Song")
        task = dl._pending_resolves[URL]
        await task

        assert dl.started == []
        assert URL not in dl._pending_resolves
        assert task.exception() is None, "der Task darf nicht mit Exception enden"

    asyncio.run(run())
