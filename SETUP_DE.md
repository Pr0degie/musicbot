# Setup & Betrieb

## 1. Voraussetzungen

- Python 3.10+ (CI testet mit 3.12)
- FFmpeg im PATH
- Node.js im PATH (für YouTube-Signatur-Solver)

## 2. Installation

**Windows (primärer Weg):**

```bat
git clone https://github.com/Pr0degie/musicbot.git
cd musicbot
install.bat
```

`install.bat` legt das venv an, installiert die Dependencies, prüft/installiert
FFmpeg via winget und erstellt einen `.env`-Rohling.

**Linux / macOS (manuell):**

```bash
git clone https://github.com/Pr0degie/musicbot.git
cd musicbot
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**Optional – eigenes Zuhören** (`VOICE_CONTROL=true` und `VOICE_OWN_LISTEN=true`, d. h. der Bot erkennt
Sprache selbst, wenn der DM-Bot nicht im Voice-Channel sitzt): `pip install -r requirements-voice.txt`
(faster-whisper, numpy, discord-ext-voice-recv). Zusammen mit dem DM-Bot nicht nötig – der bringt seine eigene
Spracherkennung mit und schickt die erkannten Sätze per `POST /command`. Ohne diese Pakete läuft der Bot
unverändert.

## 3. Konfiguration (`.env`)

`.env`-Datei im Projektordner anlegen:

```
DISCORD_TOKEN=dein_token_hier
```

Optionale Cookie-Variablen für YouTube-Authentifizierung (→ Abschnitt 4):

```
YDL_COOKIES_FILE=/pfad/zur/cookies.txt
YDL_BROWSER=firefox
```

Optionale Spracheinstellung:

```
LANGUAGE=de
```

Optionale DM-Bridge-Variablen (nur für das KI-Dungeon-Master-Setup — für normale Nutzung Default lassen):

```
DM_BRIDGE_HOST=127.0.0.1
DM_BRIDGE_PORT=8765
DM_BRIDGE_SECRET=
```

| Variable | Zweck |
|---|---|
| `DISCORD_TOKEN` | Discord-Bot-Token (erforderlich) |
| `YDL_COOKIES_FILE` | Pfad zu einer exportierten `cookies.txt` — hat Vorrang vor Browser-Extraktion |
| `YDL_BROWSER` | Browser für Live-Cookie-Extraktion (`firefox`, `chrome`, …; Standard `firefox`) — nur lokal, nicht auf Servern ohne GUI. Greift erst, wenn der Cookie-Modus anspringt (→ Abschnitt 4) |
| `LANGUAGE` | Bot-Sprache: `en` (Standard) oder `de` |
| `DM_BRIDGE_HOST` | DM-Bridge-HTTP-Host. `127.0.0.1` (Standard) = lokaler Pfad-Modus; LAN-/Tailscale-IP oder `0.0.0.0` = Remote-Byte-Modus |
| `DM_BRIDGE_PORT` | DM-Bridge-HTTP-Port (Standard `8765`) |
| `DM_BRIDGE_SECRET` | Gemeinsames Secret für die DM-Bridge-Auth. Leer + `127.0.0.1` = ohne Secret; **erforderlich** bei jedem Nicht-Localhost-Host |
| `LOG_MODE` | Terminal-Ausgabe: `quiet` (Standard, Wesentliches + Probleme) oder `debug` (volle Diagnose); zur Laufzeit umschaltbar mit `!debug on\|off` |

Alle weiteren Einstellungen (Security-Flags, Limit für den Download-Cache,
Sprachsteuerung) sind in [`.env.example`](.env.example) dokumentiert. Bei jedem
Start loggt der Bot eine `[Config]`-Zeile mit den Keys aus `.env.example`, die
in deiner `.env` fehlen — die laufen dann einfach auf ihrem Standardwert.

## 4. YouTube-Authentifizierung (Cookies)

Der Bot läuft **standardmäßig ohne Cookies** und schaltet erst auf Cookie-Auth um,
wenn YouTube sie tatsächlich verlangt — altersbeschränkte Videos, Mitglieder-Inhalte
oder eine `Sign in to confirm you're not a bot`-Sperre. Der erste solche Fehler löst
einen automatischen Zweitversuch mit Cookies aus, danach bleibt der Cookie-Modus für
den Rest des Prozesses an (→ [ADR 0010](docs/adr/0010-cookielos-zuerst-cookie-fallback.md)).

