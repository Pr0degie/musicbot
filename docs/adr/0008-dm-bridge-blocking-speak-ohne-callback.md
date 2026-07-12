# ADR 0008: DM-Bridge — blockierendes `/speak` ohne Rück-Callback

- **Status:** Akzeptiert
- **Datum:** 2026-06-04 (Commits `a162648`, `249cc38`; `dm_speaking`-Flag: `82393da`, 2026-06-05)

## Kontext

Ein separater „Bot B" (AI dungeon master) soll diesen Bot über HTTP sprechen lassen. Bot B braucht ein Fertig-Signal; gleichzeitig darf die Musik-Wiedergabe dem DM-Voice-Client nicht in die Quere kommen.

## Entscheidung

- `/speak` is **blocking** — the HTTP response is the only done-signal; `_speak_lock` serializes calls, stops running music first.
- No callback back to Bot B by design — feedback-loop protection lives entirely in Bot B.

## Konsequenzen

- The `bot.dm_speaking` flag tells the music cog it doesn't own the voice client right now: `play_next()` checks it and exits early so the `after_playing` callback can't restart music mid-DM-speech.
- Transportmodi, Auth und Endpoints → `docs/architecture.md` → *DM-Bridge*.
