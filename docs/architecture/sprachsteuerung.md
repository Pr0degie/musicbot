# Sprachsteuerung

[← Architektur-Übersicht](../architecture.md)

## Sprachsteuerung ("yo bot, spiel mal ...")

Details und Begründungen → [ADR 0011](../adr/0011-sprachsteuerung-ein-parser-zwei-eingaenge.md).

**Ein Parser, zwei Eingänge.** `VoiceListen.handle_text()` (`cogs/voice_listen.py`)
ist die einzige Stelle, durch die ein gesprochener Satz läuft:

```
Bot B im Voice-Channel?  ──ja──▶  Bot B transkribiert ohnehin
                                  └─▶ POST /command ──┐
                                                      ├──▶ handle_text() ──▶ !p / !s / !x / ...
   ──nein──▶  eigenes Zuhören (voice-recv + Whisper)  ──┘
```

Sitzt Bot B im Channel, hört dieser Bot **nicht** selbst zu: er transkribiert
bereits mit `faster-whisper-medium`, ein zweites Modell daneben wäre doppelte
Arbeit und doppelter VRAM. Erkannt wird er über die konfigurierte
`DM_BOT_USER_ID`, nie per Heuristik.

**Parser** (`utils/nl_parser.py`, rein und ohne Discord-Import): Weckwort-Regex
(tolerant gegenüber Whispers Schreibweisen — "Yo, Bot!", "Jo Bott.", "Yobot"),
dann eine Intent-Tabelle von spezifisch nach generisch. Die Songsuche steht als
Catch-all ganz unten, weil fast jeder Steuersatz ebenfalls ein Spiel-Verb
enthält ("mach mal aus", "spiel weiter"). Die Rückgabe ist ein Tupel
`(weckwort_gefunden, intent)` — ohne Weckwort schweigt der Bot, mit Weckwort
aber ohne Intent stellt er eine Rückfrage.

**Ausführung:** synthetische Message nach dem Muster aus `views/help_view.py`
(Referenz-Nachricht kopieren, Autor auf den Sprecher, Inhalt auf den Befehl,
dann `bot.get_context` + `bot.invoke`). Damit laufen alle bestehenden Checks
mit — Sprache ist kein zweiter, laxerer Weg in den Bot hinein.

**Flags:** `VOICE_CONTROL` (Master, Default aus — ohne ihn existiert
`/command` gar nicht), `VOICE_OWN_LISTEN` (eigenes Zuhören, braucht Neustart),
`DM_BOT_USER_ID`, `VOICE_WAKE_WORDS`, `VOICE_BLOCKED_USER_IDS`; fürs eigene
Zuhören außerdem `VOICE_STT_MODEL`/`_DEVICE`/`_COMPUTE`/`_ALLOW_CPU`
(Default `medium`/`cuda`/`float16`/`false`) und `VOICE_SILENCE_MS`/`_MIN_MS`/`_MAX_MS`
(800/700/12000). Zusätzlich muss `!listen on` zur Laufzeit geschaltet werden
(`check_admin`, siehe `ADMIN_ROLE_ID`); die Nachricht dient als Referenz für
die Command-Ausführung.

**Eigenes Zuhören** (`VOICE_OWN_LISTEN=true`, Neustart nötig): `SpeechSink`
(`cogs/voice_sink.py`) puffert dekodiertes PCM pro Sprecher in
`SpeakerBuffers` (`utils/speech_buffer.py`). Ein Segment gilt als fertig,
wenn `VOICE_SILENCE_MS` (800 ms) kein Paket mehr kam — bewusst über die Paketlücke statt über die
`speaking`-Events, die Clients unzuverlässig setzen. `_flush_loop` (200 ms)
holt fertige Segmente ab, ein Rauschgate bei −50 dBFS verwirft Tastaturklicks
vor der GPU, `utils/pcm.py` rechnet 48 kHz Stereo auf 16 kHz Mono float32 um
(Faktor exakt 3, reine numpy-Mittelung), und `utils/stt.py` transkribiert in
`asyncio.to_thread`. Baut der Music-Cog die Sprachverbindung neu auf
([Voice-Reparatur](voice-connection.md)), hängt `on_voice_rebuilt` den
Empfänger an den neuen Client.

**Die Weiche:** Betritt der DM-Bot den Channel, wird sofort abgeschaltet und
das Modell aus dem VRAM freigegeben. Geht er, wird erst nach 10 s Karenz
wieder geladen — asymmetrisch, weil Abschalten gratis ist und Anschalten
~20 s Ladezeit und 2,5 GB VRAM kostet.

**Logging:** `[Sprachbefehl]` (essential, mit Quellen-Präfix `(bridge)`/`(eigen)`)
für alles mit Weckwort, `[STT]` (quiet) für Transkripte ohne Weckwort.
