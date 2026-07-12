"""Tests für den progressiven Download-Pfad (wachsende Datei statt CDN-Stream).

Regeln: Ungecachte Tracks ≤ 20 min mit ext webm/opus starten als progressiver
Download (yt_dlp lädt, FFmpeg spielt die wachsende Datei ab Startpuffer).
Gates (mp3-Modus, fremde Container, TTL-Blockliste, force_download) und
Fehlschläge fallen auf den alten CDN-Stream-Pfad zurück.
"""

import asyncio
import time
from pathlib import Path

import pytest

import cogs.downloader as dlmod
import utils.files as files_mod
from cogs.downloader import (
    PROGRESSIVE_MIN_BYTES, PROGRESSIVE_SIDECAR_SUFFIX, Downloader,
)

URL = "https://www.youtube.com/watch?v=aaaaaaaaaaa"


@pytest.fixture(autouse=True)
def clean_pending_deletes():
    """Simulierte Windows-Blockaden füllen die Pending-Delete-Liste –
    zwischen den Tests leeren, damit nichts in andere Tests blutet."""
    files_mod._pending_deletes.clear()
    yield
    files_mod._pending_deletes.clear()


class FakeYdl:
    """Haupt-Instanz-Ersatz: liefert Metadaten + Zielpfad."""

    def __init__(self, info, filename):
        self._info = info
        self._filename = filename

    def extract_info(self, url, download=False):
        return self._info

    def prepare_filename(self, info):
        return str(self._filename)


class WriterYdl:
    """Progressiv-Instanz-Ersatz: download() schreibt die Zieldatei.

    write_bytes=None → wirft stattdessen (Download-Fehlschlag).
    linger: hält den Download-Thread nach dem Schreiben am Leben, damit die
    Datei aus Sicht des Wartenden noch "wächst".
    """

    def __init__(self, target, write_bytes, linger=0.0, partial_before_fail=0):
        self.target = Path(target)
        self.write_bytes = write_bytes
        self.linger = linger
        self.partial_before_fail = partial_before_fail
        self.calls = []

    def download(self, urls):
        self.calls.append(urls)
        if self.write_bytes is None:
            if self.partial_before_fail:
                self.target.write_bytes(b"\0" * self.partial_before_fail)
            raise RuntimeError("Download kaputt")
        self.target.write_bytes(b"\0" * self.write_bytes)
        if self.linger:
            time.sleep(self.linger)


def make_dl(info, filename, ydl_progressive=None):
    dl = Downloader.__new__(Downloader)
    dl.audio_format = "webm"
    dl._url_cache = {URL: info}
    dl._cache_timestamps = {}
    dl._pending_resolves = {}
    dl._progressive = {}
    dl._progressive_files = {}
    dl._incomplete_files = set()
    dl._progressive_blocked = {}
    dl.ydl = FakeYdl(info, filename)
    if ydl_progressive is not None:
        dl._make_progressive_ydl = lambda: ydl_progressive
    return dl


def resolve(dl, **kwargs):
    async def run():
        result = await dl.resolve_track(URL, "Fallback-Titel", **kwargs)
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return result

    return asyncio.run(run())


def short_info(**extra):
    return {"title": "Kurz", "duration": 180, "ext": "webm",
            "url": "https://cdn.example/stream", "webpage_url": URL, **extra}


# ---------------------------------------------------------------------------
# Primärpfad: progressiv statt Stream
# ---------------------------------------------------------------------------

def test_missing_webm_starts_progressive_and_returns_growing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(dlmod, "DOWNLOADS_MAX_MB", 0)
    target = tmp_path / "song.webm"
    writer = WriterYdl(target, write_bytes=PROGRESSIVE_MIN_BYTES + 1024, linger=0.3)
    dl = make_dl(short_info(), target, writer)

    _, filename, _, _ = resolve(dl)

    assert isinstance(filename, Path)
    assert filename == target
    assert writer.calls == [[URL]]          # webpage_url, nicht die CDN-URL
    assert dl.last_resolved_file == target
    # Nach Abschluss (resolve-Helper wartet alle Tasks ab): vollständig markiert.
    assert not dl.is_incomplete(target)
    assert not target.with_name(target.name + PROGRESSIVE_SIDECAR_SUFFIX).exists()


