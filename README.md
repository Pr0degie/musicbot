# 🎵 Discord Music Bot

Modular Discord music bot powered by yt_dlp, FFmpeg, buttons and equalizer presets.  
Built with [discord.py](https://discordpy.readthedocs.io/en/stable/) and [yt_dlp](https://github.com/yt-dlp/yt-dlp).

---

## Features

- Play YouTube links, playlists and search terms
- Control buttons directly in Discord: ⏸️ ▶️ ⏭️ 🔁
- Autoplay: finds the next song automatically when queue runs out
- Equalizer presets: `bassboost`, `flat`, `vocalboost`, `superbass`, `punchy`, `nightcore`, `karaoke`, `8d`
- Audio format: `webm` (default, lossless) or `mp3`
- Internet radio via direct stream
- Queue management with save/load, shuffle and loop mode
- Short songs are cached; long songs (>20 min) start instantly via stream

---

## Commands (prefix: `!`)

**Playback**

| Command | Description |
|---|---|
| `!p <url/search>` | Play music — YouTube URL, playlist or titel search  |
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
| `!j` / `!l` | Join / leave voice channel |
| `!reloadcookies` | Reload `cookies.txt` without restarting |
| `!restart` | Restart bot in new terminal (owner only) |

---

## Project Structure

```
├── main.py                    # Entry point
├── config.py                  # Load environment variables
├── cogs/
│   ├── music.py               # Music logic, queue, autoplay, radio
│   ├── downloader.py          # yt_dlp instances, download cache, streaming
│   ├── presets.py             # EQ filter chains
│   └── basic.py               # !j, !l, !ping, !echo, !help
├── views/
│   └── music_controls.py      # Discord buttons (Pause, Resume, Skip, Autoplay)
├── utils/
│   ├── logger.py              # Logging (console + bot.log)
│   └── i18n.py                # Localization helper (t() function)
├── locales/
│   ├── en.json                # English strings
│   └── de.json                # German strings
├── downloads/                 # Audio cache
└── playlists/                 # Saved queues (!saveq / !loadq)
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
