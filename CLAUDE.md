# CLAUDE.md

Setup → see `README.md`. Tests: `pytest` (suite in `tests/`, dev deps in `requirements-dev.txt`). Lint: `ruff check .` (nur E/F/W, line-length 140, Config in `pyproject.toml` — bewusst keine Stilregeln).

## Architecture

Four cogs loaded at startup, all responses in German:

- **`cogs/basic.py`** — `BasicCommands`: `!j`, `!l`, `!ping`, `!echo`
- **`cogs/music.py`** — `MusicCommands`: queue, FFmpeg playback, EQ presets, autoplay; delegates all yt_dlp work to `self.dl`. Der gemeinsame Such-/URL-Flow von `!p`/`!next`/`!now` steckt in vier Helpern: `_stop_radio_for_takeover` (Radio-Vorspann), `_extract_info_or_report` (yt_dlp-Fetch + Fehlermeldung, Sentinel `_YTDLP_FAILED`), `_search_and_enqueue` (ytsearch3-Zweig; Insert-Politik/Eviction/i18n-Keys als Parameter — Semantik-Unterschiede der drei Befehle stehen in der Docstring), `_fetch_single_track_info` (URL-Zweig von `!next`/`!now`; `!p` behält seinen eigenen wegen Playlist-Support + Duplikat-Warnung). Radio/stats/queue-persistence commands are split into mixins — same instance state, registered as cog commands via the MRO:
  - **`cogs/music_radio.py`** — `RadioMixin`: internet-radio streaming (`_play_radio_stream`, reconnect, `!radio`), `RADIO_STATIONS_FILE`
  - **`cogs/music_stats.py`** — `StatsMixin`: `!score` (play counts), `!stats` (process metrics)
  - **`cogs/music_queue_io.py`** — `QueuePersistenceMixin`: `!saveq`/`!loadq`/`!lists`, `PLAYLISTS_DIR`
- **`cogs/downloader.py`** — `Downloader` (`self.dl`): five yt_dlp instances (`ydl`, `search_ydl`, `url_ydl`, `playlist_ydl`, `autoplay_ydl` — all via `self.dl.*`), `_url_cache`, `resolve_track()`, `prefetch_next()`, `prefetch_autoplay()`. Also defines `DOWNLOAD_DIR`.
- **`cogs/dm_bridge.py`** — `DMBridge`: aiohttp server (`DM_BRIDGE_HOST`/`DM_BRIDGE_PORT`/`DM_BRIDGE_SECRET` in `config.py`) so a separate "Bot B" (AI dungeon master) can make this bot speak. Default `127.0.0.1` + empty secret = classic localhost path mode; set host to a LAN/Tailscale IP + a shared `DM_BRIDGE_SECRET` for remote mode. `GET /health`, `POST /speak` in **two transport modes**: (1) JSON `{"path", "guild_id"?}` → path mode (shared disk, localhost); (2) body with `Content-Type: audio/wav` (or `application/octet-stream`) + optional `X-DM-Guild-Id` header → byte mode (WAV bytes written to a `tempfile`, auto-deleted after playback). Non-loopback requests must pass the secret via HMAC (`hmac.compare_digest`). Both modes play via `FFmpegOpusAudio` (same pattern as `_play_radio_stream`). `/speak` is **blocking** — the HTTP response is the only done-signal; `_speak_lock` serializes calls, stops running music first. The `bot.dm_speaking` flag tells the music cog it doesn't own the voice client right now: `play_next()` checks it and exits early so the `after_playing` callback can't restart music mid-DM-speech. `!dm` shows status. No callback back to Bot B by design — feedback-loop protection lives entirely in Bot B.
- **`views/music_controls.py`** — `MusicControlView`: Pause/Resume/Skip/Autoplay buttons. Only current song keeps buttons — previous get `view=None`. Resume: (1) paused → resume, (2) queue has songs → `play_next`, (3) autoplay on → `autoplay()`. `SearchAutoplayView`: first search result plays immediately, alternatives as buttons (30 s timeout). **Stale-Buttons-Fallback:** Buttons tragen `custom_id = musicctl:{BOOT_ID}:{nonce}:{action}` (BOOT_ID = pro Prozessstart, nonce = pro Nachricht). `StaleControlsFallback` (DynamicItem, registriert in `setup_hook`) beantwortet Klicks auf Now-Playing-Nachrichten aus früheren Läufen mit ephemerer Erklärung + entfernt die toten Buttons. Wichtig: discord.py dispatcht Dynamic Items **zusätzlich** zu Live-Views (nicht nur als Fallback) — die Abgrenzung kommt allein aus dem Template-Regex (negative lookahead auf die aktuelle BOOT_ID), nie die Reihenfolge ändern. Nachrichten von vor diesem Feature (32-Hex-Auto-IDs) matchen nichts → weiterhin "Interaktion fehlgeschlagen", einmalig bis zum ersten Neustart danach.
- **`views/queue_view.py`** — `QueueView`: paginated queue embed with Prev/Next buttons; receives `queue_snapshot`, `current_track`, `loop_mode` at construction.

