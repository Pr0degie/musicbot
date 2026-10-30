# 🎵 Discord Music Bot

Modular Discord music bot powered by yt_dlp, FFmpeg, buttons and equalizer presets.  
Built with [discord.py](https://discordpy.readthedocs.io/en/stable/) and [yt_dlp](https://github.com/yt-dlp/yt-dlp).

---

## Features

- Play YouTube links, playlists and search terms
- Control buttons directly in Discord: ⏸️ ▶️ ⏭️ 🔁 🔂
- Autoplay: finds the next song automatically when queue runs out
- Equalizer presets: `bassboost`, `flat`, `vocalboost`, `superbass`, `punchy`, `nightcore`, `karaoke`, `8d`
- Audio format: `webm` (default, lossless) or `mp3`
- Internet radio via direct stream
- Queue management with save/load, shuffle and loop mode
- Short songs are cached; long songs (>20 min) start instantly via stream
- Auto-leave: bot leaves voice when all users go, or after 2 h of inactivity
- Voice stability: reconnect watchdog recovers playback after Discord drops (code 1006); after a network outage the bot rebuilds the voice connection itself and posts a note in chat
- Mute guard: unmutes itself when server-muted (needs *Mute Members*), pauses instead if it can't
- Move guard (optional): only the owner may move the bot to another channel, anyone else's move gets undone
- Voice control ("yo bot, play …") — optional, off by default (`VOICE_CONTROL`, `!listen`)

---

## Commands (prefix: `!`)

**Playback**

| Command | Description |
|---|---|
| `!p <url/search>` | Play music — YouTube URL, playlist or title search |
| `!next <url/search>` | Queue a song as next |
| `!s` | Skip |
| `!x` | Pause |
| `!resume` | Resume |
| `!now <url/search/n>` | Play immediately, skip current song — also accepts queue position `n` |
| `!seek <time>` | Jump to position — e.g. `1:23` or `83` |
| `!replay` | Re-queue last song |

**Queue**

| Command | Description |
|---|---|
| `!q` | Show queue (paginated) |
| `!shuffle` | Shuffle queue |
| `!clear` | Stop + clear queue |
| `!remove <n>` | Remove song at position `n` |
| `!move <n\|title>` | Move song to front — by position or title search |
| `!loop` | Loop mode: off → song → queue → off |
| `!saveq <name>` | Save current queue |
| `!loadq <name>` | Load saved queue |
| `!loadq last` | Load the queue of the last session |
| `!lists` | List all saved queues |

**Audio & Radio**

| Command | Description |
|---|---|
| `!eq <preset>` | Set EQ preset (no argument: list all) |
| `!format <type>` | Switch audio format: `mp3` or `webm` |
| `!radio` | Show station list |
| `!radio <nr/name>` | Play station |
| `!radio <url> [name]` | Play & save stream |
| `!radio delete <nr/name>` | Delete station |
| `!radio rename <nr/name> <new name>` | Rename station |
| `!stop` | Stop playback/radio (queue is preserved) |

**Misc**

| Command | Description |
|---|---|
| `!score` | Top 10 most played songs |
| `!baba` | Play Baba playlist |
| `!text` | Fetch song lyrics |
| `!stats` | Bot stats (RAM, CPU, cache) |
| `!help` / `!h` | Command overview (paginated) |
| `!j` / `!l` | Join / leave voice channel |
| `!dm` | Show DM-bridge status (AI dungeon master audio output) |
| `!reloadcookies` | Reload `cookies.txt` without restarting |
| `!listen on\|off` | Voice control on/off (requires `VOICE_CONTROL=true`) |
| `!debug on\|off` | Terminal output: full diagnostics or essentials only (`bot.log` is always complete) |
| `!restart` | Restart bot (owner only) — new console window on Windows, in-place on Linux |

---

## Project Structure

```
├── main.py                    # Entry point
├── config.py                  # Load environment variables
├── cogs/
│   ├── music.py               # Core: queue, EQ, autoplay, !text, !format
│   ├── music_playback.py      # PlaybackMixin: play_next, FFmpeg sources, prefetch, search/resolve
│   ├── music_voice_ui.py      # Join/leave/idle, watchdog, voice repair, progress bar
│   ├── music_radio.py         # RadioMixin: internet radio (!radio)
│   ├── music_stats.py         # StatsMixin: !score, !stats
│   ├── music_queue_io.py      # QueuePersistenceMixin: !saveq, !loadq, !lists
│   ├── downloader.py          # yt_dlp instances, cookie mode, download cache, streaming
│   ├── downloader_progressive.py # Progressive download (playback starts while downloading)
│   ├── presets.py             # EQ filter chains
│   ├── basic.py               # !j, !l, !ping, !echo, !help, !debug, !restart
│   ├── dm_bridge.py           # HTTP audio-output bridge for AI dungeon master (localhost path + remote byte mode)
│   ├── voice_listen.py        # Voice control (!listen)
│   └── voice_sink.py          # Audio receive for own listening (voice_recv)
├── views/
│   ├── music_controls.py      # Discord buttons (Pause, Resume, Skip, Autoplay, Loop)
│   ├── queue_view.py          # Paginated queue view
│   └── help_view.py           # Paginated !help
├── utils/                     # logger (console + bot.log), files.safe_unlink, i18n.t, voice, text, ffmpeg, checks, …
├── tools/
│   └── logsummary.py          # Timeline of the last run from bot.log
├── locales/
│   ├── en.json                # English strings
│   └── de.json                # German strings
├── downloads/                 # Audio cache
├── playlists/                 # Saved queues (!saveq / !loadq)
└── tests/                     # pytest suite
```

---

## Language

Set via `LANGUAGE` in `.env`:

```env
LANGUAGE=en   # English (default)
LANGUAGE=de   # German
```

---

## Setup

→ See [`SETUP.md`](SETUP.md)
