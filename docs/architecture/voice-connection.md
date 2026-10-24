# Voice-Verbindung & Resilienz

[← Architektur-Übersicht](../architecture.md)

## Channel-leave timers & voice resilience

Added to survive Discord idle-drops.

- `AUTO_LEAVE_SECONDS` (300 s): `on_voice_state_update()` starts this when the last human leaves → disconnect.
- `IDLE_LEAVE_SECONDS` (7200 s / 2 h): started in `play_next()` when the queue empties and autoplay is off; if
  nothing plays for 2 h the bot leaves. `_cancel_idle_timer()` clears it the moment playback resumes (also on
  radio start). This is a safety net — Discord drops silent voice connections (code **1006**) after ~80 min
  anyway.
- `_voice_watchdog` (`@tasks.loop(seconds=30)`): after a 1006 auto-reconnect, playback can hang (queue
  non-empty but `is_playing` False, vc not playing/paused). If that state persists ≥2 ticks (~60 s) it calls
  `play_next()` to recover. Suppressed by `_stopped_by_user` (set on `!stop`/`!x`) and skipped during radio.
- 1006 reconnect tracebacks are downgraded to a quiet INFO line — see **Logging**.
- **Voice-Reparatur nach Gateway-Abbruch** (`VoiceLifecycleMixin`, `on_disconnect`/`on_resumed`/`on_ready`):
  Fällt die Netzverbindung zu Discord kurz weg (`Shard ID None has stopped responding to the gateway`), baut
  discord.py das Gateway selbst neu auf, die Sprachverbindung bleibt aber stumm zurück — „verbunden“, Player
  „spielt“, kein Ton; der Watchdog sieht das nicht. Nach einer **neuen** Sitzung (statt Resume) leert
  `parse_ready` sogar discord.pys Voice-Client-Liste, der alte Client läuft verwaist weiter. Deshalb:
  `on_socket_raw_receive` (braucht `enable_debug_events=True` in `main.py`) stempelt jedes Gateway-Paket;
  `on_disconnect` merkt sich die Voice-Clients und ob das Gateway ≥ `GATEWAY_ZOMBIE_SECONDS` (50 s) still war.
  Repariert wird nach Resume **nur** bei vorheriger Stille — den routinemäßigen Reconnect, den Discord etwa
  stündlich verlangt, erreicht die Schwelle nie, der darf keinen Tonaussetzer bekommen — und nach einer neuen
  Sitzung **immer**. Die Reparatur (`_heal_one`) reiht den Song wie `!seek` an der Hörposition vorn ein
  (`_current_seek_offset` + `_elapsed_seconds()`), stoppt, meldet den alten Client ab (5-s-Timeout: ein
  verwaister bekommt nie eine Bestätigung), verbindet über `connect_voice` neu und ruft `play_next` selbst;
  währenddessen prallt jedes andere `play_next` (after_playing des alten Clients, Watchdog) an
  `_voice_healing` ab. Radio wird neu gestartet, Pause bleibt Pause. `bot.dispatch("voice_rebuilt")` lässt
  `VoiceListen` den Empfänger an den neuen Client hängen.
- **Toter Voice-Poller** (`utils/voice.py` → `dead_voice_poller`, geprüft im `_voice_watchdog`): discord.py
  hält die Sprachverbindung mit genau einer Task am Leben („Voice websocket poller“). Stirbt sie an einer
  Exception, bleibt `is_connected()` True und der Player sendet ins Leere — Discord zeigt den grünen Ring, es
  kommt kein Ton; 60 s später loggt der *Voice*-Keep-Alive `Shard ID None has stopped responding to the
  gateway` (dieselbe Meldung wie beim Haupt-Gateway, die Klasse erbt sie). Der Watchdog erkennt das binnen 30
  s und repariert über `_heal_one`. Bekannter Auslöser: voice_recv 0.5.2a179 crasht in `_remove_ssrc` (Opcode
  13, ein User verlässt den Channel), wenn nicht zugehört wird (`_reader` fehlt) — `connect_voice` nutzt
  deshalb `SafeVoiceRecvClient` mit Prüfung; `tests/test_voice_heal.py` meldet, sobald das Paket den Fehler
  selbst behebt.
