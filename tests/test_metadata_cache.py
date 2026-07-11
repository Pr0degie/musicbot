"""Tests für den reduzierten metadata_cache.json (PERSISTED_CACHE_FIELDS).

Garantien der Dauerbetrieb-Optimierung:
- Persistiert werden nur die tatsächlich gelesenen Felder; in-memory bleibt
  das volle Info-Dict.
- Roundtrip: ein reduzierter Eintrag ergibt in prepare_filename() exakt
  denselben Pfad wie das volle Dict.
- Alte Cache-Dateien im vollen Format bleiben ladbar; unlesbare Dateien
  werden sauber verworfen (Kaltstart, kein Crash).
- Ein aus der Datei geladener Eintrag, der beim Verwenden scheitert, wird
  verworfen und wie ein Cache-Miss behandelt (frische Extraktion).
"""

import asyncio
import json
import time
from pathlib import Path

from cogs.downloader import Downloader, PERSISTED_CACHE_FIELDS

URL = "https://www.youtube.com/watch?v=aaaaaaaaaaa"


def make_bare_dl():
    dl = Downloader.__new__(Downloader)
    dl.audio_format = "webm"
    dl._url_cache = {}
    dl._cache_timestamps = {}
    dl._pending_resolves = {}
    return dl


def full_info():
    """Nachbau eines vollen yt_dlp-Info-Dicts: Whitelist-Felder plus typischer
    Ballast (formats, captions, ...), der nicht mehr persistiert werden soll."""
    return {
        "id": "aaaaaaaaaaa",
        "title": "Take on Me (Official Video)",
        "ext": "webm",
        "duration": 225,
        "url": "https://cdn.example/stream",
        "webpage_url": URL,
        "thumbnail": "https://i.ytimg.com/vi/aaaaaaaaaaa/hq720.jpg",
        "uploader": "a-ha",
        "http_headers": {"User-Agent": "Mozilla/5.0", "Accept": "*/*"},
        # Ballast – macht im echten Dict >90% der Größe aus:
        "formats": [{"format_id": str(i), "url": "https://cdn.example/f" + "x" * 500} for i in range(30)],
        "automatic_captions": {"de": [{"url": "https://cdn.example/c" + "y" * 500}]},
        "tags": ["synthpop"] * 50,
        "description": "Lorem ipsum " * 200,
    }


# ---------------------------------------------------------------------------
# Roundtrip: reduziertes Dict → prepare_filename ergibt denselben Pfad
# ---------------------------------------------------------------------------

def test_reduced_roundtrip_same_prepare_filename(tmp_path, monkeypatch):
    monkeypatch.setattr("cogs.downloader.YDL_COOKIES_FILE", "")
    monkeypatch.setattr("cogs.downloader.YDL_BROWSER", "")
    monkeypatch.chdir(tmp_path)

    dl = Downloader("webm")  # echte yt_dlp-Instanzen (offline, kein Netz nötig)
    full = full_info()
    dl._url_cache = {URL: full}
    dl._cache_timestamps = {URL: time.time()}
    dl._save_cache()

    dl2 = make_bare_dl()
    dl2._load_cache()
    reduced = dl2._url_cache[URL]

    assert set(reduced) <= set(PERSISTED_CACHE_FIELDS)
    assert dl.ydl.prepare_filename(reduced) == dl.ydl.prepare_filename(full)


def test_reduced_file_is_much_smaller_than_full(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    full = full_info()
    full_size = len(json.dumps({URL: full}, ensure_ascii=False).encode("utf-8"))

    dl = make_bare_dl()
    dl._url_cache = {URL: full}
    dl._cache_timestamps = {URL: time.time()}
    payload = dl._serialize_cache()

    assert len(payload.encode("utf-8")) < full_size / 10


# ---------------------------------------------------------------------------
# Migration: alte Datei im vollen Format bleibt ladbar / kaputte Datei → Kaltstart
# ---------------------------------------------------------------------------

def test_old_full_format_file_still_loads(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = {URL: {**full_info(), "_ts": time.time()}}
    Path("metadata_cache.json").write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")

    dl = make_bare_dl()
    dl._load_cache()

    assert dl._url_cache[URL]["title"] == "Take on Me (Official Video)"
    assert dl._url_cache[URL]["ext"] == "webm"


def test_corrupt_cache_file_is_discarded_without_crash(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("metadata_cache.json").write_text("{kaputt", encoding="utf-8")

    dl = make_bare_dl()
    dl._load_cache()  # darf nicht raisen

    assert dl._url_cache == {}


def test_non_dict_entries_are_skipped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data = {URL: {"title": "Ok", "ext": "webm", "_ts": time.time()}, "https://x": "kein-dict"}
    Path("metadata_cache.json").write_text(json.dumps(data), encoding="utf-8")

    dl = make_bare_dl()
    dl._load_cache()

    assert URL in dl._url_cache
    assert "https://x" not in dl._url_cache


# ---------------------------------------------------------------------------
# Fallback: kaputter Cache-Eintrag beim Verwenden → verwerfen, frisch extrahieren
# ---------------------------------------------------------------------------

class RecoveringYdl:
    """prepare_filename schlägt für den kaputten Cache-Eintrag fehl; nach der
    frischen Extraktion (gutes Info-Dict) funktioniert alles wie beim Kaltstart."""

    def __init__(self, bad_info, good_info, filename):
        self.bad_info = bad_info
        self.good_info = good_info
        self._filename = filename
        self.extract_calls = []

    def extract_info(self, url, download=False):
        self.extract_calls.append(url)
        return self.good_info

    def prepare_filename(self, info):
        if info is self.bad_info:
            raise KeyError("ext")
        return str(self._filename)


def test_broken_cached_entry_falls_back_to_cache_miss(tmp_path):
    local = tmp_path / "song.webm"
    local.write_bytes(b"x")
    bad = {"title": "Kaputt", "duration": 180}
    good = {"title": "Frisch", "duration": 180, "url": "https://cdn.example/stream"}

    dl = make_bare_dl()
    dl._url_cache = {URL: bad}
    dl._cache_timestamps = {URL: time.time()}
    dl.ydl = RecoveringYdl(bad, good, local)

    async def run():
        return await dl.resolve_track(URL, "Fallback-Titel")

    _, filename, title, _ = asyncio.run(run())

    assert dl.ydl.extract_calls == [URL], "genau eine frische Extraktion (Cache-Miss-Pfad)"
    assert dl._url_cache[URL] is good, "kaputter Eintrag wurde ersetzt"
    assert filename == local
    assert title == "Frisch"