Der Grund ist Tempo, nicht Bequemlichkeit: Eine angemeldete Session bekommt
Pre-Roll-Werbung, und yt-dlp muss deren Überspringzeit abwarten, bevor das erste
Audio-Byte kommt (vorher antwortet das CDN mit `403`). Das kostet bei jedem
ungecachten Track 5–6 s — cookielos startet ein Song in etwa 3 s statt 13 s, bei
identischer Audioqualität.

Cookies einzurichten ist daher **empfohlen, aber optional**: Sie sind das
Sicherheitsnetz für die Fälle oben. Ohne konfigurierte Cookie-Quelle scheitern
diese Videos einfach mit YouTubes Fehlermeldung.

### Server-Setup (empfohlen)

Cookies aus einem eingeloggten Browser exportieren und hochladen:

```bash
yt-dlp --cookies-from-browser firefox --cookies cookies.txt --skip-download <beliebige-youtube-url>
scp cookies.txt user@server:/pfad/zu/MusicBot/cookies.txt
```

In `.env` setzen:
```
YDL_COOKIES_FILE=/pfad/zu/MusicBot/cookies.txt
```

### Cookie-Erneuerung

Cookies halten ca. 1–3 Monate. Wenn YouTube wieder blockiert:

1. Cookies erneut exportieren (Befehl oben)
2. Hochladen via SCP
3. `!reloadcookies` in Discord — lädt ohne Bot-Neustart neu und schaltet den
   Cookie-Modus auch dann ein, wenn der Bot gerade cookielos läuft

### Troubleshooting: Browser-Cookie-Extraktion (`YDL_BROWSER`)

`cookiesfrombrowser` liest die Cookie-DB direkt aus einem lokal installierten
Browser. Praktisch für lokale Entwicklung, aber fragil — nur als Fallback
empfohlen. Wenn es scheitert, lieber die **`cookies.txt`-Route** unten nehmen.