def test_short_download_finishes_before_threshold(tmp_path, monkeypatch):
    """Datei kleiner als der Startpuffer → fertiger Task zählt als 'genug'."""
    monkeypatch.setattr(dlmod, "DOWNLOADS_MAX_MB", 0)
    target = tmp_path / "mini.webm"
    dl = make_dl(short_info(), target, WriterYdl(target, write_bytes=10 * 1024))

    _, filename, _, _ = resolve(dl)

    assert filename == target
    assert not dl.is_incomplete(target)


def test_failed_download_cleans_up_blocks_and_streams(tmp_path, monkeypatch):
    monkeypatch.setattr(dlmod, "DOWNLOADS_MAX_MB", 0)
    target = tmp_path / "kaputt.webm"
    dl = make_dl(short_info(), target,
                 WriterYdl(target, write_bytes=None, partial_before_fail=10 * 1024))

    _, filename, _, _ = resolve(dl)

    assert filename == "https://cdn.example/stream"   # Fallback auf den alten Pfad
    assert URL in dl._progressive_blocked             # nicht sofort wieder versuchen
    assert not target.exists()                        # Teil-Datei weggeräumt
    assert not target.with_name(target.name + PROGRESSIVE_SIDECAR_SUFFIX).exists()
    assert dl._progressive == {}                      # Task ausgetragen


def test_buffer_timeout_falls_back_to_stream_without_cancel(tmp_path, monkeypatch):
    """Download schreibt zu langsam → Stream-Fallback; der Task läuft weiter
    (Datei darf im Hintergrund fertig werden und wird Cache)."""
    monkeypatch.setattr(dlmod, "DOWNLOADS_MAX_MB", 0)
    monkeypatch.setattr(dlmod, "PROGRESSIVE_WAIT_TIMEOUT", 0.2)
    target = tmp_path / "zaeh.webm"
    dl = make_dl(short_info(), target,
                 WriterYdl(target, write_bytes=PROGRESSIVE_MIN_BYTES + 1024, linger=0.0))

    # linger=0, aber der Schreib-Thread startet erst nach dem Timeout:
    class LateWriter(WriterYdl):
        def download(self, urls):
            time.sleep(0.5)
            super().download(urls)

    dl._make_progressive_ydl = lambda: LateWriter(target, write_bytes=PROGRESSIVE_MIN_BYTES + 1024)

    _, filename, _, _ = resolve(dl)

    assert filename == "https://cdn.example/stream"
    assert URL in dl._progressive_blocked
    # resolve-Helper hat den weiterlaufenden Task abgewartet → Datei kam doch an.
    assert target.exists()
    assert not dl.is_incomplete(target)


class InfoYdl:
    """Progressiv-Instanz-Ersatz mit Schnellstart-API (process_ie_result)."""

    def __init__(self, target, fail_fast=False):
        self.target = Path(target)
        self.fail_fast = fail_fast
        self.process_calls = 0
        self.download_calls = []

    def process_ie_result(self, info, download=True):
        self.process_calls += 1
        if self.fail_fast:
            self.target.write_bytes(b"\0" * 512)   # Teil-Datei vor dem Fehler
            raise RuntimeError("URL abgelaufen")
        self.target.write_bytes(b"\0" * 10 * 1024)

    def download(self, urls):
        self.download_calls.append(urls)
        self.target.write_bytes(b"\0" * 10 * 1024)


def test_full_info_uses_fast_start_without_second_extraction(tmp_path, monkeypatch):
    """Volles Info-Dict (mit formats) → Download startet direkt über
    process_ie_result, ohne zweite Extraktion (JS-Challenge = 5–10 s Latenz)."""
    monkeypatch.setattr(dlmod, "DOWNLOADS_MAX_MB", 0)
    target = tmp_path / "song.webm"
    ydl = InfoYdl(target)
    dl = make_dl(short_info(formats=[{"format_id": "251"}]), target, ydl)

    _, filename, _, _ = resolve(dl)

    assert filename == target
    assert ydl.process_calls == 1
    assert ydl.download_calls == []


