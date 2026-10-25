"""Der Bot muss ohne die optionalen Voice-Pakete starten (requirements-voice.txt).

numpy, faster-whisper und discord-ext-voice-recv braucht nur das eigene
Zuhören. Fehlen sie, darf kein Modul beim Import scheitern – sonst bricht der
Start ab, sobald jemand nur requirements.txt installiert hat.
"""
import subprocess
import sys

BLOCK_AND_IMPORT = """
import sys
for mod in ("numpy", "faster_whisper", "ctranslate2", "discord.ext.voice_recv"):
    sys.modules[mod] = None          # None in sys.modules → ImportError beim Import
import cogs.basic, cogs.music, cogs.dm_bridge, cogs.voice_listen, cogs.downloader
import utils.voice, utils.pcm, utils.speech_buffer, utils.stt, utils.diagnostics
from utils.voice import _recv_client_cls
assert _recv_client_cls() is None, "ohne voice_recv muss der normale VoiceClient greifen"
print("ok")
"""


def test_bot_module_laden_ohne_voice_pakete(tmp_path):
    env_log = {"MUSICBOT_LOG_FILE": str(tmp_path / "test.log")}
    import os
    r = subprocess.run([sys.executable, "-c", BLOCK_AND_IMPORT], capture_output=True, text=True,
                       env={**os.environ, **env_log}, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    assert r.stdout.strip().endswith("ok")
