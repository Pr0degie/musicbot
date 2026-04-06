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
- **`views/music_controls.py`** — `MusicControlView`: Pause/Resume/Skip/Autoplay buttons on now-playing messages. Only the current song's message keeps buttons — previous ones get `view=None` on next song start. Buttons send ephemeral feedback to the user.

### Music Playback Flow

1. `!p` extracts audio via `yt_dlp` in `asyncio.to_thread()` with a 30s timeout. Accepts YouTube URLs, playlists, or search terms (`ytsearch`).
2. Files cached in `downloads/%(id)s.%(ext)s` — reused if already downloaded.
3. `FFmpegOpusAudio` plays with FFmpeg filter chain per EQ preset.
4. `after` callback uses `run_coroutine_threadsafe()` to trigger next song. Errors skip to next track via `asyncio.create_task()` (not direct recursion — avoids stack overflow on many bad URLs).
5. `last_queue.json` is written after each track as a session log — intentionally not reloaded on startup.
6. Queue empty → bot stays in voice channel and waits for `!p`.

`current_track` is a 3-tuple `(url, title, duration_seconds)`. Queue stores 2-tuples `(url, title)`. When re-adding `current_track` to queue (loop mode, `!eq` restart, `!replay`), always unpack first: `url, title, *_ = self.current_track`.

### Queue

`collections.deque` in `MusicCommands`. `shuffle`/`remove`/`move` convert to list first. Max playlist size: 150.

### Audio Configuration

Default format: `webm`. Default EQ preset: `punchy`. Filter chains defined in `music.py`.
Presets: `bassboost`, `flat`, `vocalboost`, `superbass`, `punchy`, `nightcore`, `karaoke`, `8d`.
`!eq` mid-song: restarts the current track with the new filter (prepends to queue, calls stop).
Switch with `!format mp3|webm` or `!eq <preset>`. Format change reinitializes yt_dlp via `update_ydl()`, which also rebuilds `self.search_ydl`, `self.url_ydl`, and `self.playlist_ydl` — three cached instances used by `!p` to avoid per-call initialization overhead.

### Logging

Single setup in `utils/logger.py` — writes to both console and `bot.log`. `config.py` only loads `.env` and exports `TOKEN`; it does NOT call `logging.basicConfig()` (that would silently disable the file handler in `utils/logger.py`). `music.py` imports `logger` from `utils.logger`.

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
| `!genres` | List autoplay genres |
| `!genres add <genre>` | Add a genre to autoplay (max 20) |
| `!genres remove <genre>` | Remove a genre from autoplay |
| `!genres reset` | Restore default autoplay genres |
| `!stats` | Live metrics: RAM (RSS/VMS), CPU, uptime, asyncio tasks, songs played, cache size |
