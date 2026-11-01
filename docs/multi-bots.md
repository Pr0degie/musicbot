# Mehrere Bots gleichzeitig

`tools/multi_bots.py` startet beliebig viele Instanzen des Musikbots parallel, jede unter
einem eigenen Bot-Account. Ein `!p <song>` im Chat lässt dann alle gleichzeitig spielen
(alle hören auf das Präfix `!`).

## 1. Bot-Accounts anlegen (pro Bot einmal, von Hand)

Das Developer Portal lässt sich nicht automatisieren: Discord bietet dafür keine Bot-API,
und dein Login per Skript zu steuern verstößt gegen die Nutzungsbedingungen (Self-Bot,
der Account kann gesperrt werden). Pro Bot sind es etwa zwei Minuten:

1. <https://discord.com/developers/applications> öffnen → **New Application** → Name
   (z. B. `MusicBot 2`) → Bedingungen bestätigen → **Create**.
2. Links **Bot** wählen:
   - **Reset Token** → bestätigen → Token **kopieren** (wird nur einmal angezeigt).
   - Unter **Privileged Gateway Intents** den **Message Content Intent** einschalten →
     **Save Changes**. Ohne den sieht der Bot keine `!`-Befehle.
3. Links **OAuth2** → **OAuth2 URL Generator**:
   - Scopes: `bot`
   - Bot Permissions: *View Channels*, *Send Messages*, *Embed Links*, *Read Message History*,
     *Connect*, *Speak*. Für den Move-Schutz außerdem *Move Members* und *View Audit Log*.
   - Die erzeugte URL unten kopieren, im Browser öffnen, deinen Server wählen → **Autorisieren**.

Achtung: Auf der OAuth2-Seite steht auch ein **Client Secret**. Das ist *nicht* der Token.
Das Skript erkennt den Unterschied und bricht mit einem Hinweis ab.

## 2. Tokens eintragen

```
python tools/multi_bots.py start
```

Der erste Aufruf legt `multi_bots/tokens.txt` an. Dort einen Token pro Zeile eintragen,
`#` leitet einen Kommentar ein:

```
MTIz...abc.Gh12Kl.xyz...   # MusicBot 2
MTQ1...def.Ab34Cd.uvw...   # MusicBot 3
```

`multi_bots/` steht in `.gitignore`, die Tokens landen also nicht im Repo. Den Token des
Hauptbots (aus `.env`) nicht eintragen. Das Skript lehnt ihn ab, weil derselbe Bot zweimal
eingeloggt um die Voice-Verbindung kämpft.

## 3. Starten, prüfen, beenden

```
python tools/multi_bots.py start          # alle Tokens
python tools/multi_bots.py start -n 3     # nur die ersten 3
python tools/multi_bots.py status         # in einem zweiten Terminal: wer spielt was
```

- Die Instanzen starten im Abstand von 2 Sekunden. Dann: alle per `!j` in den
  Voice-Channel holen, anschließend `!p <song>`.
- **Beenden:** zweimal Strg+C im Fenster des Skripts. Das wirkt auf alle Instanzen
  gleichzeitig, das Skript wartet, bis alle geordnet heruntergefahren sind.
- Der Hauptbot (`start.bat`) kann parallel weiterlaufen.
- Das Skript aktualisiert yt-dlp nicht. Das macht `start.bat` beim Start des Hauptbots.

## Was pro Instanz anders ist

| | Hauptbot | Instanz N |
|---|---|---|
| Arbeitsordner | Repo | `multi_bots/botN/` |
| `bot.log`, `downloads/`, Playlists, Statistik | Repo | im Instanz-Ordner |
| Terminal-Ausgabe | Konsole | `multi_bots/botN/console.log` |
| DM-Bridge / `/debug` | `DM_BRIDGE_PORT` (8765) | 8765 + N, immer nur localhost |
| Sprachsteuerung | laut `.env` | aus |
| alles andere | `.env` | von `.env` geerbt |

Die Fehlersuche funktioniert wie gewohnt, nur mit dem Pfad der Instanz:
`curl http://127.0.0.1:8767/debug` (bot2) bzw.
`python tools/logsummary.py multi_bots/bot2/bot.log`.

## Grenzen

- **Downloads:** Jede Instanz lädt jeden Song selbst. Ein gemeinsamer `downloads/`-Ordner
  geht nicht, weil pro Datei nur ein Prozess schreiben darf (ADR 0002). 10× derselbe Song
  heißt 10 Downloads gleichzeitig, das kann YouTube-Drosselung auslösen. Speicherplatz
  kostet es entsprechend mehrfach (`DOWNLOADS_MAX_MB` gilt pro Instanz).
- **CPU:** Jede Instanz transkodiert mit eigenem ffmpeg. 10 Instanzen spüren schwächere
  Rechner.
- **Befehle:** Jeder Befehl geht an alle Instanzen. Wer im Channel ist, reagiert. Einzelne
  Bots steuerst du, indem du nur die gewünschten per `!j` holst.
- **Lokales Stummschalten** (Rechtsklick → Stummschalten) bleibt pro Bot möglich. Das ist
  eine Einstellung im Client des Zuhörers, kein Bot sieht oder verhindert sie.
- **`!restart`** auf Windows öffnet die Instanz in einem eigenen Konsolenfenster. Sie
  hängt dann nicht mehr am Strg+C des Skripts und wird in ihrem Fenster beendet.
