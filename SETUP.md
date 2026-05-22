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

| Variable | Zweck |
|---|---|
| `DISCORD_TOKEN` | Discord-Bot-Token (erforderlich) |
| `YDL_COOKIES_FILE` | Pfad zu einer exportierten `cookies.txt` — hat Vorrang vor Browser-Extraktion |
| `YDL_BROWSER` | Browser für Live-Cookie-Extraktion (`firefox`, `chrome`, …) — nur lokal, nicht auf Servern ohne GUI |

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
