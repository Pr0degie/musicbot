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
- **`views/music_controls.py`** — `MusicControlView`: Pause/Resume/Skip/Autoplay buttons on now-playing messages. Only the current song's message keeps buttons — previous ones get `view=None` on next song start. Buttons send ephemeral feedback to the user. **Resume button** has three cases: (1) paused → resume, (2) not playing but queue has songs → `play_next`, (3) not playing, queue empty, autoplay on → `autoplay()`. `SearchAutoplayView`: shown after `!p` and `!next` searches — first result plays/queues immediately, alternatives appear as buttons (timeout 30 s). Accepts optional `base_content` for the timeout fallback text.

### Music Playback Flow

1. `!p` extracts audio via `yt_dlp` in `asyncio.to_thread()` with a 30s timeout. Accepts YouTube URLs, playlists, or search terms (`ytsearch`).
2. Files cached in `downloads/%(title)s.%(ext)s` — reused if already downloaded.
3. `FFmpegOpusAudio` plays with FFmpeg filter chain per EQ preset.
4. `after` callback uses `run_coroutine_threadsafe()` to always call `play_next`. Errors skip to next track via `asyncio.create_task()` (not direct recursion — avoids stack overflow on many bad URLs).
5. `last_queue.json` is written after each track as a session log — intentionally not reloaded on startup.
6. Queue empty → `play_next` sets `is_playing = False`, saves `current_track` into `last_played`, clears `current_track`, then triggers autoplay if enabled. Bot stays in voice channel.

`current_track` is a 3-tuple `(url, title, duration_seconds)`. Queue stores 2-tuples `(url, title)`. When re-adding `current_track` to queue (loop mode, `!eq` restart, `!replay`), always unpack first: `url, title, *_ = self.current_track`.

Background tasks on `MusicCommands`: `prefetch_task` (downloads next queued song while current plays), `_autoplay_prefetch_task` (searches + downloads next autoplay song while current plays — started when queue is empty and autoplay is on).

### Queue

`collections.deque` in `MusicCommands`. `shuffle`/`remove`/`move` convert to list first. Max playlist size: 150.

### Audio Configuration

Default format: `webm`. Default EQ preset: `punchy`. Filter chains defined in `cogs/presets.py` (`EQ_PRESETS`), imported into `MusicCommands` as `self.eq_presets`.
Presets: `bassboost`, `flat`, `vocalboost`, `superbass`, `punchy`, `nightcore`, `karaoke`, `8d`.
`!eq` mid-song: restarts the current track with the new filter (prepends to queue, calls stop).
Switch with `!format mp3|webm` or `!eq <preset>`. Format change reinitializes yt_dlp via `update_ydl()`, which rebuilds all cached ydl instances:

| Instance | Purpose |
|---|---|
| `ydl` | Main download instance |
| `search_ydl` | `extract_flat=True`, for `ytsearch` queries |
| `url_ydl` | Single-video URL resolution, `noplaylist=True` |
| `playlist_ydl` | Playlist extraction, `extract_flat="in_playlist"` |
| `autoplay_ydl` | YouTube Mix metadata only — `extract_flat=True`, `playlistend=6`, no download |

FFmpeg filter notes and the "do not add" list are documented as comments at the top of `cogs/presets.py`.

**yt_dlp** prefers highest-quality Opus/webm — see `update_ydl()` in `music.py` for selector details.

### YouTube Authentication (Cookies)

Cookie config is read from `.env` via `update_ydl()` and applied to all five ydl instances. `cookiefile` takes priority over `cookiesfrombrowser`. Setup and renewal instructions → `SETUP.md`.

### Logging

Single setup in `utils/logger.py` — writes to both console and `bot.log`. `config.py` only loads `.env` and exports `TOKEN`; it does NOT call `logging.basicConfig()` (that would silently disable the file handler in `utils/logger.py`). `music.py` imports `logger` from `utils.logger`.

### Autoplay

Toggled via the `🔁 Autoplay` button in `MusicControlView`. When enabled:

1. **During playback** — `_prefetch_autoplay(ctx)` starts as a background task the moment a song begins, but only if the queue is currently empty. It fetches the YouTube Mix for the current video (`list=RD{video_id}`) via `autoplay_ydl` (first 6 entries, flat metadata), picks the first non-duplicate video, and downloads it fully via `ydl` while the song is still playing.
2. **On song end** — `play_next` checks if `_autoplay_prefetch_task` is still running and waits up to 60 s. If the prefetch succeeded the file is already on disk and plays with no delay. If it timed out or failed, falls back to `autoplay()` (same logic, no pre-download).
3. **`autoplay()`** (fallback) — same YouTube Mix lookup but runs after song ends; adds URL to queue and calls `play_next`.
4. **`▶️ Fortsetzen` button** — if nothing is paused but queue has songs: starts `play_next`. If queue is empty and autoplay is on: triggers `autoplay()` directly.

Reference track priority: `current_track` → `last_played`. `last_played` is updated in the queue-empty branch of `play_next` (before `current_track` is cleared) so it's always available when autoplay runs. Autoplay stays enabled until the button is pressed again — no one-shot behaviour.

### Key Bot Commands

See `!help` (implemented in `cogs/basic.py`) for the full command list. Architectural notes on specific commands:

- **`!p` / `!next`** — yt-dlp extraction in `asyncio.to_thread()`, 30 s timeout; search shows `SearchAutoplayView` with alternatives
- **`!loop`** — cycles `loop_mode`: `None` → `"song"` → `"queue"` → `None`; handled in `after_playing`
- **`!eq`** — mid-song restart: prepends current track to queue, calls stop
- **`!reloadcookies`** — calls `update_ydl()` to pick up a newly uploaded `cookies.txt` without restart
- **`!text`** — lyrics via lyrics.ovh, parses "Artist - Title" from YouTube title
