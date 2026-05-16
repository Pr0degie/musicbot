# 🎵 Discord Musikbot

Ein modularer Discord-Musikbot mit yt_dlp, FFmpeg, Buttons und Equalizer-Profilen.  
Geschrieben mit [discord.py](https://discordpy.readthedocs.io/en/stable/) und [yt_dlp](https://github.com/yt-dlp/yt-dlp).

---

## Features

- YouTube-Links, Playlists und Suchbegriffe abspielen
- Steuerbuttons direkt in Discord: ⏸️ ▶️ ⏭️ 🔁
- Autoplay: sucht automatisch den nächsten Song wenn die Queue leer läuft
- Equalizer-Presets: `bassboost`, `flat`, `vocalboost`, `superbass`, `punchy`, `nightcore`, `karaoke`, `8d`
- Audioformat: `webm` (Standard, verlustfrei) oder `mp3`
- Internet-Radio via direktem Stream
- Queue-Verwaltung mit Speichern/Laden, Shuffle, Loop-Modus
- Kurze Songs werden gecacht, lange Songs (>20 min) und unbekannte Songs starten sofort per Stream

---

## Befehle (Prefix: `!`)

**Wiedergabe**

| Befehl | Funktion |
|---|---|
| `!p <url/suche>` | Musik abspielen — YouTube-URL, Playlist oder Suchbegriff |
| `!next <url/suche>` | Song als nächsten in die Queue legen |
| `!s` | Überspringen |
| `!x` | Pause |
| `!resume` | Fortsetzen |
| `!now` | Aktuellen Song anzeigen |
| `!seek <zeit>` | Position springen — z.B. `1:23` oder `83` |
| `!replay` | Letzten Song nochmal in die Queue |

**Queue**

| Befehl | Funktion |
|---|---|
| `!q` | Queue anzeigen (paginiert) |
| `!shuffle` | Queue mischen |
| `!clear` | Stopp + Queue leeren |
| `!remove <n>` | Song an Position `n` entfernen |
| `!move <n>` | Ab Position `n` abspielen |
| `!loop` | Loop-Modus: aus → Song → Queue → aus |
| `!saveq <name>` | Aktuelle Queue speichern |
| `!loadq <name>` | Gespeicherte Queue laden |
| `!lists` | Gespeicherte Queues auflisten |

**Audio & Radio**

| Befehl | Funktion |
|---|---|
| `!eq <preset>` | Equalizer-Preset setzen (ohne Argument: Liste) |
| `!format <typ>` | Audioformat wechseln: `mp3` oder `webm` |
| `!radio` | Senderliste anzeigen |
| `!radio <nr/name>` | Sender abspielen |
| `!radio <url> [Name]` | Stream abspielen & speichern |
| `!radio delete <nr/name>` | Sender löschen |
| `!radio rename <nr/name> <neuer name>` | Sender umbenennen |
| `!stop` | Radio stoppen |

**Sonstiges**

| Befehl | Funktion |
|---|---|
| `!text` | Songtext abrufen |
| `!stats` | Bot-Statistiken (RAM, CPU, Cache) |
| `!j` / `!l` | Voice-Channel beitreten / verlassen |

---

## Projektstruktur

```
├── main.py                    # Einstiegspunkt
├── config.py                  # Env-Variablen laden
├── cogs/
│   ├── music.py               # Musik-Logik, Queue, Autoplay, Radio
│   ├── downloader.py          # yt_dlp-Instanzen, Download-Cache, Streaming
│   ├── presets.py             # EQ-Filterketten
│   └── basic.py               # !j, !l, !ping, !echo, !help
├── views/
│   └── music_controls.py      # Discord-Buttons (Pause, Resume, Skip, Autoplay)
├── utils/
│   └── logger.py              # Logging (Konsole + bot.log)
├── downloads/                 # Audio-Cache
└── playlists/                 # Gespeicherte Queues (!saveq / !loadq)
```

---

## Setup

→ Siehe [`SETUP.md`](SETUP.md)
