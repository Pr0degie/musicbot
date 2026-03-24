# 🎵 Discord Musikbot in Python

Ein modular aufgebauter Musikbot für Discord mit yt_dlp, FFmpeg, Buttons und Equalizer-Profilen.
Geschrieben mit [discord.py](https://discordpy.readthedocs.io/en/stable/) und [yt_dlp](https://github.com/yt-dlp/yt-dlp).

---

## 🚀 Features

- Musik abspielen per YouTube-Link, Playlist oder Suchbegriff
- Steuerbuttons direkt in Discord: ⏸️ ▶️ ⏭️ 🔁
- Autoplay-Toggle: aktiviert automatische Wiedergabe wenn die Queue leer läuft
- Equalizer-Presets: `bassboost`, `flat`, `vocalboost`, `superbass`
- Audioformat wählbar: `webm` (Standard) oder `mp3`
- Warteschlange verwalten: `!q`, `!shuffle`, `!clear`, `!remove`
- Download-Cache: bereits gespielte Songs werden lokal gespeichert und nicht neu geladen
- Modularer Aufbau (`cogs/`, `views/`, `utils/`)

---

## 🧠 Befehle (Prefix: `!`)

| Befehl            | Funktion                                        |
|-------------------|-------------------------------------------------|
| `!p <url/suche>`  | Spielt Musik – YouTube-URL, Playlist oder Suchbegriff |
| `!s`              | Nächsten Song überspringen                      |
| `!x`              | Pause                                           |
| `!resume`         | Wiedergabe fortsetzen                           |
| `!q`              | Warteschlange anzeigen                          |
| `!shuffle`        | Warteschlange mischen                           |
| `!clear`          | Stoppt Musik und leert die Queue                |
| `!remove <n>`     | Song an Position `n` entfernen                  |
| `!replay`         | Letzten Song nochmal in die Queue legen         |
| `!eq <preset>`    | Equalizer-Profil setzen (ohne Argument: Liste)  |
| `!format <typ>`   | Audioformat wechseln: `mp3` oder `webm`         |
| `!j`              | Voice-Channel beitreten                         |
| `!l`              | Voice-Channel verlassen                         |
| `!ping` / `!echo` | Testbefehle                                     |

---

## ⚙️ Installation

### 1. Klonen & Setup

```bash
git clone https://github.com/PabloTestobar/Musicbot.git
cd Musicbot
python -m venv venv
source venv/Scripts/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
pip install 'discord.py[voice]' -U
```

### 2. Konfiguration

`.env`-Datei im Projektordner anlegen:

```
DISCORD_TOKEN=dein_token_hier
```

### 3. Starten

```bash
python main.py
```

---

## 📁 Projektstruktur

```
├── main.py           # Einstiegspunkt, Bot-Initialisierung
├── config.py         # Token-Laden, Logging-Basis
├── cogs/
│   ├── basic.py      # Voice-Channel-Befehle (!j, !l, !ping, !echo)
│   └── music.py      # Gesamte Musik-Logik
├── views/
│   └── music_controls.py  # Discord-UI-Buttons (Pause, Resume, Skip, Autoplay)
├── utils/
│   └── logger.py     # Logging-Setup (Konsole + bot.log)
└── downloads/        # Lokaler Audio-Cache
```
