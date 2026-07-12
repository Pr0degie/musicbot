"""Tests für !restart: Windows → Popen in neuer Konsole (CREATE_NEW_CONSOLE) +
os._exit, Linux/macOS → os.execv in-place. Vor beiden Pfaden: Voice-Disconnect
+ Flush (Scores, dann Cache). Der frühere wt.exe/WSL-Pfad existiert nicht mehr."""

import asyncio
import sys
import types

import cogs.basic as basic_mod
from cogs.basic import BasicCommands
from tests.conftest import FakeCtx

FAKE_NEW_CONSOLE = 0x10


class FakeVC:
    def __init__(self):
        self.disconnected = []

    async def disconnect(self, force=False):
        self.disconnected.append(force)


def _setup(monkeypatch, platform):
    calls = []
    vc = FakeVC()
    music = types.SimpleNamespace(
        _stopped_by_user=False,
        _flush_scores_now=lambda: calls.append("flush_scores"),
        dl=types.SimpleNamespace(flush_cache_now=lambda: calls.append("flush_cache")),
    )
    bot = types.SimpleNamespace(
        get_cog=lambda name: music,
        voice_clients=[vc],
    )

    def fake_popen(args, **kwargs):
        calls.append(("popen", list(args), kwargs.get("creationflags")))

    monkeypatch.setattr(sys, "platform", platform)
    # Auf Linux-Python existiert das Attribut nicht → für den Test bereitstellen.
    monkeypatch.setattr(basic_mod.subprocess, "CREATE_NEW_CONSOLE", FAKE_NEW_CONSOLE, raising=False)
    monkeypatch.setattr(basic_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(basic_mod.os, "_exit", lambda code: calls.append("os._exit"))
    monkeypatch.setattr(basic_mod.os, "execv", lambda *a: calls.append(("execv", a)))
    return BasicCommands(bot), music, vc, calls


def test_restart_windows_new_console(monkeypatch):
    async def run():
        cog, music, vc, calls = _setup(monkeypatch, platform="win32")
        await cog.restart.callback(cog, FakeCtx())
        assert vc.disconnected == [True]
        assert music._stopped_by_user is True
        # Flush-Reihenfolge bleibt: Scores → Cache, danach Popen + harter Exit.
        assert calls[:2] == ["flush_scores", "flush_cache"]
        kind, args, creationflags = calls[2]
        assert kind == "popen"
        assert args == [sys.executable] + sys.argv
        assert creationflags == FAKE_NEW_CONSOLE
        assert calls[3] == "os._exit"
        assert len(calls) == 4  # insbesondere: kein execv auf Windows

    asyncio.run(run())


def test_restart_linux_execv_in_place(monkeypatch):
    async def run():
        cog, music, vc, calls = _setup(monkeypatch, platform="linux")
        await cog.restart.callback(cog, FakeCtx())
        assert vc.disconnected == [True]
        assert music._stopped_by_user is True
        assert calls[:2] == ["flush_scores", "flush_cache"]
        kind, args = calls[2]
        assert kind == "execv"
        assert args == (sys.executable, [sys.executable] + sys.argv)
        assert len(calls) == 3  # kein Popen, kein os._exit auf Linux

    asyncio.run(run())
