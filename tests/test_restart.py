"""Tests für !restart: wt.exe/WSL-Pfad wird zuerst versucht, os.execv ist der
Normalfall ohne Windows Terminal. Vor beiden Pfaden: Voice-Disconnect + Flush."""

import asyncio
import types

import cogs.basic as basic_mod
from cogs.basic import BasicCommands
from tests.conftest import FakeCtx


class FakeVC:
    def __init__(self):
        self.disconnected = []

    async def disconnect(self, force=False):
        self.disconnected.append(force)


def _setup(monkeypatch, popen_raises):
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

    def fake_popen(*args, **kwargs):
        if popen_raises:
            raise FileNotFoundError
        calls.append("wt")

    monkeypatch.setattr(basic_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(basic_mod.os, "_exit", lambda code: calls.append("os._exit"))
    monkeypatch.setattr(basic_mod.os, "execv", lambda *a: calls.append("execv"))
    return BasicCommands(bot), music, vc, calls


def test_restart_wt_path_first(monkeypatch):
    async def run():
        cog, music, vc, calls = _setup(monkeypatch, popen_raises=False)
        await cog.restart.callback(cog, FakeCtx())
        assert vc.disconnected == [True]
        assert music._stopped_by_user is True
        assert calls == ["flush_scores", "flush_cache", "wt", "os._exit"]

    asyncio.run(run())


def test_restart_execv_fallback_without_wt(monkeypatch):
    async def run():
        cog, music, vc, calls = _setup(monkeypatch, popen_raises=True)
        await cog.restart.callback(cog, FakeCtx())
        assert vc.disconnected == [True]
        assert calls == ["flush_scores", "flush_cache", "execv"]

    asyncio.run(run())
