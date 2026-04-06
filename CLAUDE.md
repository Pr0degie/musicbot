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
- **`views/music_controls.py`** — `MusicControlView`: Pause/Resume/Skip/Autoplay buttons on now-playing messages. Only the current song's message keeps buttons — previous ones get `view=None` on next song start. Buttons send ephemeral feedback to the user. `SearchAutoplayView`: shown after `!p` and `!next` searches — first result plays/queues immediately, alternatives appear as buttons (timeout 30 s). Accepts optional `base_content` for the timeout fallback text.

### Music Playback Flow

1. `!p` extracts audio via `yt_dlp` in `asyncio.to_thread()` with a 30s timeout. Accepts YouTube URLs, playlists, or search terms (`ytsearch`).
2. Files cached in `downloads/%(title)s.%(ext)s` — reused if already downloaded.
3. `FFmpegOpusAudio` plays with FFmpeg filter chain per EQ preset.
4. `after` callback uses `run_coroutine_threadsafe()` to trigger next song. Errors skip to next track via `asyncio.create_task()` (not direct recursion — avoids stack overflow on many bad URLs).
5. `last_queue.json` is written after each track as a session log — intentionally not reloaded on startup.
6. Queue empty → `after_playing` sets `is_playing = False` and bot stays in voice channel waiting for `!p`. **Important:** `is_playing` must be reset to `False` in the queue-empty branch of `after_playing` — otherwise subsequent `!p` calls add to queue but never trigger `play_next`.

`current_track` is a 3-tuple `(url, title, duration_seconds)`. Queue stores 2-tuples `(url, title)`. When re-adding `current_track` to queue (loop mode, `!eq` restart, `!replay`), always unpack first: `url, title, *_ = self.current_track`.

### Queue

`collections.deque` in `MusicCommands`. `shuffle`/`remove`/`move` convert to list first. Max playlist size: 150.

### Audio Configuration

Default format: `webm`. Default EQ preset: `punchy`. Filter chains defined in `cogs/presets.py` (`EQ_PRESETS`), imported into `MusicCommands` as `self.eq_presets`.
Presets: `bassboost`, `flat`, `vocalboost`, `superbass`, `punchy`, `nightcore`, `karaoke`, `8d`.
`!eq` mid-song: restarts the current track with the new filter (prepends to queue, calls stop).
Switch with `!format mp3|webm` or `!eq <preset>`. Format change reinitializes yt_dlp via `update_ydl()`, which also rebuilds `self.search_ydl`, `self.url_ydl`, and `self.playlist_ydl` — three cached instances used by `!p` to avoid per-call initialization overhead.

**FFmpeg filter notes:** All active presets start with `asetpts=N/SR/TB` and end with `aresample=48000` — see code for full chains. `asetpts=N/SR/TB` normalizes YouTube stream timestamps at the input so filters never receive gaps or jumps; do not remove (removing it causes audible speed artifacts on certain songs). Do **not** add `resampler=soxr`, `aformat=sample_fmts=fltp`, or `stereotools` — crash on this FFmpeg build. Short-lived track (< 2 s) logs a warning with active preset. **`!vol` intentionally absent.**

**yt_dlp** prefers highest-quality Opus/webm — see `update_ydl()` in `music.py` for selector details.

### YouTube Authentication (Cookies)

YouTube bot-detection requires cookie auth. Configured via `.env`:

| Variable | Purpose |
|---|---|
| `YDL_COOKIES_FILE` | Path to an exported `cookies.txt` (takes priority) |
| `YDL_BROWSER` | Browser to extract cookies from live (`firefox`, `chrome`, …) — local use only, won't work on headless servers |

`cookiefile` takes priority over `cookiesfrombrowser` when both are set. All three ydl instances (`ydl`, `search_ydl`, `url_ydl`/`playlist_ydl`) receive the same cookie config.

**EJS challenge solver:** yt-dlp requires `yt-dlp[default]` (installs `yt-dlp-ejs`) and Node.js in PATH to solve YouTube's signature challenges. `js_runtimes: {node: {}}` must be set in ydl opts — yt-dlp defaults to Deno only.

**Cookie renewal:** Cookies last ~1–3 months. Export with `yt-dlp --cookies-from-browser firefox --cookies cookies.txt --skip-download <any-yt-url>`, upload via SCP, then run `!reloadcookies` in Discord (no bot restart needed).

### Logging

Single setup in `utils/logger.py` — writes to both console and `bot.log`. `config.py` only loads `.env` and exports `TOKEN`; it does NOT call `logging.basicConfig()` (that would silently disable the file handler in `utils/logger.py`). `music.py` imports `logger` from `utils.logger`.

### Key Bot Commands

See `!help` (implemented in `cogs/basic.py`) for the full command list. Architectural notes on specific commands:

- **`!p` / `!next`** — yt-dlp extraction in `asyncio.to_thread()`, 30 s timeout; search shows `SearchAutoplayView` with alternatives
- **`!eq`** — mid-song restart: prepends current track to queue, calls stop
- **`!reloadcookies`** — calls `update_ydl()` to pick up a newly uploaded `cookies.txt` without restart
- **`!text`** — lyrics via lyrics.ovh, parses "Artist - Title" from YouTube title
