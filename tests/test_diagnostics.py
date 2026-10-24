"""GET /debug – Zustands-Schnappschuss für die Fehlersuche (utils/diagnostics.py)."""
import asyncio
import json
import time
import types

from cogs.dm_bridge import DMBridge
from tests.conftest import build_cog
from utils.diagnostics import snapshot


def run(coro):
    return asyncio.run(coro)


class FakeVC:
    def __init__(self, runner=None):
        self.channel = types.SimpleNamespace(name="circlejerk")
        self.guild = types.SimpleNamespace(name="Server")
        self._connection = types.SimpleNamespace(_runner=runner)
        self._reader = None
        self.latency = float("inf")   # vor dem ersten Voice-Heartbeat

    def is_connected(self):
        return True

    def is_playing(self):
        return True

    def is_paused(self):
        return False


def fake_bot(cogs, vcs):
    return types.SimpleNamespace(
        user="EarRape#8961", latency=0.042, dm_speaking=False,
        voice_clients=vcs, get_cog=lambda name: cogs.get(name),
    )


def test_schnappschuss_zeigt_track_position_und_toten_poller():
    async def body():
        async def crash():
            raise AttributeError("speaking_timer")
        runner = asyncio.ensure_future(crash())
        await asyncio.sleep(0)

        mc = build_cog()
        mc.current_track = ("u1", "Song", 200)
        mc._current_seek_offset = 30
        mc.track_start_time = time.monotonic() - 10
        mc.queue.extend([("u2", "Zwei"), ("u3", "Drei"), ("u4", "Vier"), ("u5", "Fünf")])
        mc._gw_last_recv = time.monotonic() - 5
        mc._start_time = time.monotonic() - 100

        snap = snapshot(fake_bot({"MusicCommands": mc}, [FakeVC(runner)]))
        json.dumps(snap)   # muss JSON-tauglich sein

        assert snap["gateway_latency_ms"] == 42
        vc = snap["voice_clients"][0]
        assert vc["channel"] == "circlejerk"
        assert "speaking_timer" in vc["poller_dead"]
        assert vc["latency_ms"] is None
        m = snap["music"]
        assert m["track"] == "Song" and 39 <= m["position_s"] <= 41
        assert m["queue_len"] == 4 and m["queue_next"] == ["Zwei", "Drei", "Vier"]
        assert 4 <= m["gateway_silent_s"] <= 6
        assert snap["voice_listen"] is None
    run(body())


def test_schnappschuss_ohne_cogs_und_ohne_voice():
    bot = fake_bot({}, [])
    bot.latency = float("nan")
    snap = snapshot(bot)
    assert snap["voice_clients"] == [] and snap["music"] is None
    assert snap["gateway_latency_ms"] is None


class FakeRequest:
    def __init__(self, remote):
        self.remote = remote


def bridge():
    b = DMBridge.__new__(DMBridge)
    b.bot = fake_bot({}, [])
    return b


def test_debug_nur_von_localhost():
    async def body():
        resp = await bridge()._handle_debug(FakeRequest("192.168.1.20"))
        assert resp.status == 403
        resp = await bridge()._handle_debug(FakeRequest("127.0.0.1"))
        assert resp.status == 200
        assert json.loads(resp.text)["bot"] == "EarRape#8961"
    run(body())
