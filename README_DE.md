# 🎵 Discord Musikbot

Ein modularer Discord-Musikbot mit yt_dlp, FFmpeg, Buttons und Equalizer-Profilen.  
Geschrieben mit [discord.py](https://discordpy.readthedocs.io/en/stable/) und [yt_dlp](https://github.com/yt-dlp/yt-dlp).

---

## Features

- YouTube-Links, Playlists und Suchbegriffe abspielen
- Steuerbuttons direkt in Discord: ⏸️ ▶️ ⏭️ 🔁 🔂
- Autoplay: sucht automatisch den nächsten Song wenn die Queue leer läuft
- Equalizer-Presets: `bassboost`, `flat`, `vocalboost`, `superbass`, `punchy`, `nightcore`, `karaoke`, `8d`
- Audioformat: `webm` (Standard, verlustfrei) oder `mp3`
- Internet-Radio via direktem Stream
- Queue-Verwaltung mit Speichern/Laden, Shuffle, Loop-Modus
- Kurze Songs werden gecacht, lange Songs (>20 min) starten sofort per Stream
- Auto-Leave: Bot verlässt den Voice-Channel wenn alle User weg sind oder nach 2 h Inaktivität
- Voice-Stabilität: Reconnect-Watchdog setzt die Wiedergabe nach Discord-Abbrüchen (Code 1006) fort; nach einem Netzabbruch baut der Bot die Sprachverbindung selbst neu auf und schreibt einen Hinweis in den Chat
- Mute-Schutz: entmutet sich bei Server-Mute selbst (braucht *Mitglieder stummschalten*), pausiert sonst
- Move-Schutz (optional): nur der Owner darf den Bot verschieben oder rauswerfen, alles andere macht er rückgängig
- Sprachsteuerung („yo bot, spiel mal …") — optional, standardmäßig aus (`VOICE_CONTROL`, `!listen`)

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
| `!now <url/suche/n>` | Song sofort abspielen, aktuellen überspringen — auch Queue-Position `n` |
| `!seek <zeit>` | Position springen — z.B. `1:23` oder `83` |
| `!replay` | Letzten Song nochmal in die Queue |

**Queue**

| Befehl | Funktion |
|---|---|
| `!q` | Queue anzeigen (paginiert) |
| `!shuffle` | Queue mischen |
| `!clear` | Stopp + Queue leeren |
| `!remove <n>` | Song an Position `n` entfernen |
| `!move <n\|titel>` | Song nach vorne schieben — per Position oder Titelsuche |
| `!loop` | Loop-Modus: aus → Song → Queue → aus |
| `!saveq <name>` | Aktuelle Queue speichern |
| `!loadq <name>` | Gespeicherte Queue laden |
| `!loadq last` | Queue der letzten Session laden |
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
| `!stop` | Wiedergabe/Radio stoppen (Queue bleibt erhalten) |

**Sonstiges**

| Befehl | Funktion |
|---|---|
| `!score` | Top-10 der meistgespielten Songs |
| `!baba` | Baba-Playlist abspielen |
| `!text` | Songtext abrufen |
| `!stats` | Bot-Statistiken (RAM, CPU, Cache) |
| `!help` / `!h` | Befehlsübersicht (blätterbar) |
| `!j` / `!l` | Voice-Channel beitreten / verlassen |
| `!dm` | DM-Bridge-Status anzeigen (KI-Dungeon-Master-Audioausgabe) |
| `!reloadcookies` | `cookies.txt` neu laden ohne Bot-Neustart |
| `!listen on\|off` | Sprachsteuerung an/aus (setzt `VOICE_CONTROL=true` voraus) |
| `!debug on\|off` | Terminal-Ausgabe: volle Diagnose oder nur das Wesentliche (`bot.log` ist immer vollständig) |
| `!restart` | Bot neu starten (nur Owner) — Windows: neues Konsolenfenster, Linux: in-place |

---

## Projektstruktur

```
├── main.py                    # Einstiegspunkt
├── config.py                  # Env-Variablen laden
├── cogs/
│   ├── music.py               # Kern: Queue, EQ, Autoplay, !text, !format
│   ├── music_playback.py      # PlaybackMixin: play_next, FFmpeg-Quellen, Prefetch, Suche/Resolve
│   ├── music_voice_ui.py      # Join/Leave/Idle, Watchdog, Voice-Reparatur, Fortschrittsbalken
│   ├── music_radio.py         # RadioMixin: Internet-Radio (!radio)
│   ├── music_stats.py         # StatsMixin: !score, !stats
│   ├── music_queue_io.py      # QueuePersistenceMixin: !saveq, !loadq, !lists
│   ├── downloader.py          # yt_dlp-Instanzen, Cookie-Modus, Download-Cache, Streaming
│   ├── downloader_progressive.py # Progressiver Download (Wiedergabe startet schon während des Downloads)
│   ├── presets.py             # EQ-Filterketten
│   ├── basic.py               # !j, !l, !ping, !echo, !help, !debug, !restart
│   ├── dm_bridge.py           # HTTP-Audio-Ausgabe-Bridge für KI-Dungeon-Master (localhost-Pfad + Remote-Byte-Modus)
│   ├── voice_listen.py        # Sprachsteuerung (!listen)
│   └── voice_sink.py          # Audio-Empfang fürs eigene Zuhören (voice_recv)
├── views/
│   ├── music_controls.py      # Discord-Buttons (Pause, Resume, Skip, Autoplay, Loop)
│   ├── queue_view.py          # Paginierte Queue-Ansicht
│   └── help_view.py           # Paginierte !help-Ansicht
├── utils/                     # logger (Konsole + bot.log), files.safe_unlink, i18n.t, voice, text, ffmpeg, checks, …
├── tools/
│   └── logsummary.py          # Zeitleiste des letzten Laufs aus bot.log
├── locales/
│   ├── en.json                # Englische Texte
│   └── de.json                # Deutsche Texte
├── downloads/                 # Audio-Cache
├── playlists/                 # Gespeicherte Queues (!saveq / !loadq)
└── tests/                     # pytest-Suite
```

---

## Sprache

Die Bot-Sprache wird über die Variable `LANGUAGE` in `.env` gesetzt:

```env
LANGUAGE=en   # Englisch (Standard)
LANGUAGE=de   # Deutsch
```

---

## Setup

→ Siehe [`SETUP_DE.md`](SETUP_DE.md)