def test_fast_start_failure_falls_back_to_fresh_extract(tmp_path, monkeypatch):
    """Abgelaufene CDN-URL im Schnellstart → Teil-Datei weg, einmal frisch
    extrahieren (download über webpage_url) – kein User-sichtbarer Fehler."""
    monkeypatch.setattr(dlmod, "DOWNLOADS_MAX_MB", 0)
    target = tmp_path / "song.webm"
    ydl = InfoYdl(target, fail_fast=True)
    dl = make_dl(short_info(formats=[{"format_id": "251"}]), target, ydl)

    _, filename, _, _ = resolve(dl)

    assert filename == target
    assert ydl.process_calls == 1
    assert ydl.download_calls == [[URL]]
    assert target.stat().st_size == 10 * 1024   # frischer Download, nicht die Teil-Datei
    assert not dl.is_incomplete(target)


def test_fast_start_failure_with_blocked_unlink_aborts_and_blocks(tmp_path, monkeypatch):
    """Windows: FFmpeg hält die Teil-Datei des gescheiterten Schnellstarts noch
    offen → unlink schlägt fehl. Dann darf KEIN zweiter Download an dieselbe
    Datei anhängen (yt_dlp continuedl → korrupter Mischling); stattdessen wird
    die URL geblockt und der Stream-Fallback übernimmt."""
    monkeypatch.setattr(dlmod, "DOWNLOADS_MAX_MB", 0)
    target = tmp_path / "blockiert.webm"
    ydl = InfoYdl(target, fail_fast=True)
    dl = make_dl(short_info(formats=[{"format_id": "251"}]), target, ydl)
    monkeypatch.setattr(
        Path, "unlink",
        lambda self, missing_ok=False: (_ for _ in ()).throw(PermissionError("in use")),
    )

    _, filename, _, _ = resolve(dl)

    assert filename == "https://cdn.example/stream"   # Fallback statt Mischling
    assert ydl.download_calls == []                   # nie an die blockierte Datei angehängt
    assert URL in dl._progressive_blocked
    assert target.exists()                            # bleibt liegen – Startup-Purge räumt auf
    assert dl.is_incomplete(target)


def test_reduced_cache_entry_skips_fast_start(tmp_path, monkeypatch):
    """Reduzierter Eintrag aus metadata_cache.json (keine formats) → direkt
    frische Extraktion, process_ie_result wird nie versucht."""
    monkeypatch.setattr(dlmod, "DOWNLOADS_MAX_MB", 0)
    target = tmp_path / "song.webm"
    ydl = InfoYdl(target)
    dl = make_dl(short_info(), target, ydl)

    _, filename, _, _ = resolve(dl)

    assert filename == target
    assert ydl.process_calls == 0
    assert ydl.download_calls == [[URL]]


# ---------------------------------------------------------------------------
# Gates: mp3-Modus, fremder Container, Blockliste, force_download
# ---------------------------------------------------------------------------

def _assert_streams_without_progressive(dl, target):
    _, filename, _, _ = resolve(dl)
    assert filename == "https://cdn.example/stream"
    assert not target.exists()


def test_mp3_mode_keeps_stream_path(tmp_path):
    target = tmp_path / "song.webm"
    dl = make_dl(short_info(), target)
    dl.audio_format = "mp3"
    dl._make_progressive_ydl = None  # würde krachen, falls doch progressiv
    _assert_streams_without_progressive(dl, target)


def test_non_webm_container_keeps_stream_path(tmp_path):
    target = tmp_path / "song.m4a"
    dl = make_dl(short_info(ext="m4a"), target)
    dl._make_progressive_ydl = None
    _assert_streams_without_progressive(dl, target)


def test_blocked_url_keeps_stream_path(tmp_path):
    target = tmp_path / "song.webm"
    dl = make_dl(short_info(), target)
    dl._progressive_blocked[URL] = time.monotonic()
    dl._make_progressive_ydl = None
    _assert_streams_without_progressive(dl, target)


def test_block_expires_after_ttl(tmp_path, monkeypatch):
    monkeypatch.setattr(dlmod, "DOWNLOADS_MAX_MB", 0)
    target = tmp_path / "song.webm"
    dl = make_dl(short_info(), target,
                 WriterYdl(target, write_bytes=10 * 1024))
    dl._progressive_blocked[URL] = time.monotonic() - dlmod.PROGRESSIVE_BLOCK_TTL - 1

    _, filename, _, _ = resolve(dl)

    assert filename == target                 # abgelaufen → wieder progressiv
    assert URL not in dl._progressive_blocked


