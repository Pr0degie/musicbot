# CLAUDE.md

Setup → see `README.md`. No test suite or linter configured.

## Architecture

Two cogs loaded at startup, all responses in German:

- **`cogs/basic.py`** — `BasicCommands`: `!j`, `!l`, `!ping`, `!echo`
- **`cogs/music.py`** — `MusicCommands`: queue, FFmpeg playback, EQ presets, autoplay; delegates all yt_dlp work to `self.dl`
- **`cogs/downloader.py`** — `Downloader` (`self.dl`): five yt_dlp instances (`ydl`, `search_ydl`, `url_ydl`, `playlist_ydl`, `autoplay_ydl` — all via `self.dl.*`), `_url_cache`, `resolve_track()`, `prefetch_next()`, `prefetch_autoplay()`. Also defines `DOWNLOAD_DIR`.
- **`views/music_controls.py`** — `MusicControlView`: Pause/Resume/Skip/Autoplay buttons. Only current song keeps buttons — previous get `view=None`. Resume: (1) paused → resume, (2) queue has songs → `play_next`, (3) autoplay on → `autoplay()`. `SearchAutoplayView`: first search result plays immediately, alternatives as buttons (30 s timeout).

### Music Playback Flow

1. `after` callback uses `run_coroutine_threadsafe()` to call `play_next`. Errors skip via `asyncio.create_task()` — not direct recursion (avoids stack overflow on many bad URLs).
2. `last_queue.json` written after each track — intentionally not reloaded on startup.
3. Queue empty → `play_next` sets `is_playing = False`, saves `current_track` → `last_played`, clears `current_track`, triggers autoplay if enabled. Bot stays in voice channel.

`current_track` is a 3-tuple `(url, title, duration_seconds)`. Queue stores 2-tuples `(url, title)`. When re-adding `current_track` to queue (loop mode, `!eq` restart, `!replay`), always unpack: `url, title, *_ = self.current_track`.

Background tasks: `prefetch_task` downloads the next two queued songs sequentially (`_prefetch_next(0)` then `_prefetch_next(1)` — sequential because yt_dlp is not thread-safe); `_autoplay_prefetch_task` searches + downloads next autoplay song while current plays.

`_url_cache` on `self.dl`: URL → yt_dlp info-dict. All three callers cache the **full** info-dict (with `ext`, `webpage_url`) so `prepare_filename()` works. `autoplay_ydl` yields shallow playlist entries; `prefetch_autoplay()` upgrades via `ydl.extract_info(url, download=True)`. `update_ydl()` keeps only entries still in queue/`current_track`; `clear()` wipes entirely.

### Audio Configuration

Default format: `webm`. Default EQ preset: `punchy`. Filter chains in `cogs/presets.py` (`EQ_PRESETS`).
Presets: `bassboost`, `flat`, `vocalboost`, `superbass`, `punchy`, `nightcore`, `karaoke`, `8d`.
`!eq` mid-song: restarts current track with new filter (prepends to queue, calls stop).
`!format mp3|webm` or `!eq <preset>` → `update_ydl()` → `self.dl.rebuild()` recreates all five instances.
FFmpeg filter notes and "do not add" list → comments at top of `cogs/presets.py`.

### Streaming vs. Download

`STREAM_THRESHOLD_SECONDS = 20 * 60` (in `downloader.py`). `resolve_track()` returns a direct CDN URL (`str`, not `Path`) in two cases — **this is intentional, not a bug**:
- Duration > 20 min → always stream, never download.
- File not locally cached → stream immediately; `prefetch_next()` downloads queue songs in background.

`play_next` detects streams via `isinstance(filename, str)` → adds FFmpeg reconnect options, skips `codec=copy`.
`prefetch_next()` skips download for videos > 20 min.

### YouTube Authentication (Cookies)

Cookie config read from `.env` via `update_ydl()`. `cookiefile` takes priority over `cookiesfrombrowser`. Details → `SETUP.md`.

### Logging

`utils/logger.py` — console + `bot.log`. `config.py` must NOT call `logging.basicConfig()` — silently disables the file handler. Import `logger` from `utils.logger`.

### Autoplay

Toggled via `🔁 Autoplay` button. `_prefetch_autoplay` starts at song-begin (only if queue empty): fetches YouTube Mix (`list=RD{video_id}`) via `autoplay_ydl`, downloads best candidate, appends to queue. On song end `play_next` waits up to 60 s for the prefetch task; falls back to `autoplay()` (same lookup, no pre-download) if needed.

Reference track: `current_track` → `last_played`. Autoplay stays on until button pressed again — not one-shot.

`_autoplay_queued_url`: URL last added by autoplay; cleared when popped by `play_next` or evicted by `_evict_autoplay_song()`.

`_recently_played`: `deque(maxlen=15)` of URLs. `_recently_played_titles`: `deque(maxlen=15)` of normalized titles (via `normalize_title()` from `downloader.py` — strips suffixes like "(Official Video)", special chars, lowercase, sorts words alphabetically — so "AHA Take on Me" and "Take on Me AHA" map to the same key). Autoplay filters candidates against both; falls back to filtering only `ref_url` if all candidates are in history.

**`!p` with autoplay active** — `_evict_autoplay_song()` cancels prefetch, removes autoplay URL from queue, inserts new song at front (`appendleft`). Playlist additions evict but append at end.

### Radio

`RADIO_STATIONS_FILE` = `radio_stations.json` (key → `{name, url}`).
State: `is_radio`, `radio_station_name`, `radio_stream_url`, `_radio_reconnect_count`.
`_play_radio_stream()` plays via FFmpeg directly (no yt_dlp); `after_radio` reconnects up to 3× on error.
`!radio <Nr|Name>` → aus Liste. `!radio <url> [Name]` → spielt + speichert automatisch (kein Duplikat).
`!stop` beendet Radio. Radio-Modus und Song-Modus schließen sich gegenseitig aus.

### Key Bot Commands

Full list via `!help`. Non-obvious:
- **`!loop`** — cycles `loop_mode`: `None` → `"song"` → `"queue"` → `None`; handled in `after_playing`
- **`!text`** — lyrics via lyrics.ovh, parses "Artist - Title" from YouTube title
