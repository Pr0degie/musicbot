# Logging & Fehlersuche

[← Architektur-Übersicht](../architecture.md)

## Logging

`utils/logger.py` — console + `bot.log`. `config.py` must NOT call `logging.basicConfig()` — silently disables
the file handler. Import `logger` from `utils.logger`.

**Terminal-Modi:** `LOG_MODE` aus `.env` (`quiet` Default | `debug`) setzt den Start-Modus des
Console-Handlers: `quiet` = wesentliche Ereignisse + alle echten Probleme, `debug` = volle Diagnose. `quiet`
filtert dabei über den `[Tag]` am Zeilenanfang, nicht über das Handler-Level: Songwechsel, Queue und Radio
laufen als INFO, ein Level-Filter auf WARNING würde den kompletten Normalbetrieb verschlucken. Maßgeblich sind
`_ESSENTIAL_TAGS` (sichtbar) und `_QUIET_TAGS` (still) in `utils/logger.py`; WARNING+ passiert den Filter
immer, auch bei stillen Tags. `tests/test_logger_mode.py` erzwingt, dass jeder im Code benutzte INFO-Tag in
genau einer der beiden Listen steht — sonst verschwände eine neue Meldung stillschweigend aus dem Terminal.
`!debug on|off` (`cogs/basic.py`) schaltet zur Laufzeit via `set_console_mode()` um. Beim Start meldet
`[Config]` (essential) die Keys aus `.env.example`, die in der eigenen `.env` fehlen und still auf Default
laufen (`utils/env_check.py`, aufgerufen in `main.py` → `setup_hook`). **`bot.log` bekommt immer
die volle Diagnose** — der File-Handler wird nie gefiltert.
`_VoiceReconnectFilter` (attached to the `discord.voice_state` logger) downgrades the noisy code-1006
reconnect message from ERROR to INFO and strips its traceback — discord.py auto-reconnects on idle channels
are expected, so they're logged quietly instead of as a red stack trace (rewritten to a `[Voice] Verbindung kurz
unterbrochen …` line, which therefore also shows in the `quiet` terminal).
Der `discord`-Logger steht auf WARNING (Gateway-/HTTP-Spam), mit einer Ausnahme: `discord.gateway`,
`discord.client` und `discord.voice_state` loggen INFO — Resume vs. neue Sitzung, Voice-Close-Codes und
Voice-Reconnects sind die einzige Spur, wenn nach einem Netz-Abbruch der Ton fehlt. Sie tragen keinen `[Tag]`,
landen im `quiet`-Terminal also nicht, nur in `bot.log` (Ausnahme: die von `_VoiceReconnectFilter`
umgeschriebene Zeile). Die eigenen `[Gateway]`-Zeilen der Voice-Reparatur stehen in `_QUIET_TAGS` (Discord
trennt etwa stündlich routinemäßig).

## Rotation

`utils/logger.py` → `rotate_on_start()`: Jeder Bot-Start beginnt ein frisches `bot.log`, die letzten
`LOG_KEEP_RUNS` (10) Läufe bleiben als `bot.log.1` (vorheriger Lauf) … `bot.log.10`. Bewusst beim Start statt
`RotatingFileHandler`: unter Windows scheitert das Umbenennen, solange ein anderes Programm die Datei offen
hat — mitten im Lauf hieße das bei jeder Zeile `--- Logging error ---`. Scheitert die Rotation (Editor offen,
`!restart` mit noch lebendem Altprozess), wird wie früher angehängt. Die Testsuite schreibt über
`MUSICBOT_LOG_FILE` nach `%TEMP%/musicbot-tests.log` (gesetzt in `tests/conftest.py` vor dem ersten Import).

## Fehlersuche

Reihenfolge, die am wenigsten Log-Archäologie kostet:

1. **Läuft der Bot noch und wie geht es ihm?** `curl http://127.0.0.1:8765/debug` (DM-Bridge, nur von
   localhost) liefert `utils/diagnostics.py` → `snapshot()`: Voice-Clients (verbunden, spielt, `poller_dead`
   mit der Exception, falls die Sprachverbindung tot ist, hört zu, Latenz), Gateway-Latenz und Stille seit dem
   letzten Paket, aktueller Track mit Position, Queue, Radio, Loop/Autoplay/EQ, ob eine Voice-Reparatur läuft,
   Sprachsteuerungs-Zustand. Liest nur, crasht nie.
2. **Was ist passiert?** `python tools/logsummary.py` verdichtet den letzten Lauf auf eine Zeitleiste
   (Warnungen/Fehler mit Wiederholungszähler, Tracebacks auf die Exception-Zeile gekürzt, Gateway/Voice,
   Eingaben, gestartete Songs). `bot.log.1` = vorheriger Lauf, `--list` zeigt alle Läufe einer Datei mit
   Kennzahlen, `--session -2` wählt den vorletzten Lauf derselben Datei, `-n` begrenzt (Default 80). Importiert `utils.logger` bewusst nicht (der Import würde rotieren).
3. **Hängt etwas?** `py-spy dump --pid <pid>` zeigt alle Thread-Stacks des laufenden Prozesses, ohne ihn
   anzuhalten. Ein `audio-player`-Thread in `time.sleep` heißt: es wird gesendet — kommt trotzdem kein Ton, ist
   die Sprachverbindung tot (→ `poller_dead` in `/debug`).

Achtung, Verwechslungsgefahr: `Shard ID None has stopped responding to the gateway` loggt auch der
**Voice**-Keep-Alive (die Klasse erbt die Meldung). Ohne folgendes `[Gateway] Verbindung getrennt` war es die
Sprachverbindung, nicht das Haupt-Gateway → [voice-connection.md](voice-connection.md).