### Music Playback Flow

1. `after` callback uses `run_coroutine_threadsafe()` to call `play_next`. Errors skip via `asyncio.create_task()` — not direct recursion (avoids stack overflow on many bad URLs).
2. `last_queue.json` written after each track — intentionally not reloaded on startup.
3. Queue empty → `play_next` sets `is_playing = False`, saves `current_track` → `last_played`, clears `current_track`, triggers autoplay if enabled. Bot stays in voice channel (but see idle-leave below).

**Channel-leave timers & voice resilience** (added to survive Discord idle-drops):
- `AUTO_LEAVE_SECONDS` (300 s): `on_voice_state_update()` starts this when the last human leaves → disconnect.
- `IDLE_LEAVE_SECONDS` (7200 s / 2 h): started in `play_next()` when the queue empties and autoplay is off; if nothing plays for 2 h the bot leaves. `_cancel_idle_timer()` clears it the moment playback resumes (also on radio start). This is a safety net — Discord drops silent voice connections (code **1006**) after ~80 min anyway.
- `_voice_watchdog` (`@tasks.loop(seconds=30)`): after a 1006 auto-reconnect, playback can hang (queue non-empty but `is_playing` False, vc not playing/paused). If that state persists ≥2 ticks (~60 s) it calls `play_next()` to recover. Suppressed by `_stopped_by_user` (set on `!stop`/`!x`) and skipped during radio.
- 1006 reconnect tracebacks are downgraded to a quiet INFO line — see **Logging**.

**Race-Schutz & Stream-Retry:** `play_next` setzt den kompletten Track-Zustand (`current_track`, `track_start_time`, `is_playing`, Play-Count …) synchron **vor** `vc.play()`. `_track_generation` (erhöht bei Track-Start, Leere-Queue-Cleanup und den dm_speaking-/Radio-Returns) entwertet die Post-Play-awaits eines schnell gestorbenen Tracks — nach jedem await wird geprüft; sonst überschreiben späte Zuweisungen den Cleanup und `_progress_loop` editiert eine tote Nachricht endlos (Discord-429). Der Cleanup kappt `now_playing_msg`/`-embed`/`track_start_time`; die letzte Nachricht wandert nach `_ended_np`, damit der nächste Track ihre Buttons entfernen kann. FFmpeg-stderr läuft in eine Temp-Datei; `after_playing` loggt bei Fehler/Kurzläufer (<2 s) die letzten 20 Zeilen und klassifiziert sie (`_classify_ffmpeg_error`: `input` vs. `filter`). Stream-Track <1 s + Input-Fehler (z. B. 403 vom CDN) → genau **ein** Retry mit frischer URL (`_stream_retry_url`, `dl.invalidate()`), kein doppelter Play-Count; danach Aufgeben mit `error.stream_giveup`. Der Stream-Pfad reicht `info["http_headers"]` via `-headers` an FFmpeg durch.

`current_track` is a 3-tuple `(url, title, duration_seconds)`. Queue stores 2-tuples `(url, title)`. When re-adding `current_track` to queue (loop mode, `!eq` restart, `!replay`), always unpack: `url, title, *_ = self.current_track`.

Background tasks: `prefetch_task` downloads the next two queued songs sequentially (`_prefetch_next(0)` then `_prefetch_next(1)` — sequential because yt_dlp is not thread-safe); `_autoplay_prefetch_task` searches + downloads next autoplay song while current plays.