# ---------------------------------------------------------------------------
# Laufender Task wird mitgenutzt (Loop/!replay/!eq-Restart) & Leichen
# ---------------------------------------------------------------------------

def test_resolve_joins_running_progressive_task(tmp_path):
    target = tmp_path / "song.webm"
    target.write_bytes(b"\0" * (PROGRESSIVE_MIN_BYTES + 1024))
    dl = make_dl(short_info(), target)
    dl._incomplete_files.add(target.resolve())
    dl._make_progressive_ydl = None  # kein zweiter Download-Start erlaubt

    async def run():
        async def fake_download():
            await asyncio.sleep(0.05)
            return True

        dl._progressive[URL] = asyncio.create_task(fake_download())
        result = await dl.resolve_track(URL, "Fallback-Titel")
        await dl._progressive[URL]
        return result

    _, filename, _, _ = asyncio.run(run())

    assert filename == target


def test_incomplete_leftover_is_deleted_and_redownloaded(tmp_path, monkeypatch):
    """Leiche eines gescheiterten Laufs (Datei existiert, gilt als unvollständig)
    → wird gelöscht und frisch progressiv geladen, nie als Cache missdeutet."""
    monkeypatch.setattr(dlmod, "DOWNLOADS_MAX_MB", 0)
    target = tmp_path / "song.webm"
    target.write_bytes(b"\0" * 512)
    dl = make_dl(short_info(), target,
                 WriterYdl(target, write_bytes=10 * 1024))
    dl._incomplete_files.add(target.resolve())

    _, filename, _, _ = resolve(dl)

    assert filename == target
    assert target.stat().st_size == 10 * 1024   # neu geschrieben, nicht die Leiche
    assert not dl.is_incomplete(target)


def test_unlink_failure_keeps_incomplete_marker(tmp_path, monkeypatch):
    """Kann die Teil-Datei nach einem Fehlschlag nicht gelöscht werden (FFmpeg
    liest noch), bleibt sie als unvollständig markiert + Sidecar liegen."""
    monkeypatch.setattr(dlmod, "DOWNLOADS_MAX_MB", 0)
    target = tmp_path / "blockiert.webm"
    dl = make_dl(short_info(), target)
    dl._make_progressive_ydl = lambda: WriterYdl(target, write_bytes=None, partial_before_fail=512)
    dl._incomplete_files.add(target.resolve())
    dl._progressive_files[URL] = target
    dl._sidecar_for(target).touch()
    monkeypatch.setattr(Path, "unlink", lambda self, missing_ok=False: (_ for _ in ()).throw(OSError("in use")))

    ok = asyncio.run(dl._run_progressive(URL, short_info(), target, "Kurz"))

    assert ok is False
    assert dl.is_incomplete(target)
    assert dl._sidecar_for(target).exists()


def test_purge_stale_progressive_removes_leftovers(tmp_path, monkeypatch):
    monkeypatch.setattr(dlmod, "DOWNLOAD_DIR", tmp_path)
    media = tmp_path / "alt.webm"
    media.write_bytes(b"\0" * 512)
    sidecar = tmp_path / ("alt.webm" + PROGRESSIVE_SIDECAR_SUFFIX)
    sidecar.touch()
    fertig = tmp_path / "fertig.webm"     # ohne Sidecar → bleibt
    fertig.write_bytes(b"\0" * 512)
    dl = Downloader.__new__(Downloader)

    dl._purge_stale_progressive()

    assert not media.exists()
    assert not sidecar.exists()
    assert fertig.exists()


# ---------------------------------------------------------------------------
# Prefetch-Koordination
# ---------------------------------------------------------------------------

def test_prefetch_skips_url_with_running_progressive_download(tmp_path):
    dl = make_dl(short_info(), tmp_path / "song.webm")
    dl.ydl = None  # jeder Zugriff würde krachen → Skip muss vorher greifen

    async def run():
        async def forever():
            await asyncio.sleep(10)

        task = asyncio.create_task(forever())
        dl._progressive[URL] = task
        await dl.prefetch_next([(URL, "Kurz")], 0)
        task.cancel()

    asyncio.run(run())  # kein Fehler = Skip hat gegriffen
