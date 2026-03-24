# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup & Running

All working files are under `Musicbot/`. The entry point is `Musicbot/main.py`.

**Install dependencies:**
```bash
cd Musicbot
python -m venv venv
source venv/Scripts/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
pip install 'discord.py[voice]' -U
```

**Configure:** Add `DISCORD_TOKEN=your_token` to `Musicbot/.env`.

**Run:**
```bash
cd Musicbot && python main.py
```

No test suite or linter is configured.

## Architecture

The bot uses `discord.ext.commands` with two cogs loaded at startup:

- **`cogs/basic.py`** — `BasicCommands`: Join/leave voice channel (`!j`, `!l`), `!ping`, `!echo`
- **`cogs/music.py`** — `MusicCommands`: All playback logic — queue management, yt_dlp extraction, FFmpeg playback, equalizer presets, autoplay
- **`views/music_controls.py`** — `MusicControlView`: Discord UI buttons (Pause, Resume, Skip, Autoplay) attached to now-playing messages. `timeout=None` means buttons never expire.

**Note:** All bot response messages are in German.

### Music Playback Flow

1. `!p <url or search term>` extracts audio via `yt_dlp` using `asyncio.to_thread()` (non-blocking). Accepts YouTube URLs, playlist URLs, or plain search terms (uses `ytsearch`).
2. Audio file saved to `downloads/%(id)s.%(ext)s`. Files are cached by video ID and reused on subsequent plays — no re-download if the file already exists.
3. `discord.FFmpegPCMAudio` plays the file with an FFmpeg filter chain (equalizer preset)
4. On track end, the `after` callback uses `asyncio.run_coroutine_threadsafe()` to trigger the next song
5. Queue state is persisted to `last_queue.json` (in the working directory) after each track. The queue intentionally starts empty on each startup — `last_queue.json` is a log of the last session's queue, not a restore point.
6. When the queue empties, the bot **stays in the voice channel** and waits for new `!p` commands.

### Queue

Stored as a `collections.deque` inside `MusicCommands`. `shuffle` and `remove` convert to list first (deque doesn't support those operations directly). Max playlist size: 150 songs.

### Audio Configuration

Default audio format: `webm`. Default EQ preset: `bassboost`.

FFmpeg filter chains per preset (in `music.py`):
- `bassboost`: `bass=g=12,dynaudnorm=f=200,volume=0.85,aresample=48000`
- `flat`: no filter
- `vocalboost`: `equalizer=f=1000:width_type=o:width=2:g=5`
- `superbass`: `bass=g=20`

Switch format with `!format mp3|webm`, switch preset with `!eq <preset>`. Changing format reinitializes `yt_dlp` options via `update_ydl()`.

### Logging

There are two logger setups: `config.py` (basic) and `utils/logger.py` (file + stream handler writing to `bot.log`). `music.py` imports `logger` from `config`, not from `utils/logger.py`.

### Key Bot Commands

| Command | Description |
|---|---|
| `!p <url or query>` | Play from YouTube URL, playlist URL, or search term (max 150 for playlists) |
| `!s` | Skip current track |
| `!x` / `!resume` | Pause / resume |
| `!q` | Show queue |
| `!shuffle` | Shuffle queue |
| `!clear` | Stop and clear queue |
| `!remove <n>` | Remove track at position n |
| `!replay` | Re-queue last played song |
| `!eq <preset>` | Set equalizer preset (or omit to list presets) |
| `!format <type>` | Switch audio format (mp3 or webm) |
