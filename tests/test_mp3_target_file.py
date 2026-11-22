"""mp3-Modus: die Zieldatei heißt nach dem FFmpeg-Postprocessor <name>.mp3.

yt_dlps prepare_filename nennt weiter die Endung der Quelle (.webm). Ohne
Korrektur fand der Downloader seine mp3-Dateien nie: resolve_track streamte
immer, der Prefetch lud und konvertierte bei jedem Anstoß neu, und der
Cleanup kannte die Dateien der Queue-Songs nicht.
"""

import asyncio
from pathlib import Path

from cogs.downloader import Downloader

URL = "https://www.youtube.com/watch?v=aaaaaaaaaaa"


class FakeYdl:
    """Wie die Haupt-Instanz: prepare_filename liefert den Namen mit der
    Endung der Quelle; download() zählt die Aufrufe."""

    def __init__(self, source_name):
        self._source_name = source_name
        self.download_calls = []

    def prepare_filename(self, info):
        return str(self._source_name)

    def extract_info(self, url, download=False):
        raise AssertionError("extract_info darf bei Cache-Hit nicht laufen")

    def download(self, urls):
        self.download_calls.append(urls)


def make_dl(tmp_path, audio_format):
    info = {"id": "aaaaaaaaaaa", "title": "Song", "ext": "webm", "duration": 200,
            "url": "https://cdn.example/stream", "webpage_url": URL}
    dl = Downloader.__new__(Downloader)
    dl.audio_format = audio_format
    dl._url_cache = {URL: info}
    dl._cache_timestamps = {}
    dl._pending_resolves = {}
    dl.ydl = FakeYdl(tmp_path / "Song [aaaaaaaaaaa].webm")
    return dl


def test_zieldatei_folgt_dem_format(tmp_path):
    info = {"title": "Song"}
    assert make_dl(tmp_path, "webm")._target_path(info) == tmp_path / "Song [aaaaaaaaaaa].webm"
    assert make_dl(tmp_path, "mp3")._target_path(info) == tmp_path / "Song [aaaaaaaaaaa].mp3"


def test_resolve_track_spielt_die_vorhandene_mp3(tmp_path):
    mp3 = tmp_path / "Song [aaaaaaaaaaa].mp3"
    mp3.write_bytes(b"x")
    dl = make_dl(tmp_path, "mp3")

    _, filename, _, _ = asyncio.run(dl.resolve_track(URL, "Song"))

    assert filename == mp3, "die konvertierte Datei liegt vor – kein Stream"
    assert dl.last_resolved_file == mp3


def test_prefetch_laedt_eine_vorhandene_mp3_nicht_erneut(tmp_path):
    (tmp_path / "Song [aaaaaaaaaaa].mp3").write_bytes(b"x")
    dl = make_dl(tmp_path, "mp3")

    asyncio.run(dl._prefetch_one(URL, "Song"))

    assert dl.ydl.download_calls == [], "Datei liegt schon konvertiert vor"


def test_cleanup_schuetzt_die_mp3_eines_queue_songs(tmp_path):
    dl = make_dl(tmp_path, "mp3")

    protected = dl._protected_paths([URL])

    assert (tmp_path / "Song [aaaaaaaaaaa].mp3").resolve() in protected


def test_webm_modus_bleibt_bei_der_quell_endung(tmp_path):
    webm = tmp_path / "Song [aaaaaaaaaaa].webm"
    webm.write_bytes(b"x")
    dl = make_dl(tmp_path, "webm")

    _, filename, _, _ = asyncio.run(dl.resolve_track(URL, "Song"))

    assert filename == webm
    assert isinstance(filename, Path)
