# Architektur

Beschreibung des Ist-Zustands. Das **Warum** hinter den Entscheidungen steht in [`docs/adr/`](adr/README.md);
die Regeln, die nie gebrochen werden dürfen, in `CLAUDE.md` → *Invarianten*.

Vier Cogs, geladen in `main.py` → `setup_hook`: `BasicCommands`, `MusicCommands`, `DMBridge`, `VoiceListen`.
Antworttexte kommen über `utils/i18n.t()` aus `locales/de.json`/`locales/en.json` (`LANGUAGE`, Default `en`).

## Themen

| Datei | Inhalt |
|---|---|
| [`architecture/playback.md`](architecture/playback.md) | Playback-Flow, Race-Schutz & Stream-Retry, Now-Playing-Karte, Streaming vs. Download, Prefetch, Persistenz, Audio-Config |
| [`architecture/voice-connection.md`](architecture/voice-connection.md) | Auto-/Idle-Leave, Watchdog, Voice-Reparatur nach Gateway-Abbruch, toter Voice-Poller |
| [`architecture/autoplay-radio.md`](architecture/autoplay-radio.md) | Autoplay-Auswahl, Prefetch, Radio |
| [`architecture/logging.md`](architecture/logging.md) | Terminal-Modi, Tags, bot.log-Rotation, Fehlersuche (`/debug`, `tools/logsummary.py`) |
| [`architecture/security.md`](architecture/security.md) | Security-Flags, URL-Check, YouTube-Cookies |
| [`architecture/sprachsteuerung.md`](architecture/sprachsteuerung.md) | Parser, zwei Eingänge, eigenes Zuhören, DM-Bot-Weiche |

## Cogs & Module

- **`cogs/basic.py`** — `BasicCommands`: `!j`, `!l`, `!ping`, `!echo`, `!help` (→ `views/help_view.py`
  `HelpView`), `!debug on|off` (Terminal-Modi → [logging.md](architecture/logging.md)); außerdem `!restart`
  (Mechanik → [ADR 0003](adr/0003-windows-native-zielplattform.md)).
- **`cogs/music.py`** — `MusicCommands`: queue, FFmpeg playback, EQ presets (`cogs/presets.py` → `EQ_PRESETS`),
  autoplay; delegates all yt_dlp work to `self.dl`. Alles Weitere steckt in Mixins — same instance state,
  registered as cog commands via the MRO (Zerlegung → [ADR 0009](adr/0009-music-cog-mixin-zerlegung.md)):
  - **`cogs/music_playback.py`** — `PlaybackMixin`: `play_next`-Orchestrator (delegiert an
    `_handle_queue_empty`, `_build_audio_source`, `_make_after_playing`, `_snapshot_track_state`,
    `_build_np_embed`, `_schedule_background_work`, `_skip_broken_track` → [playback.md](architecture/playback.md)),
    `_stop_for_advance`, Prefetch-Kick. Der gemeinsame Such-/URL-Flow von `!p`/`!next`/`!now` steckt in vier Helpern:
  `_stop_radio_for_takeover` (Radio-Vorspann), `_extract_info_or_report` (yt_dlp-Fetch + Fehlermeldung,
  Sentinel `_YTDLP_FAILED`), `_search_and_enqueue` (ytsearch3-Zweig; Insert-Politik/Eviction/i18n-Keys als
  Parameter — Semantik-Unterschiede der drei Befehle stehen in der Docstring), `_fetch_single_track_info`
  (URL-Zweig von `!next`/`!now`; `!p` behält seinen eigenen wegen Playlist-Support + Duplikat-Warnung).
  - **`cogs/music_voice_ui.py`** — `VoiceLifecycleMixin`: Auto-/Idle-Leave, `_voice_watchdog` (auch toter
    Voice-Poller), Voice-Reparatur nach Gateway-Abbruch (`on_socket_raw_receive`/`on_disconnect`/`on_resumed`/
    `on_ready` → `_heal_voice`/`_heal_one`, `GATEWAY_ZOMBIE_SECONDS`, danach Chat-Hinweis `status.voice_rebuilt(_track)`) →
    [voice-connection.md](architecture/voice-connection.md);
    `PlaybackUiMixin`: Fortschrittsbalken, Pause-Uhr (`_elapsed_seconds`), `_retire_np_message`.
  - **`cogs/music_voice_guard.py`** — `VoiceGuardMixin`: Mute-Schutz (entmutet den Bot bei Server-Mute selbst,
    ohne Recht Pause bis zum Unmute) und Move-/Rauswurf-Schutz (nur der Owner darf verschieben oder rauswerfen, Audit-Log entscheidet)
    → [voice-connection.md](architecture/voice-connection.md)
  - **`cogs/music_radio.py`** — `RadioMixin`: internet-radio streaming (`_play_radio_stream`, reconnect,
    `!radio`), `RADIO_STATIONS_FILE`
  - **`cogs/music_stats.py`** — `StatsMixin`: `!score` (play counts), `!stats` (process metrics)
  - **`cogs/music_queue_io.py`** — `QueuePersistenceMixin`: `!saveq`/`!loadq`/`!loadq last`/`!lists`, `PLAYLISTS_DIR`
