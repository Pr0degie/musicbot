"""Characterization-Tests: Downloader-Metadaten-Cache, rebuild und yt_video_id.

METADATA_CACHE_FILE ist ein relativer Pfad – via monkeypatch.chdir(tmp_path)
landet die Datei im Temp-Verzeichnis.
"""

import json
import time
from pathlib import Path

import pytest

from cogs.downloader import CACHE_TTL, METADATA_CACHE_FILE, Downloader, yt_video_id


def bare_downloader():
    """Downloader ohne __init__ – keine yt_dlp-Instanzen, kein Datei-Load."""
    dl = Downloader.__new__(Downloader)
    dl.audio_format = "webm"
    dl._url_cache = {}
    dl._cache_timestamps = {}
    dl._pending_resolves = {}
    return dl


# ---------------------------------------------------------------------------
# yt_video_id
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?t=10", "dQw4w9WgXcQ"),
        ("https://example.com/?other=1&v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),  # jedes "v=" matcht
        ("https://soundcloud.com/foo/bar", None),
        ("https://www.youtube.com/watch?v=zukurz", None),  # < 11 Zeichen
        ("", None),
        (None, None),
    ],
)
def test_yt_video_id(url, expected):
    assert yt_video_id(url) == expected


def test_yt_video_id_takes_first_11_chars_of_longer_ids():
    """IST: Bei überlangen IDs werden stumpf die ersten 11 Zeichen genommen."""
    assert yt_video_id("https://youtu.be/aaaaaaaaaaabbbb") == "aaaaaaaaaaa"


# ---------------------------------------------------------------------------
# _save_cache / _load_cache
# ---------------------------------------------------------------------------

