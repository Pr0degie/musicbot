# ADR 0007: Track-Zustand vor `vc.play()` + Generationszähler gegen Races

- **Status:** Akzeptiert
- **Datum:** 2026-07-11 (Commit `136f1ee`)

## Kontext

Stirbt ein Track sehr schnell (kaputte Quelle, sofortiger FFmpeg-Exit), läuft `after_playing`/der Leere-Queue-Cleanup, während die Post-Play-awaits von `play_next` noch ausstehen. Ohne Schutz überschreiben späte Zuweisungen den Cleanup und `_progress_loop` editiert eine tote Nachricht endlos (Discord-429).

## Entscheidung

- `play_next` setzt den kompletten Track-Zustand (`current_track`, `track_start_time`, `is_playing`, Play-Count …) synchron **vor** `vc.play()`.
- `_track_generation` (erhöht bei Track-Start, Leere-Queue-Cleanup und den dm_speaking-/Radio-Returns) entwertet die Post-Play-awaits eines schnell gestorbenen Tracks — nach jedem await wird geprüft.

## Konsequenzen

- Der Cleanup kappt `now_playing_msg`/`-embed`/`track_start_time`; die letzte Nachricht wandert nach `_ended_np`, damit der nächste Track ihre Buttons entfernen kann.
- Neue Post-Play-Zuweisungen in `play_next` brauchen immer einen Generationscheck nach dem vorangehenden await.