**`ERROR: Could not copy Chrome cookie database` (yt-dlp [#7271](https://github.com/yt-dlp/yt-dlp/issues/7271))**

Zwei Ursachen:

1. **Chrome läuft noch** → die Cookie-DB ist gesperrt. Chrome *komplett*
   schließen, inkl. Hintergrundprozesse (System-Tray / unter Windows
   `taskkill /F /IM chrome.exe`), dann Bot neu starten.
2. **Chrome ≥ 127 (Windows): App-Bound Encryption** → Cookies sind verschlüsselt,
   yt-dlp kann sie *auch bei geschlossenem Chrome* nicht lesen. Dafür gibt es
   keinen zuverlässigen Fix bei der Direkt-Extraktion — nimm die
   `cookies.txt`-Route unten.

**Firefox-Extraktion scheitert auch (`could not find ... cookies database` / leere Cookies)**

Meist eine dieser Ursachen:

- Im betreffenden Firefox-Profil nicht wirklich bei YouTube eingeloggt.
- Mehrere Profile — yt-dlp nimmt das Default-Profil, das evtl. nicht das
  eingeloggte ist. Gezielt ein Profil ansprechen: `YDL_BROWSER=firefox:<Profilname>`
  (Profilname via `about:profiles`), z. B. `firefox:default-release`.
- Snap-/Flatpak-Firefox (Linux) legt das Profil in einem ungewöhnlichen Pfad ab,
  den yt-dlp nicht findet → nimm die `cookies.txt`-Route.

### Empfohlener Fallback: `cookies.txt` exportieren (browser-unabhängig)

Umgeht die Browser-Extraktion komplett — funktioniert unabhängig von
Chrome-/Firefox-Update-Problemen und sogar bei geöffnetem Browser:

1. Browser-Extension **„Get cookies.txt LOCALLY"** installieren, auf
   `youtube.com` eingeloggt die `cookies.txt` exportieren.
2. In die `.env` (hat Vorrang vor `YDL_BROWSER`, der Browser wird dann gar nicht
   mehr angefasst):
   ```
   YDL_COOKIES_FILE=C:\pfad\zur\cookies.txt
   ```
3. `!reloadcookies` in Discord ausführen — kein Neustart nötig.

> Sicherheitshinweis: Die `cookies.txt` enthält deine YouTube-Session.
> Nicht committen, nicht teilen — lokal halten / aus git heraushalten (`.gitignore`).

### EJS Signature Solver

yt-dlp benötigt Node.js im PATH sowie `yt-dlp[default]` für YouTube-Signatur-Challenges:

```bash
pip install "yt-dlp[default]"
```

`js_runtimes: {node: {}}` ist bereits in allen ydl-Instanzen in `update_ydl()` konfiguriert.

## 5. Starten

**Windows (primärer Weg):**

```bat
start.bat
```

Aktiviert das venv (falls vorhanden) und startet `python main.py` in einem
eigenen Konsolenfenster — das `start.bat`-Fenster schließt sich sofort. Strg+C
im Bot-Fenster ist zweistufig: einmal drücken fragt nach, ein zweites Mal
innerhalb von 5 s fährt geordnet herunter (ein drittes bricht hart ab).
`!restart` öffnet ein neues Konsolenfenster; das alte schließt sich von selbst.

**Linux / macOS:**

```bash
python main.py
```

## 6. Voice- & Auto-Disconnect-Verhalten

Der Bot verlässt den Voice-Channel in zwei Fällen automatisch:

- **Keine User mehr im Channel** → nach 5 Min (`AUTO_LEAVE_SECONDS = 300`).
- **Lange keine Wiedergabe** → nach 2 h Stille (`IDLE_LEAVE_SECONDS = 7200`).
  Discord wirft stille Voice-Verbindungen ohnehin nach ~80 Min mit Fehler **1006**
  weg; freiwilliges Verlassen vermeidet, auf einer toten Verbindung zu sitzen.

Passiert ein 1006-Abbruch *während* der Wiedergabe, reconnectet discord.py
automatisch, und ein eingebauter **Reconnect-Watchdog** startet den nächsten Track
neu, falls die Wiedergabe hängen blieb — transparent, ohne Konfiguration. Diese
Reconnects werden als ruhige INFO-Zeile statt als roter Traceback geloggt; eine
solche Zeile gelegentlich in `bot.log` zu sehen ist also normal.

Fällt die Netzverbindung zu Discord länger weg (oder stirbt die Sprachverbindung
still), baut der Bot die Sprachverbindung selbst neu auf, setzt den Song an der
aktuellen Stelle fort und schreibt einen kurzen Hinweis in den Chat
(`🔌 … neu verbunden`).

## 7. Logs & Fehlersuche

- `bot.log` enthält immer die volle Diagnose, unabhängig von `LOG_MODE`/`!debug`.
  Es rotiert bei jedem Start: der aktuelle Lauf steht in `bot.log`, die zehn
  vorherigen bleiben als `bot.log.1` (vorheriger) … `bot.log.10` erhalten.
  Startfehler landen ebenfalls dort, weil sich das `start.bat`-Fenster sofort
  schließt.
- `python tools/logsummary.py` verdichtet den letzten Lauf zu einer Zeitleiste
  (Warnungen, Fehler, Voice-/Gateway-Ereignisse, Befehle, Songs). `--list` zeigt
  alle Läufe einer Datei, `-n 30` nur die letzten 30 Ereignisse,
  `python tools/logsummary.py bot.log.1` den vorherigen Lauf.
- Solange der Bot läuft, liefert `GET /debug` an der DM-Bridge (Standard
  `http://127.0.0.1:8765/debug`) einen Zustands-Schnappschuss als JSON — es
  antwortet nur auf Anfragen von derselben Maschine.