`_url_cache` on `self.dl`: URL → yt_dlp info-dict. All three callers cache the **full** info-dict (with `ext`, `webpage_url`) so `prepare_filename()` works. `autoplay_ydl` yields shallow playlist entries; `prefetch_autoplay()` upgrades via `ydl.extract_info(url, download=True)`. `update_ydl()` keeps only entries still in queue/`current_track`; `clear()` wipes entirely. Persisted to `metadata_cache.json` — survives bot restarts. **Persistiert wird reduziert**: nur `PERSISTED_CACHE_FIELDS` (title, ext, duration, url, webpage_url, thumbnail, uploader, http_headers — die einzigen Felder, die je aus dem Cache gelesen werden; ~1,3 MB → ~7 KB bei 4 Songs). In-Memory bleibt das volle Dict. Alte Dateien im vollen Format laden weiterhin; unlesbare werden geloggt verworfen (Kaltstart). Scheitert ein aus der Datei geladener Eintrag bei `prepare_filename`, wird er verworfen und frisch extrahiert (Cache-Miss-Pfad, nie ein User-Fehler).

### Persistenz & Dauerbetrieb (schwache Hardware)

Kein blockierendes File-I/O im Event-Loop; alle wiederkehrenden Writes sind gedebounct:

- **`_persist_flush_loop`** (`@tasks.loop(seconds=30)` in `music.py`): schreibt `play_counts.json` (`_score_dirty`, gesetzt von `_record_play`) und `metadata_cache.json` (`dl._cache_dirty`, via `dl.flush_cache()`) gebündelt — Serialisierung/Snapshot auf dem Loop, Write in `asyncio.to_thread`. **Trade-off (im Code dokumentiert):** bei hartem Crash fehlen bis zu 30 s Play-Counts bzw. Cache-Einträge (Letzteres = nur Cache-Miss).
- **Flush-Garantien:** `cog_unload` und `!restart` (in `basic.py`, vor `os._exit`) rufen `_flush_scores_now()` + `dl.flush_cache_now()` synchron auf. `!restart` setzt davor `_stopped_by_user` und trennt alle Voice-Clients; danach zuerst wt.exe/WSL-Terminal-Pfad (WSL-Setup), bei `FileNotFoundError` `os.execv` in-place (Normalfall auf Servern ohne Windows Terminal) — der genommene Pfad wird geloggt.
- **Einmalige Writes** (`radio_stations.json`, `!saveq`-Playlists) laufen ohne Debounce via `asyncio.to_thread` (`_write_stations`/`_write_playlist`). Der `last_queue.json`-Write in `after_playing` bleibt synchron — der Callback läuft ohnehin im FFmpeg-Thread, nicht im Event-Loop.
- **`DOWNLOADS_MAX_MB`** (`.env`, Default 0 = aus = heutiges Verhalten): nach jedem Download löscht `dl.cleanup_downloads()` (to_thread) die ältesten Dateien (mtime) aus `downloads/`, bis das Limit passt. Tabu: Songs in Queue/`current_track` (`protected_provider` aus `music.py`; Schutz über Cache→`prepare_filename` **und** Titel-Stem), `dl.last_resolved_file` (aktive FFmpeg-Quelle, aus dem letzten `resolve_track`-Ergebnis) und der frisch geladene Autoplay-Song (`extra_protected`). Fehler werden geschluckt — Cleanup löscht im Zweifel lieber nichts.
- **HTTP:** `lyrics_cmd` nutzt die geteilte `self._http_session` (cog_load→cog_unload; `_http()` erstellt bei geschlossener Session eine neue).

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
`_VoiceReconnectFilter` (attached to the `discord.voice_state` logger) downgrades the noisy code-1006 reconnect message from ERROR to INFO and strips its traceback — discord.py auto-reconnects on idle channels are expected, so they're logged quietly instead of as a red stack trace.

### Autoplay

