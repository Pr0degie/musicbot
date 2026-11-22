"""Der In-Memory-Metadaten-Cache beachtet CACHE_TTL auch zur Laufzeit.

Vorher wurde die TTL nur beim Laden/Speichern von metadata_cache.json geprüft:
lief der Bot länger als zwei Stunden, lieferte der Cache abgelaufene CDN-URLs
aus. Abgelaufene Einträge gelten jetzt als Cache-Miss – außer die lokale Datei
liegt schon vollständig vor, dann spielt sie ohne neue Extraktion.
"""
import asyncio
import time

from cogs.downloader import CACHE_TTL, STREAM_THRESHOLD_SECONDS, Downloader

URL = "https://www.youtube.com/watch?v=aaaaaaaaaaa"


class FakeYdl:
    def __init__(self, fresh_info, filename):
        self._fresh = fresh_info
        self._filename = filename
        self.extract_calls = []
        self.download_calls = []

    def extract_info(self, url, download=False):
        self.extract_calls.append(url)
        return self._fresh

    def prepare_filename(self, info):
        return str(self._filename)

    def download(self, urls):
        self.download_calls.append(urls)
        self._filename.write_bytes(b"audio")


def make_dl(cached_info, fresh_info, filename, age):
    """age: Alter des Cache-Eintrags in Sekunden; None = Eintrag ohne Timestamp."""
    dl = Downloader.__new__(Downloader)
    dl.audio_format = "webm"
    dl._url_cache = {URL: cached_info}
    dl._cache_timestamps = {} if age is None else {URL: time.time() - age}
    dl._pending_resolves = {}
    dl.ydl = FakeYdl(fresh_info, filename)
    return dl


def run(coro_factory):
    async def runner():
        result = await coro_factory()
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return result

    return asyncio.run(runner())


ALT = {"title": "Song", "duration": 180, "url": "https://cdn.example/abgelaufen"}
NEU = {"title": "Song", "duration": 180, "url": "https://cdn.example/frisch"}


def test_abgelaufener_eintrag_ohne_datei_wird_frisch_extrahiert(tmp_path):
    dl = make_dl(ALT, NEU, tmp_path / "song.webm", age=CACHE_TTL + 60)

    info, source, title, duration = run(lambda: dl.resolve_track(URL, "Song"))

    assert dl.ydl.extract_calls == [URL]
    assert source == "https://cdn.example/frisch", "abgelaufene CDN-URL darf nicht mehr ausgeliefert werden"
    assert dl._url_cache[URL] is NEU
    assert time.time() - dl._cache_timestamps[URL] < 5


def test_abgelaufener_eintrag_mit_lokaler_datei_spielt_ohne_extraktion(tmp_path):
    local = tmp_path / "song.webm"
    local.write_bytes(b"audio")
    dl = make_dl(ALT, NEU, local, age=CACHE_TTL + 60)

    info, source, title, duration = run(lambda: dl.resolve_track(URL, "Song"))

    assert dl.ydl.extract_calls == []
    assert source == local
    assert info is ALT


def test_abgelaufener_eintrag_mit_unvollstaendiger_datei_wird_frisch_extrahiert(tmp_path):
    """Eine noch wachsende/abgebrochene Datei zählt nicht als vorhanden."""
    local = tmp_path / "song.webm"
    local.write_bytes(b"halb")
    dl = make_dl(ALT, NEU, local, age=CACHE_TTL + 60)
    dl._incomplete_files = {local.resolve()}

    run(lambda: dl.resolve_track(URL, "Song"))

    assert dl.ydl.extract_calls == [URL]


def test_abgelaufener_langer_track_bekommt_frische_stream_url(tmp_path):
    lang_alt = {**ALT, "duration": STREAM_THRESHOLD_SECONDS + 1}
    lang_neu = {**NEU, "duration": STREAM_THRESHOLD_SECONDS + 1}
    dl = make_dl(lang_alt, lang_neu, tmp_path / "nie.webm", age=CACHE_TTL + 60)

    info, source, title, duration = run(lambda: dl.resolve_track(URL, "Song"))

    assert dl.ydl.extract_calls == [URL]
    assert source == "https://cdn.example/frisch"


def test_frischer_eintrag_bleibt_cache_hit(tmp_path):
    dl = make_dl(ALT, NEU, tmp_path / "song.webm", age=CACHE_TTL - 60)

    info, source, title, duration = run(lambda: dl.resolve_track(URL, "Song"))

    assert dl.ydl.extract_calls == []
    assert source == "https://cdn.example/abgelaufen"


def test_eintrag_ohne_timestamp_gilt_als_frisch(tmp_path):
    """Wie beim Speichern (_serialize_cache): fehlender Timestamp = jetzt."""
    dl = make_dl(ALT, NEU, tmp_path / "song.webm", age=None)

    run(lambda: dl.resolve_track(URL, "Song"))

    assert dl.ydl.extract_calls == []


def test_prefetch_extrahiert_abgelaufenen_eintrag_frisch(tmp_path):
    local = tmp_path / "song.webm"
    dl = make_dl(ALT, {**NEU, "webpage_url": URL}, local, age=CACHE_TTL + 60)

    run(lambda: dl._prefetch_one(URL, "Song"))

    assert dl.ydl.extract_calls == [URL]
    assert dl.ydl.download_calls == [[URL]]
    assert dl._url_cache[URL]["url"] == "https://cdn.example/frisch"


def test_prefetch_laesst_vorhandene_datei_trotz_abgelaufenem_eintrag_in_ruhe(tmp_path):
    local = tmp_path / "song.webm"
    local.write_bytes(b"audio")
    dl = make_dl(ALT, NEU, local, age=CACHE_TTL + 60)

    run(lambda: dl._prefetch_one(URL, "Song"))

    assert dl.ydl.extract_calls == []
    assert dl.ydl.download_calls == []
