# CLAUDE.md

Setup → see `README.md`. No test suite or linter configured.

## Architecture

Two cogs loaded at startup, all responses in German:

- **`cogs/basic.py`** — `BasicCommands`: `!j`, `!l`, `!ping`, `!echo`
- **`cogs/music.py`** — `MusicCommands`: queue, yt_dlp extraction, FFmpeg playback, EQ presets, autoplay
- **`views/music_controls.py`** — `MusicControlView`: Pause/Resume/Skip/Autoplay buttons. Only current song keeps buttons — previous get `view=None`. Resume: (1) paused → resume, (2) queue has songs → `play_next`, (3) autoplay on → `autoplay()`. `SearchAutoplayView`: first search result plays immediately, alternatives as buttons (30 s timeout).

### Music Playback Flow

1. `!p` extracts audio via `yt_dlp` in `asyncio.to_thread()` with a 30s timeout. Accepts YouTube URLs, playlists, or search terms (`ytsearch`).
2. Files cached in `downloads/%(title)s.%(ext)s` — reused if already downloaded.
3. `FFmpegOpusAudio` plays with FFmpeg filter chain per EQ preset.
4. `after` callback uses `run_coroutine_threadsafe()` to always call `play_next`. Errors skip to next track via `asyncio.create_task()` (not direct recursion — avoids stack overflow on many bad URLs).
5. `last_queue.json` is written after each track as a session log — intentionally not reloaded on startup.
6. Queue empty → `play_next` sets `is_playing = False`, saves `current_track` into `last_played`, clears `current_track`, then triggers autoplay if enabled. Bot stays in voice channel.

`current_track` is a 3-tuple `(url, title, duration_seconds)`. Queue stores 2-tuples `(url, title)`. When re-adding `current_track` to queue (loop mode, `!eq` restart, `!replay`), always unpack first: `url, title, *_ = self.current_track`.

Background tasks on `MusicCommands`: `prefetch_task` (downloads the next **two** queued songs in parallel while the current plays — `asyncio.gather(_prefetch_next(0), _prefetch_next(1))`), `_autoplay_prefetch_task` (searches + downloads next autoplay song while current plays — started when queue is empty and autoplay is on).

`_url_cache` is a `dict` mapping URL → yt_dlp info-dict (`download=False`). Populated by `_prefetch_next()` and `_resolve_track()`; prevents a second `extract_info` call when the prefetch already fetched the metadata. Cleared by `update_ydl()` (every 50 songs) and `clear()`.

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

`_autoplay_queued_url` tracks the URL that was most recently added to the queue by autoplay (either via `_prefetch_autoplay` or `autoplay()`). It is cleared when that entry is popped by `play_next` or evicted by `_evict_autoplay_song()`.

`_recently_played` is a `deque(maxlen=10)` of URLs. Each track appended to `current_track` in `play_next` gets added here. Autoplay candidate selection filters against this list first to prevent recent songs from repeating; falls back to filtering only `ref_url` if all candidates are in history.

**`!p` with autoplay active** — `_evict_autoplay_song()` is called before adding the new song. It cancels any running `_prefetch_autoplay` task, removes the tracked autoplay URL from the queue, and the new song is inserted at the front (`appendleft`) so it plays next. Playlist additions also evict but append at the end as usual.

### Key Bot Commands

Full list via `!help`. Non-obvious internals:

- **`!loop`** — cycles `loop_mode`: `None` → `"song"` → `"queue"` → `None`; handled in `after_playing`
- **`!text`** — lyrics via lyrics.ovh, parses "Artist - Title" from YouTube title