- **`cogs/downloader.py`** — `Downloader` (`self.dl`): five yt_dlp instances (`ydl`, `search_ydl`, `url_ydl`,
  `playlist_ydl`, `autoplay_ydl` — aufgelöst per `kind` in `extract_info_async`, Cookie-Fallback via
  `enable_cookie_mode` → [ADR 0010](adr/0010-cookielos-zuerst-cookie-fallback.md)), `_url_cache`, `resolve_track()`, `prefetch_next()`,
  `prefetch_autoplay()`. Also defines `DOWNLOAD_DIR`. `resolve_track()` läuft in vier Phasen:
  `_resolve_metadata` → `_adopt_inflight` → `_clear_corpse` → `_acquire_file`.
- **`cogs/downloader_progressive.py`** — `ProgressiveDownloadMixin` (Basis von `Downloader`): In-Flight-Registry
  `_inflight` („pro URL maximal ein schreibender Download" → [ADR 0002](adr/0002-inflight-registry.md)),
  progressiver Download (`_start_progressive`, `_run_progressive`, `_wait_for_buffer`, `prime_first_hit`) und
  die Konstanten `STREAM_THRESHOLD_SECONDS`/`PROGRESSIVE_*` (von `downloader.py` re-exportiert). Zustand in
  `Downloader.__init__`; `_purge_stale_progressive` bleibt im Downloader (braucht `DOWNLOAD_DIR`).
- **`cogs/dm_bridge.py`** — `DMBridge`: aiohttp server (`DM_BRIDGE_HOST`/`DM_BRIDGE_PORT`/`DM_BRIDGE_SECRET`
  in `config.py`) so a separate DM-Bot (AI dungeon master) can make this bot speak. Default `127.0.0.1` +
  empty secret = classic localhost path mode; set host to a LAN/Tailscale IP + a shared `DM_BRIDGE_SECRET` for
  remote mode. `GET /health`, `GET /debug` (nur localhost, Zustands-Schnappschuss → [logging.md](architecture/logging.md)),
  `POST /command` (nur mit `VOICE_CONTROL`, → `VoiceListen.handle_text()`), `POST /speak` in **two transport modes**: (1) JSON `{"path", "guild_id"?}` →
  path mode (shared disk, localhost); (2) body with `Content-Type: audio/wav` (or `application/octet-stream`)
  + optional `X-DM-Guild-Id` header → byte mode (WAV bytes written to a `tempfile`, auto-deleted after
  playback). Non-loopback requests must pass the secret via HMAC (`hmac.compare_digest`). Both modes play via
  `FFmpegOpusAudio` (same pattern as `_play_radio_stream`). `!dm` shows status. Blocking-`/speak`,
  `bot.dm_speaking` und der bewusst fehlende Rück-Callback → [ADR
  0008](adr/0008-dm-bridge-blocking-speak-ohne-callback.md).
- **`cogs/voice_listen.py`** — `VoiceListen`: Sprachsteuerung, `handle_text()` als gemeinsamer Eingang
  (DM-Bot-`/command` + eigenes Zuhören), `!listen on|off`; **`cogs/voice_sink.py`** — `SpeechSink`, einzige Datei
  mit `voice_recv`-Import auf Modulebene. Dazu `utils/nl_parser.py` (Weckwort + Intents), `utils/pcm.py`
  (48k-Stereo → 16k-Mono), `utils/speech_buffer.py` (`SpeakerBuffers`), `utils/stt.py` (faster-whisper) →
  [sprachsteuerung.md](architecture/sprachsteuerung.md).
- **`utils/logger.py`** — `logger`, Terminal-Modi (`set_console_mode`, `_ESSENTIAL_TAGS`/`_QUIET_TAGS`),
  `rotate_on_start` (bot.log-Rotation) → [logging.md](architecture/logging.md); **`tools/logsummary.py`** —
  Zeitleiste eines Laufs aus `bot.log` (`--list`, `--session`); **`utils/env_check.py`** — `missing_env_keys()`
  gegen `.env.example`, Startzeile `[Config]` in `setup_hook`.
- **`utils/checks.py`** (`require_same_voice`/`require_admin`), **`utils/url_check.py`** (`check_url`) →
  [security.md](architecture/security.md); **`utils/files.py`** (`safe_unlink`, `drain_pending_deletes`),
  **`utils/text.py`** (`normalize_title`/`parse_time`/`progress_bar`), **`utils/i18n.py`** (`t()`).
- **`utils/ffmpeg.py`** — zustandslos: `ffmpeg_header_opts`/`stderr_tail`/`classify_ffmpeg_error`; `music.py`
  spiegelt sie (plus `utils/text.progress_bar`) als Klassen-Aliase `_ffmpeg_header_opts`/`_stderr_tail`/
  `_classify_ffmpeg_error`/`_progress_bar`, damit bestehende Aufrufer und Tests unverändert bleiben.
- **`utils/voice.py`** — `stop_playback`, `connect_voice`, `voice_client_cls` (Klassenwahl per `.env`,
  `SafeVoiceRecvClient`), `dead_voice_poller`; **`utils/diagnostics.py`** — `snapshot()` für `GET /debug`.
- **`utils/autoplay.py`** — zustandslose Autoplay-Kandidatenauswahl (→ [autoplay-radio.md](architecture/autoplay-radio.md));
  **`utils/ytdlp_update.py`** — `check_ytdlp_update()`, einmaliger PyPI-Vergleich nach `on_ready`.
- **`utils/shutdown.py`** — `install_sigint_handler(bot)`, in `main.py` vor `bot.run()` registriert: Strg+C im
  Terminal ist zweistufig. 1× fragt nur nach (Fenster `CONFIRM_WINDOW` = 5 s, danach meldet ein
  `loop.call_later`-Timer „Abbruch“ und entschärft), 2× fährt geordnet über `bot.close()` herunter (entlädt
  alle Cogs → `cog_unload` flusht Scores/Cache und stoppt die DM-Bridge, danach Voice-Disconnect; vorher
  `_stopped_by_user = True` wie bei `!restart`), 3× bricht mit `os._exit(0)` hart ab. Ohne laufenden Loop
  (Strg+C während des Starts) fällt der Handler auf `KeyboardInterrupt` zurück. Ein Fehler in `close()` wird
  geloggt und beendet den Prozess mit Code 1, statt still im Task zu verschwinden; `main.py` protokolliert
  nach `bot.run()` `[Shutdown] Bot beendet.` — fehlt die Zeile, hing das Herunterfahren. Deshalb startet
  `start.bat` den Bot per `start` in einem **eigenen** Konsolenfenster: in einer Batch-Datei würde cmd.exe
  jedes Strg+C abfangen und `Terminate batch job (Y/N)?` fragen.
- **`views/music_controls.py`** — `MusicControlView`: Pause/Resume/Skip/Autoplay/Loop buttons. Only current song
  keeps its card — previous ones go through `_retire_np_message` (→ Textzeile `🎶 Titel`). Resume: (1) paused →
  resume, (2) queue has songs → `play_next`, (3) queue empty → Song dieser Karte vorne einreihen, (4) autoplay
  on → `autoplay()`. Pause-Button bestätigt ephemer mit Position (`Pausiert bei m:ss` via
  `_elapsed_seconds()`; ohne `track_start_time`, z. B. Radio, ohne Position). `SearchAutoplayView`: first
  search result plays immediately, alternatives as buttons (30 s timeout). Stale-Buttons-Fallback nach
  Neustart (`custom_id`-Schema, `StaleControlsFallback`-DynamicItem) → [ADR
  0006](adr/0006-stale-buttons-dynamicitem.md).
- **`views/queue_view.py`** — `QueueView`: paginated queue embed with Prev/Next buttons; receives
  `queue_snapshot`, `current_track`, `loop_mode`, `autoplay_next_title` at construction. Footer summiert Dauern **nur aus dem
  Metadaten-Cache** (kein yt_dlp-Lookup); fehlen welche → `≥ Summe (n ohne Angabe)` als Untergrenze.


## Key Bot Commands

Full list via `!help`. Non-obvious:

- **`!loop`** — cycles `loop_mode`: `None` → `"song"` → `"queue"` → `None`; handled in `after_playing`
- **`!now <n>`** — integer argument moves queue entry at position `n` to front and skips current song;
  non-integer falls through to search/URL logic
- **`!text`** — lyrics via lyrics.ovh, parses "Artist - Title" from YouTube title