Toggled via `🔁 Autoplay` button. `_prefetch_autoplay` starts at song-begin (only if queue empty): fetches YouTube Mix (`list=RD{video_id}`) via `autoplay_ydl` (max 10 entries), picks randomly from `candidates[1:]` (skips YouTube's top pick which is most personalized), downloads chosen candidate, appends to queue. On song end `play_next` waits up to 60 s for the prefetch task; falls back to `autoplay()` (same lookup, no pre-download) if needed.

Reference track: `current_track` → `last_played`. Autoplay stays on until button pressed again — not one-shot.

Die Kandidatenauswahl (nur Videos, `is_seen`-Filter, Fallback-Kaskade) lebt einmal als Modul-Funktionen in `downloader.py` (`entry_url`, `is_video`, `is_seen`, `select_autoplay_candidates`) und wird von beiden Pfaden genutzt — `MusicCommands.autoplay()` (Sofort-Pfad) und `Downloader.prefetch_autoplay()` (Hintergrund-Pfad).

`_autoplay_queued_url`: URL last added by autoplay; cleared when popped by `play_next` or evicted by `_evict_autoplay_song()`.

`_recently_played`: `deque(maxlen=15)` of URLs. `_recently_played_titles`: `deque(maxlen=15)` of normalized titles (via `normalize_title()` — defined in `utils/text.py` (dependency-free, unit-tested), re-exported from `downloader.py` — strips suffixes like "(Official Video)", special chars, lowercase, sorts words alphabetically — so "AHA Take on Me" and "Take on Me AHA" map to the same key). Autoplay filters candidates against both; falls back to filtering only `ref_url` if all candidates are in history.

**`!p` with autoplay active** — `_evict_autoplay_song()` cancels prefetch, removes autoplay URL from queue, inserts new song at front (`appendleft`). Playlist additions evict but append at end.

### Radio

Radio code lives in `cogs/music_radio.py` (`RadioMixin`). `RADIO_STATIONS_FILE` = `radio_stations.json` (key → `{name, url}`).
State: `is_radio`, `radio_station_name`, `radio_stream_url`, `_radio_reconnect_count`.
`_play_radio_stream()` plays via FFmpeg directly (no yt_dlp); `after_radio` reconnects up to 3× on error.
`!radio <Nr|Name>` → aus Liste. `!radio <url> [Name]` → spielt + speichert automatisch (kein Duplikat).
`!stop` beendet Radio oder aktuelle Wiedergabe (Queue bleibt erhalten). Radio-Modus und Song-Modus schließen sich gegenseitig aus.

### Security

Alle einschränkenden Änderungen hängen an `.env`-Flags, deren **Default das alte Verhalten beibehält** (Ausnahme: globales `allowed_mentions=none()` im Bot-Konstruktor in `main.py` — Fremd-Content wie YouTube-Titel/`!echo`/Lyrics kann nie pingen; kein Command nutzt Mentions absichtlich).

- **`URL_VALIDATION`** = `off` | `warn` (Default) | `block` — SSRF-Schutz (`utils/url_check.py`) für `!radio <url>` und `!next url||titel`. Nur http/https; Hostname darf nach DNS-Resolve nicht auf private/loopback/link-local IPs zeigen. `warn` spielt wie bisher + Warnung in Log/Channel; `block` lehnt mit i18n-Meldung ab. Nicht auflösbare Hosts gelten als ok (Stream scheitert ohnehin). `!radio` persistiert neue Sender erst **nach** erfolgreichem Stream-Start (`_play_radio_stream` → `bool`).
- **`REQUIRE_SAME_VOICE`** = `false` (Default) | `true` — Wiedergabe-steuernde Commands (`!s`, `!stop`, `!clear`, `!eq`, `!seek`, `!now`, `!remove`, `!move`, `!shuffle`) erfordern denselben Voice-Channel wie der Bot (`utils/checks.py` → `require_same_voice()`). Bot nicht in Voice → kein Check.
- **`ADMIN_ROLE_ID`** = leer (Default = kein Gating) | Rollen-ID — `!radio delete/rename` (inline `check_admin()`), `!reloadcookies`, `!format` nur für diese Rolle oder den Owner.
- **Owner-Garantie:** `is_owner()` gewinnt in beiden Checks immer — der Owner kann sich durch keine Flag-Kombination aussperren. Check-Fehlermeldungen kommen via i18n aus dem Check selbst; die `CheckFailure` schluckt `on_command_error` still. Checks greifen nicht in Tests, die Commands über `.callback` aufrufen.
- **`!loadq`** validiert ohne Flag (nur Fehlerpfad geändert): Datei muss Liste von `[url, titel]`-String-Paaren sein (`_is_valid_playlist` in `music_queue_io.py`), max. `HARD_PLAYLIST_LIMIT` Einträge — sonst i18n-Fehlermeldung statt Traceback. Valide saveq-Dateien laden unverändert.

### Key Bot Commands

Full list via `!help`. Non-obvious:
- **`!loop`** — cycles `loop_mode`: `None` → `"song"` → `"queue"` → `None`; handled in `after_playing`
- **`!now <n>`** — integer argument moves queue entry at position `n` to front and skips current song; non-integer falls through to search/URL logic
- **`!text`** — lyrics via lyrics.ovh, parses "Artist - Title" from YouTube title
