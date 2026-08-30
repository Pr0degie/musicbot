# Sprachsteuerung testen

Schritt-für-Schritt-Anleitung für den ersten Praxistest von
„yo bot, spiel mal …". Technischer Hintergrund →
[ADR 0011](adr/0011-sprachsteuerung-ein-parser-zwei-eingaenge.md).

**Stand:** Der Code ist gebaut und automatisiert getestet (544 Tests).
Ungetestet ist bisher nur der echte Audio-Empfang aus einem laufenden
Voice-Channel — genau dafür ist diese Liste da.

---

## Vorbereitung

Die `.env` ist bereits vorbereitet:

```
VOICE_CONTROL=true
VOICE_OWN_LISTEN=true
DM_BOT_USER_ID=          ← wird in Teil 2 gefüllt
```

- [ ] Bot **neu starten** (`start.bat`) — ein laufender Bot hat noch den alten Code
- [ ] Im Terminal steht `[DMBridge] /command aktiv – Sprachbefehle von Bot B.`

Fehlt diese Zeile, greift `VOICE_CONTROL=true` nicht. Dann `.env` prüfen.

---

## Teil 1 — Ohne Mikrofon: funktioniert die Kette?

Das prüft Parser, Rechte und Command-Ausführung, bevor Audio ins Spiel kommt.

- [ ] `!j` — Bot kommt in deinen Voice-Channel
- [ ] `!listen on` — Bestätigung im Chat, im Terminal `[Voice-Listen] Aktiviert`
- [ ] Deine Discord-ID holen: Rechtsklick auf dich selbst → **ID kopieren**
      (Entwicklermodus muss in den Discord-Einstellungen an sein)
- [ ] In einem Terminal (Git Bash), `DEINE_ID` ersetzen:

```bash
curl -X POST http://127.0.0.1:8765/command \
  -H "Content-Type: application/json" \
  -d '{"text":"yo bot spiel mal Bohemian Rhapsody","user_id":"DEINE_ID"}'
```

**Erwartet:** Antwort `{"status":"executed","command":"p","arg":"Bohemian Rhapsody"}`,
im Chat „🎙️ Verstanden", der Song startet, im Terminal eine
`[Sprachbefehl] (bridge)`-Zeile.

Weitere Sätze zum Durchprobieren (jeweils `text` austauschen):

| Satz | Erwartet |
|---|---|
| `yo bot überspring das mal` | überspringt (`!s`) |
| `yo bot mach mal die Musik aus` | stoppt (`!stop`) |
| `yo bot was läuft als nächstes` | zeigt die Queue |
| `yo bot mach mal mehr Bass rein` | schaltet EQ auf `bassboost` |
| `wie war eigentlich dein Tag` | **nichts** — `{"status":"ignored","reason":"no_wake_word"}` |
| `yo bot blablubb` | Rückfrage im Chat, kein Command |

- [ ] Der letzte Fall postet **nichts** in den Chat (nur `bot.log` bekommt eine `[STT]`-Zeile)

---

## Teil 2 — Die Weiche: DM-Bot erkennen

Ziel: sitzt der DM-Bot im Channel, lädt der Musikbot **kein eigenes Modell**.

- [ ] DM-Bot in denselben Voice-Channel holen
- [ ] `!listen off`, dann `!listen on`
- [ ] Der Musikbot schreibt dir jetzt eine Zeile wie:
      *„💡 **DungeonMaster** ist auch im Channel. Wenn das dein DM-Bot ist, trag
      `DM_BOT_USER_ID=123456789` in die .env ein …"*
- [ ] Diese ID in die `.env` bei `DM_BOT_USER_ID=` eintragen
- [ ] Bot neu starten

Ab jetzt gilt die Weiche. Prüfen:

- [ ] Mit DM-Bot im Channel: `!listen on` → meldet „über **DM-Bot**", **kein**
      „Lade Sprachmodell", `nvidia-smi` zeigt **keinen** zusätzlichen Speicher
- [ ] DM-Bot verlässt den Channel → nach ~10 Sekunden „Lade Sprachmodell medium",
      danach hört der Musikbot selbst zu
