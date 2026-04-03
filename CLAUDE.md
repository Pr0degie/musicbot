# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup & Running

Entry point: `Musicbot/main.py`. FFmpeg must be in PATH.

```bash
cd Musicbot
python -m venv venv && source venv/Scripts/activate
pip install -r requirements.txt
```

Add `DISCORD_TOKEN=your_token` to `Musicbot/.env`, then `python main.py`.

No test suite or linter configured.

## Architecture

Two cogs loaded at startup, all responses in German:

- **`cogs/basic.py`** — `BasicCommands`: `!j`, `!l`, `!ping`, `!echo`
- **`cogs/music.py`** — `MusicCommands`: queue, yt_dlp extraction, FFmpeg playback, EQ presets, autoplay
- **`views/music_controls.py`** — `MusicControlView`: Pause/Resume/Skip/Autoplay buttons on now-playing messages. Only the current song's message keeps buttons — previous ones get `view=None` on next song start.

### Music Playback Flow

1. `!p` extracts audio via `yt_dlp` in `asyncio.to_thread()`. Accepts YouTube URLs, playlists, or search terms (`ytsearch`).
2. Files cached in `downloads/%(id)s.%(ext)s` — reused if already downloaded.
3. `FFmpegPCMAudio` plays with FFmpeg filter chain per EQ preset.
4. `after` callback uses `run_coroutine_threadsafe()` to trigger next song.
5. `last_queue.json` is written after each track as a session log — not reloaded on startup.
6. Queue empty → bot stays in voice channel and waits for `!p`.

### Queue

`collections.deque` in `MusicCommands`. `shuffle`/`remove`/`move` convert to list first. Max playlist size: 150.

### Audio Configuration

Default format: `webm`. Default EQ preset: `punchy`. Filter chains defined in `music.py`.
Presets: `bassboost`, `flat`, `vocalboost`, `superbass`, `punchy`, `nightcore`, `karaoke`, `8d`.
`!eq` mid-song: restarts the current track with the new filter (prepends to queue, calls stop).
Switch with `!format mp3|webm` or `!eq <preset>`. Format change reinitializes yt_dlp via `update_ydl()`.

### Logging

Two setups: `config.py` (basic) and `utils/logger.py` (file+stream → `bot.log`). `music.py` uses `config`, not `utils/logger.py`.

### Key Bot Commands

| Command | Description |
|---|---|
| `!p <url or query>` | Play from YouTube URL, playlist, or search (max 150 for playlists) |
| `!s` | Skip current track |
| `!x` / `!resume` | Pause / resume |
| `!q` | Show queue |
| `!move <n>` | Jump to position n in queue and play from there |
| `!shuffle` | Shuffle queue |
| `!clear` | Stop and clear queue |
| `!remove <n>` | Remove track at position n |
| `!replay` | Re-queue last played song |
| `!eq <preset>` | Set equalizer preset (omit to list presets) |
| `!format <type>` | Switch audio format (mp3 or webm) |
