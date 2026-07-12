"""Tests für utils.files: safe_unlink + Pending-Delete-Liste.

Windows wirft PermissionError beim Löschen einer offenen Datei (FFmpeg);
safe_unlink schiebt die Löschung dann auf, drain_pending_deletes holt sie
nach. Die Blockade wird plattformunabhängig per Monkeypatch simuliert, damit
die Suite auch auf Linux läuft (dort ist unlink-while-open legal)."""

from pathlib import Path

import pytest

import utils.files as files_mod
from utils.files import drain_pending_deletes, safe_unlink


@pytest.fixture(autouse=True)
def clean_pending():
    files_mod._pending_deletes.clear()
    yield
    files_mod._pending_deletes.clear()


def _block_unlink(monkeypatch, blocked: set):
    """Simuliert Windows-Sharing: unlink auf Pfade in `blocked` wirft PermissionError."""
    real_unlink = Path.unlink

    def fake_unlink(self, missing_ok=False):
        if self.resolve() in blocked:
            raise PermissionError(13, "Der Prozess kann nicht auf die Datei zugreifen")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fake_unlink)


def test_success_deletes_and_returns_true(tmp_path):
    f = tmp_path / "a.webm"
    f.write_bytes(b"\0")
    assert safe_unlink(f) is True
    assert not f.exists()
    assert not files_mod._pending_deletes


def test_missing_file_counts_as_gone(tmp_path):
    assert safe_unlink(tmp_path / "nie-da.webm") is True
    assert not files_mod._pending_deletes


def test_blocked_file_goes_to_pending(tmp_path, monkeypatch):
    f = tmp_path / "offen.webm"
    f.write_bytes(b"\0")
    _block_unlink(monkeypatch, {f.resolve()})

    assert safe_unlink(f) is False
    assert f.resolve() in files_mod._pending_deletes
    assert f.exists()


def test_blocked_file_with_defer_false_is_not_queued(tmp_path, monkeypatch):
    f = tmp_path / "offen.webm"
    f.write_bytes(b"\0")
    _block_unlink(monkeypatch, {f.resolve()})

    assert safe_unlink(f, defer=False) is False
    assert not files_mod._pending_deletes


def test_drain_retries_and_clears(tmp_path, monkeypatch):
    f = tmp_path / "offen.webm"
    f.write_bytes(b"\0")
    blocked = {f.resolve()}
    _block_unlink(monkeypatch, blocked)
    assert safe_unlink(f) is False

    blocked.clear()  # "FFmpeg hat die Datei geschlossen"
    assert drain_pending_deletes() == 1
    assert not f.exists()
    assert not files_mod._pending_deletes


def test_drain_keeps_still_blocked_files(tmp_path, monkeypatch):
    f = tmp_path / "offen.webm"
    f.write_bytes(b"\0")
    _block_unlink(monkeypatch, {f.resolve()})
    safe_unlink(f)

    assert drain_pending_deletes() == 0
    assert f.resolve() in files_mod._pending_deletes
    assert f.exists()


def test_drain_skips_protected_paths(tmp_path, monkeypatch):
    f = tmp_path / "wieder-aktiv.webm"
    f.write_bytes(b"\0")
    _block_unlink(monkeypatch, {f.resolve()})
    safe_unlink(f)
    monkeypatch.undo()  # Datei wäre jetzt löschbar – aber geschützt

    assert drain_pending_deletes(protect={f.resolve()}) == 0
    assert f.exists()
    assert f.resolve() in files_mod._pending_deletes  # bleibt für später


def test_successful_unlink_removes_stale_pending_entry(tmp_path, monkeypatch):
    """Wird ein Pfad nach fehlgeschlagenem Löschen doch noch via safe_unlink
    entfernt (z. B. Leichen-Cleanup vor Neu-Download), verschwindet er aus der
    Pending-Liste – der Drain darf die neu erzeugte Datei nicht später killen."""
    f = tmp_path / "leiche.webm"
    f.write_bytes(b"\0")
    _block_unlink(monkeypatch, {f.resolve()})
    safe_unlink(f)
    monkeypatch.undo()

    assert safe_unlink(f) is True
    assert not files_mod._pending_deletes