- [ ] DM-Bot kommt zurück → **sofort** Meldung über den Quellenwechsel,
      `nvidia-smi` zeigt die ~2,5 GB wieder frei

---

## Teil 3 — Mit Mikrofon: hört er wirklich zu?

Voraussetzung: DM-Bot ist **nicht** im Channel (sonst hört der Musikbot
absichtlich nicht selbst).

- [ ] `!j`, dann `!listen on` → „⏳ Lade Sprachmodell `medium`", nach ein paar
      Sekunden die Bereitschaftsmeldung
- [ ] `nvidia-smi` zeigt ~2,5 GB mehr belegt
- [ ] **Sprich:** „yo bot, spiel mal Bohemian Rhapsody"
      → Bestätigung im Chat, Song startet
- [ ] **Sprich:** „yo bot, überspring das mal" → springt weiter
- [ ] **Sprich normal weiter, ohne „yo bot"** → im Chat passiert **nichts**
- [ ] `!listen off` → Zuhören endet, Modell wird freigegeben

### Der wichtigste Regressionstest

- [ ] Song läuft, `!s` drücken → danach **noch einmal sprechen**.
      Der Bot muss weiterhin reagieren.

Hintergrund: bei aktivem Empfang beendet ein rohes `vc.stop()` auch das
Zuhören. Das ist entschärft, aber genau hier würde es auffallen — und zwar nur
hier.

---

## Wenn etwas nicht klappt

Zuerst immer `bot.log` ansehen (bekommt **immer** die volle Diagnose,
unabhängig vom Terminal-Modus). Alternativ `!debug on` für mehr im Terminal.

| Symptom | Wahrscheinliche Ursache |
|---|---|
| Keine `[DMBridge] /command aktiv`-Zeile beim Start | `VOICE_CONTROL` nicht `true`, oder Bot nicht neu gestartet |
| `curl` antwortet `{"reason":"disabled"}` | `!listen on` fehlt |
| `curl` antwortet `{"reason":"unknown_user"}` | falsche User-ID |
| `curl` antwortet `{"reason":"dm_speaking"}` | der DM-Bot spricht gerade — kurz warten |
| Bot reagiert auf Sprache gar nicht, **`[STT]`-Zeilen im Log vorhanden** | Audio kommt an, nur das Weckwort wird nicht erkannt → sieh nach, was Whisper stattdessen verstanden hat |
| Bot reagiert nicht, **keine `[STT]`-Zeilen** | es kommt kein Audio an → das ist der Punkt, an dem `discord-ext-voice-recv` das Problem wäre |
| „Sprachmodell konnte nicht geladen werden" | GPU/CUDA-Problem. Notfalls `VOICE_STT_ALLOW_CPU=true` (kostet dann CPU-Leistung beim Spielen) |
| Er schneidet dich mitten im Satz ab | `VOICE_SILENCE_MS` erhöhen (Default 800) |
| Er wartet nach dem Sprechen zu lange | `VOICE_SILENCE_MS` senken |
| Leise gesprochene Sätze werden geschluckt | Rauschgate liegt bei −50 dBFS; normale Sprache lag im Test bei −24. Im Log steht „Segment verworfen (zu leise, … dBFS)" |
| Kurze Wörter werden ignoriert | `VOICE_MIN_MS` senken (Default 700) |

---

## Was danach offen bleibt

- **Empfehlungen** („spiel was Chilliges") führen bewusst noch zur Rückfrage.
  Das ist ein eigenes Thema und war ausdrücklich zurückgestellt.
- **Der DM-Bot kann noch nichts schicken.** Damit gesprochene Wünsche *über
  ihn* laufen (statt über das eigene Modell), muss in seinem Repo bei „yo bot"
  im Transkript ein `POST /command` abgesetzt werden — Payload und Auth stehen
  in [ADR 0011](adr/0011-sprachsteuerung-ein-parser-zwei-eingaenge.md). Solange
  das fehlt, hört der Musikbot immer selbst zu, wenn er allein im Channel ist.
- **Weckwörter müssen in beiden Repos gleich sein.** Ändere `VOICE_WAKE_WORDS`
  nie nur auf einer Seite, sonst reagiert der Bot einfach nicht mehr.
