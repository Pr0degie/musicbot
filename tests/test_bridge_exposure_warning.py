"""DM-Bridge ohne Secret auf einem Nicht-Loopback-Host: deutliche Warnung im
Log beim Start – sonst ändert sich nichts (kein Verweigern, gleiche Antworten)."""
import asyncio
import logging
import types

import pytest

import cogs.dm_bridge as bridge_mod
from cogs.dm_bridge import DMBridge, _host_is_loopback


class _FakeSite:
    """Ersetzt web.TCPSite: bindet nichts (kein 0.0.0.0-Socket im Test)."""
    started = []

    def __init__(self, runner, host, port):
        self.host, self.port = host, port

    async def start(self):
        _FakeSite.started.append((self.host, self.port))


def _warnungen(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING and "[DMBridge]" in r.getMessage()]


def _starte(monkeypatch, tmp_path, host, secret):
    _FakeSite.started = []
    monkeypatch.setattr(bridge_mod, "DM_BRIDGE_HOST", host)
    monkeypatch.setattr(bridge_mod, "DM_BRIDGE_SECRET", secret)
    monkeypatch.setattr(bridge_mod.web, "TCPSite", _FakeSite)
    monkeypatch.setattr(bridge_mod.tempfile, "gettempdir", lambda: str(tmp_path))

    async def run():
        cog = DMBridge(types.SimpleNamespace(user="TestBot"))
        await cog.cog_load()
        await cog.cog_unload()

    asyncio.run(run())


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.5", "LOCALHOST"])
def test_loopback_hosts(host):
    assert _host_is_loopback(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "100.64.0.7", "192.168.1.20", "::", "", "musikbot.lan"])
def test_nicht_loopback_hosts(host):
    assert _host_is_loopback(host) is False


def test_warnung_bei_offenem_host_ohne_secret(monkeypatch, tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        _starte(monkeypatch, tmp_path, "0.0.0.0", "")
    assert _FakeSite.started == [("0.0.0.0", bridge_mod.DM_BRIDGE_PORT)], "Server startet trotzdem"
    warn = _warnungen(caplog)
    assert len(warn) == 1
    assert "DM_BRIDGE_SECRET" in warn[0] and "0.0.0.0" in warn[0]


def test_keine_warnung_mit_secret(monkeypatch, tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        _starte(monkeypatch, tmp_path, "0.0.0.0", "geheim")
    assert _warnungen(caplog) == []


def test_keine_warnung_auf_localhost(monkeypatch, tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        _starte(monkeypatch, tmp_path, "127.0.0.1", "")
    assert _warnungen(caplog) == []
