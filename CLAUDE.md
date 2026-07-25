# CLAUDE.md

Setup → `README.md`, Cookies → `SETUP.md`. Tests: `pytest` (suite in `tests/`, dev deps in `requirements-dev.txt`). Lint: `ruff check .` (nur E/F/W, line-length 140, Config in `pyproject.toml` — bewusst keine Stilregeln). Start: `start.bat` (Windows-Normalfall) bzw. `python main.py`.

**Zielplattform: Windows nativ** (Start via `start.bat`); Linux/macOS bleibt lauffähig → [ADR 0003](docs/adr/0003-windows-native-zielplattform.md).

## Invarianten — niemals brechen

- Queue speichert 2-Tupel `(url, title)`; `current_track` ist ein 3-Tupel `(url, title, duration_seconds)`. Beim Re-Add in die Queue (Loop-Mode, `!eq`-Restart, `!replay`) immer entpacken: `url, title, *_ = self.current_track`.
- Keine nackten `unlink`/`os.remove` in `cogs/` — immer `utils/files.py` → `safe_unlink` → [ADR 0003](docs/adr/0003-windows-native-zielplattform.md).
- Pro URL maximal ein schreibender Download; lebendig vs. Leiche entscheidet allein die `_inflight`-Registry, nie eine Datei-Heuristik. Eine wachsende progressive Datei nie unlinken/neu starten — den laufenden Task adoptieren → [ADR 0002](docs/adr/0002-inflight-registry.md).
- `vc.stop()` auf Musik-Quellen nur via `_stop_for_advance()` (Radio-Quellen nicht) → [ADR 0001](docs/adr/0001-progressiver-download-statt-cdn-stream.md).
- In `after_playing` läuft der Retry-/Resume-Marker-Reset **nach** dem Progressive-Resume-Block (sonst griffe dessen Kappe nie) → [architecture.md](docs/architecture.md) → *Race-Schutz & Stream-Retry*.
- Track-Zustand komplett **vor** `vc.play()` setzen; nach jedem Post-Play-await `_track_generation` prüfen, sonst überschreiben späte Zuweisungen den Cleanup → [ADR 0007](docs/adr/0007-track-zustand-vor-play-generationszaehler.md).
- Wachsende Dateien immer transkodieren, nie `codec=copy` → [ADR 0001](docs/adr/0001-progressiver-download-statt-cdn-stream.md).
- Now-Playing-Karte (Embed + Buttons) nur beim aktuellen Song: jede hinterlassene Nachricht über `_retire_np_message` zur Textzeile `🎶 Titel` zurückbauen, nie leer editieren (Discord lehnt eine Nachricht ohne Text/Embed/Anhang mit 400 ab → Karte bliebe still stehen) → [architecture.md](docs/architecture.md) → *Music Playback Flow*.
- `StaleControlsFallback`: discord.py dispatcht Dynamic Items **zusätzlich** zu Live-Views — die Abgrenzung kommt allein aus dem Template-Regex (negative lookahead auf BOOT_ID), nie die Reihenfolge ändern → [ADR 0006](docs/adr/0006-stale-buttons-dynamicitem.md).
- Neue `t()`-Keys immer in **beiden** `locales/*.json` anlegen — `tests/test_i18n_keys.py` erzwingt Key- und Platzhalter-Parität.
- `config.py` ruft nie `logging.basicConfig()` — das deaktiviert still den File-Handler; `logger` aus `utils.logger` importieren → [architecture.md](docs/architecture.md) → *Logging*.
- Kein blockierendes File-I/O im Event-Loop; wiederkehrende Writes nur gedebounct → [ADR 0005](docs/adr/0005-persistenz-debouncing.md).
- yt_dlp ist nicht thread-safe — Prefetch-Downloads laufen sequenziell, nie parallel → [architecture.md](docs/architecture.md) → *Music Playback Flow*.
- yt_dlp läuft cookielos; Cookies kommen erst, wenn YouTube sie verlangt (eine angemeldete Session bekommt Werbung → 5–6 s Zwangspause vor dem ersten Byte). Extraktionen daher immer über `dl.extract_info_async(query, kind)`, nie direkt auf einer yt_dlp-Instanz → [ADR 0010](docs/adr/0010-cookielos-zuerst-cookie-fallback.md).

## Architektur-Landkarte

- `cogs/basic.py` — `!j`/`!l`/`!ping`/`!echo` + `!restart`
- `cogs/music.py` — `MusicCommands`-Kern (Queue, EQ, Autoplay, Klassen-Aliase `_ffmpeg_header_opts`/`_stderr_tail`/`_classify_ffmpeg_error`/`_progress_bar`); Mixins: `music_playback.py` (`PlaybackMixin`: `play_next`-Orchestrator + `_handle_queue_empty`/`_build_audio_source`/`_make_after_playing`/`_snapshot_track_state`, Prefetch, Such-/Resolve-Flow → [ADR 0009](docs/adr/0009-music-cog-mixin-zerlegung.md)), `music_voice_ui.py` (`VoiceLifecycleMixin` Join/Leave/Idle/Watchdog, `PlaybackUiMixin` Fortschrittsbalken), `music_radio.py` (Radio), `music_stats.py` (`!score`/`!stats`), `music_queue_io.py` (`!saveq`/`!loadq`/`!lists`)
- `cogs/downloader.py` — alles yt_dlp: fünf Instanzen (`extract_info_async` löst sie per `kind` auf), Cookie-Modus (`enable_cookie_mode`), `_url_cache`, `resolve_track()`, Prefetch (`prime_first_hit` für den Sofortstart), In-Flight-Registry, `DOWNLOAD_DIR`
- `cogs/dm_bridge.py` — HTTP-Server, über den "Bot B" (KI-Dungeon-Master) diesen Bot sprechen lässt
- `cogs/presets.py` — EQ-Filterketten (`EQ_PRESETS`) + FFmpeg-Filter-Notizen
- `views/music_controls.py` — Playback-Buttons + Stale-Buttons-Fallback; `views/queue_view.py` — paginierte Queue
- `utils/` — `logger`, `files.safe_unlink`, `checks`, `url_check`, `text` (`normalize_title`/`parse_time`/`progress_bar`), `ffmpeg` (zustandslos: `ffmpeg_header_opts`/`stderr_tail`/`classify_ffmpeg_error`, in `music.py` via Klassen-Aliase gespiegelt), `i18n.t`

## Doku

- [`docs/architecture.md`](docs/architecture.md) — Playback-Flow, Cache, Autoplay, Radio, Security-Flags, DM-Bridge, Voice-Resilienz im Detail
- [`docs/adr/`](docs/adr/README.md) — nummerierte Entscheidungen (das Warum)
- [`docs/smoke-checklist.md`](docs/smoke-checklist.md) — manuelle End-to-End-Checkliste
