# Setup & Betrieb

## 1. Voraussetzungen

- Python 3.10+
- FFmpeg im PATH
- Node.js im PATH (für YouTube-Signatur-Solver)

## 2. Installation

```bash
git clone https://github.com/Pr0degie/musicbot.git
cd Musicbot
python -m venv venv

# Windows:
venv\Scripts\activate
# Linux / macOS:
source venv/bin/activate

pip install -r requirements.txt
```

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
| `YDL_BROWSER` | Browser für Live-Cookie-Extraktion (`firefox`, `chrome`, …) — nur lokal, nicht auf Servern ohne GUI |
| `LANGUAGE` | Bot-Sprache: `en` (Standard) oder `de` |
| `DM_BRIDGE_HOST` | DM-Bridge-HTTP-Host. `127.0.0.1` (Standard) = lokaler Pfad-Modus; LAN-/Tailscale-IP oder `0.0.0.0` = Remote-Byte-Modus |
| `DM_BRIDGE_PORT` | DM-Bridge-HTTP-Port (Standard `8765`) |
| `DM_BRIDGE_SECRET` | Gemeinsames Secret für die DM-Bridge-Auth. Leer + `127.0.0.1` = ohne Secret; **erforderlich** bei jedem Nicht-Localhost-Host |

## 4. YouTube-Authentifizierung (Cookies)

YouTube blockiert unauthentifizierte Bot-Anfragen. Cookie-Auth ist notwendig.

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
3. `!reloadcookies` in Discord — lädt ohne Bot-Neustart neu

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
