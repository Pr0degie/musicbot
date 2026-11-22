"""POST /speak im Byte-Modus nimmt WAVs über 1 MiB an.

web.Application() ohne client_max_size kappt Request-Bodies bei aiohttps
Standard von 1 MiB (→ 413), lange bevor der eigene 25-MB-Riegel
(_MAX_WAV_BYTES) greift. Der Test fährt den echten aiohttp-Server auf einem
freien Port hoch und schickt eine 2-MB-WAV hindurch.
"""
import asyncio
import json
import types

import aiohttp
import discord

import cogs.dm_bridge as bridge_mod
from cogs.dm_bridge import DMBridge


class _FakeVoiceClient:
    def __init__(self):
        self.played = []

    def is_connected(self):
        return True

    def is_playing(self):
        return False

    def is_paused(self):
        return False

    def play(self, source, after=None):
        self.played.append(source)
        after(None)   # Wiedergabe sofort "fertig"


def test_speak_bytes_nimmt_wav_ueber_1_mib(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge_mod, "DM_BRIDGE_HOST", "127.0.0.1")
    monkeypatch.setattr(bridge_mod, "DM_BRIDGE_PORT", 0)          # freier Port
    monkeypatch.setattr(bridge_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(discord, "FFmpegOpusAudio", lambda path, **kw: path)

    async def run():
        vc = _FakeVoiceClient()
        bot = types.SimpleNamespace(
            loop=asyncio.get_running_loop(), voice_clients=[vc], dm_speaking=False, user="TestBot",
        )
        cog = DMBridge(bot)
        await cog.cog_load()
        try:
            port = cog._runner.addresses[0][1]
            wav = b"RIFF" + b"\0" * (2 * 1024 * 1024)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{port}/speak", data=wav,
                    headers={"Content-Type": "audio/wav"},
                ) as resp:
                    status, text = resp.status, await resp.text()
        finally:
            await cog.cog_unload()

        assert status == 200, f"2-MB-WAV abgelehnt: HTTP {status}"
        assert json.loads(text) == {"status": "played"}
        assert len(vc.played) == 1
        assert bot.dm_speaking is False
        assert list(tmp_path.glob("dm_recv_*.wav")) == [], "Temp-WAV wurde nicht gelöscht"

    asyncio.run(run())
