# 🎵 Discord Musikbot in Python

Ein modular aufgebauter Musikbot für Discord mit yt_dlp, FFmpeg, Buttons und Equalizer-Profilen.  
Geschrieben mit [discord.py](https://discordpy.readthedocs.io/en/stable/) und [yt_dlp](https://github.com/yt-dlp/yt-dlp).

---

## 🚀 Features

- Musik abspielen per YouTube-Link oder Playlist
- Steuerbuttons: ⏸️ ▶️ ⏭️ 🔁
- Autoplay-Funktion mit zufälligen Songs
- Equalizer-Presets: `bassboost`, `flat`, `vocalboost`, `superbass`
- Warteschlange verwalten: `!q`, `!shuffle`, `!clear`, `!remove`
- Modularer Aufbau (`cogs/`, `views/`, `config.py`)

---

## 🧠 Befehle (Prefix: `!`)

| Befehl      | Funktion                         |
|-------------|----------------------------------|
| `!p <url>`  | Spielt Musik / Playlist          |
| `!s`        | Nächster Song                    |
| `!x`        | Pause                            |
| `!resume`   | Fortsetzen                       |
| `!q`        | Zeigt die Warteschlange         |
| `!eq <preset>` | Equalizer-Profil wählen      |
| `!shuffle`  | Mische die Queue                 |
| `!replay`   | Letzten Song wiederholen         |
| `!clear`    | Stoppt Musik & leert die Queue   |
| `!remove <n>` | Entfernt Song an Position `n` |
| `!j` / `!leave` | Voice-Channel beitreten/verlassen |
| `!ping` / `!echo` | Testbefehle               |

---

## ⚙️ Installation

### 1. Klonen & Setup

```bash
git clone https://github.com/PabloTestobar/Musicbot.git
cd discord-musikbot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt --upgrade