def test_cache_roundtrip_keeps_fresh_entries(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    dl = bare_downloader()
    dl._url_cache["https://x/1"] = {"title": "Eins", "duration": 100}
    dl._cache_timestamps["https://x/1"] = time.time()

    dl._save_cache()

    dl2 = bare_downloader()
    dl2._load_cache()
    assert dl2._url_cache["https://x/1"] == {"title": "Eins", "duration": 100}
    # _ts wird beim Laden aus dem Entry entfernt (pop)
    assert "_ts" not in dl2._url_cache["https://x/1"]


def test_save_cache_writes_ts_field(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    dl = bare_downloader()
    ts = time.time() - 10
    dl._url_cache["https://x/1"] = {"title": "Eins"}
    dl._cache_timestamps["https://x/1"] = ts

    dl._save_cache()

    data = json.loads(Path("metadata_cache.json").read_text(encoding="utf-8"))
    assert data["https://x/1"]["_ts"] == ts


def test_save_cache_drops_expired_entries(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    dl = bare_downloader()
    dl._url_cache["https://x/alt"] = {"title": "Alt"}
    dl._cache_timestamps["https://x/alt"] = time.time() - CACHE_TTL - 1
    dl._url_cache["https://x/neu"] = {"title": "Neu"}
    dl._cache_timestamps["https://x/neu"] = time.time()

    dl._save_cache()

    data = json.loads(Path("metadata_cache.json").read_text(encoding="utf-8"))
    assert "https://x/alt" not in data
    assert "https://x/neu" in data


def test_save_cache_entry_without_timestamp_gets_now(monkeypatch, tmp_path):
    """IST: Fehlt der Timestamp (z.B. nach invalidate), wird beim Speichern
    'jetzt' angenommen – der Eintrag bekommt effektiv eine frische TTL."""
    monkeypatch.chdir(tmp_path)
    dl = bare_downloader()
    dl._url_cache["https://x/1"] = {"title": "Ohne TS"}

    before = time.time()
    dl._save_cache()

    data = json.loads(Path("metadata_cache.json").read_text(encoding="utf-8"))
    assert data["https://x/1"]["_ts"] >= before


def test_load_cache_skips_expired_entries(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    Path("metadata_cache.json").write_text(
        json.dumps({
            "https://x/alt": {"title": "Alt", "_ts": time.time() - CACHE_TTL - 1},
            "https://x/neu": {"title": "Neu", "_ts": time.time()},
        }),
        encoding="utf-8",
    )
    dl = bare_downloader()

    dl._load_cache()

    assert list(dl._url_cache) == ["https://x/neu"]


def test_load_cache_entry_without_ts_counts_as_expired(monkeypatch, tmp_path):
    """IST: Ohne _ts-Feld gilt ts=0.0 → Eintrag ist immer abgelaufen."""
    monkeypatch.chdir(tmp_path)
    Path("metadata_cache.json").write_text(
        json.dumps({"https://x/1": {"title": "Ohne TS"}}), encoding="utf-8"
    )
    dl = bare_downloader()

    dl._load_cache()

    assert dl._url_cache == {}


def test_load_cache_corrupt_file_does_not_crash(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    Path("metadata_cache.json").write_text("{kaputt", encoding="utf-8")
    dl = bare_downloader()

    dl._load_cache()  # darf keine Exception werfen

    assert dl._url_cache == {}


def test_load_cache_missing_file_is_noop(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    dl = bare_downloader()
    dl._load_cache()
    assert dl._url_cache == {}


# ---------------------------------------------------------------------------
# rebuild / invalidate
# ---------------------------------------------------------------------------

def test_rebuild_with_keep_urls_keeps_only_those(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Downloader, "_init_ydl", lambda self: None)
    dl = bare_downloader()
    now = time.time()
    for u in ("https://x/1", "https://x/2", "https://x/3"):
        dl._url_cache[u] = {"title": u}
        dl._cache_timestamps[u] = now

    dl.rebuild("mp3", keep_urls={"https://x/1", "https://x/3"})

    assert set(dl._url_cache) == {"https://x/1", "https://x/3"}
    assert set(dl._cache_timestamps) == {"https://x/1", "https://x/3"}
    assert dl.audio_format == "mp3"


def test_rebuild_without_keep_urls_clears_everything(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Downloader, "_init_ydl", lambda self: None)
    dl = bare_downloader()
    dl._url_cache["https://x/1"] = {"title": "Eins"}
    dl._cache_timestamps["https://x/1"] = time.time()
    dl._pending_resolves["https://x/2"] = object()

    dl.rebuild("webm", keep_urls=None)

    assert dl._url_cache == {}
    assert dl._cache_timestamps == {}
    assert dl._pending_resolves == {}


def test_rebuild_saves_cache_to_disk_first(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Downloader, "_init_ydl", lambda self: None)
    dl = bare_downloader()
    dl._url_cache["https://x/1"] = {"title": "Eins"}
    dl._cache_timestamps["https://x/1"] = time.time()

    dl.rebuild("webm", keep_urls=set())

    # Vor dem Filtern wurde der volle Stand auf Disk gesichert.
    data = json.loads(METADATA_CACHE_FILE.read_text(encoding="utf-8"))
    assert "https://x/1" in data
    assert dl._url_cache == {}


def test_invalidate_removes_entry_but_clears_all_timestamps():
    """IST: invalidate(url) entfernt den einen Cache-Eintrag, leert aber
    zusätzlich ALLE Timestamps – auch die fremder Einträge (Auffälligkeit)."""
    dl = bare_downloader()
    now = time.time()
    dl._url_cache["https://x/1"] = {"title": "Eins"}
    dl._url_cache["https://x/2"] = {"title": "Zwei"}
    dl._cache_timestamps["https://x/1"] = now
    dl._cache_timestamps["https://x/2"] = now

    dl.invalidate("https://x/1")

    assert "https://x/1" not in dl._url_cache
    assert "https://x/2" in dl._url_cache          # Eintrag bleibt ...
    assert dl._cache_timestamps == {}              # ... sein Timestamp nicht
