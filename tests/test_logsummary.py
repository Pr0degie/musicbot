"""tools/logsummary.py – Zeitleiste aus bot.log."""
from tools.logsummary import main, split_sessions, summarize

LOG = """\
2026-10-24 21:00:00,000 [INFO] DiscordMusicBot: [INIT] MusicCommands erfolgreich initialisiert.
2026-10-24 21:00:01,000 [INFO] DiscordMusicBot: [Wiedergabe] Starte: Alter Lauf
2026-10-24 22:00:00,000 [INFO] DiscordMusicBot: [INIT] MusicCommands erfolgreich initialisiert.
2026-10-24 22:00:01,000 [INFO] discord.voice_state: Connecting to voice...
2026-10-24 22:00:02,000 [INFO] DiscordMusicBot: [Prefetch] Bereits im Cache: X.webm
2026-10-24 22:00:03,000 [INFO] DiscordMusicBot: [Wiedergabe] Starte: Song A
2026-10-24 22:00:04,000 [WARNING] discord.gateway: Shard ID None has stopped responding to the gateway. Closing and restarting.
2026-10-24 22:00:05,000 [WARNING] DiscordMusicBot: [Progress] 429
2026-10-24 22:00:06,000 [WARNING] DiscordMusicBot: [Progress] 429
2026-10-24 22:00:07,000 [ERROR] asyncio: Task exception was never retrieved
Traceback (most recent call last):
  File "x.py", line 1, in y
AttributeError: '_MissingSentinel' object has no attribute 'speaking_timer'
2026-10-24 22:00:08,000 [INFO] DiscordMusicBot: [Voice] Sprachverbindung nach totem Voice-Poller neu aufgebaut: Allgemein
"""


def test_teilt_an_jedem_start_und_nimmt_den_letzten_lauf():
    sessions = split_sessions(LOG.splitlines())
    assert [s.start for s in sessions] == ["21:00:00", "22:00:00"]


def test_zeitleiste_ohne_laerm_mit_zaehlern_und_exception():
    events, stats = summarize(split_sessions(LOG.splitlines())[-1])
    texts = [e.text for e in events]
    assert "▶ Song A" in texts
    assert not any("Prefetch" in t or "Connecting to voice" in t for t in texts)
    assert next(e for e in events if e.text == "[Progress] 429").count == 2
    assert any("Task exception" in t and "speaking_timer" in t for t in texts)
    assert stats["Gateway-Abbrüche"] == 1 and stats["Voice-Neuaufbauten"] == 1
    assert stats["Songs"] == 1 and stats["Fehler"] == 1 and stats["Warnungen"] == 3


def test_cli_gibt_letzten_lauf_aus(tmp_path, capsys):
    log = tmp_path / "bot.log"
    log.write_text(LOG, encoding="utf-8")
    assert main([str(log)]) == 0
    out = capsys.readouterr().out
    assert "Song A" in out and "Alter Lauf" not in out
    assert main([str(log), "--list"]) == 0
    assert "-2" in capsys.readouterr().out
