# Architektur

Beschreibung des Ist-Zustands. Das **Warum** hinter den Entscheidungen steht in [`docs/adr/`](adr/README.md); die Regeln, die nie gebrochen werden dürfen, in `CLAUDE.md` → *Invarianten*.

Four cogs loaded at startup, all responses in German.

## Cogs & Module

- **`cogs/basic.py`** — `BasicCommands`: `!j`, `!l`, `!ping`, `!echo`; außerdem `!restart` (Mechanik → [ADR 0003](adr/0003-windows-native-zielplattform.md)).
- **`cogs/music.py`** — `MusicCommands`: queue, FFmpeg playback, EQ presets, autoplay; delegates all yt_dlp work to `self.dl`. Der gemeinsame Such-/URL-Flow von `!p`/`!next`/`!now` steckt in vier Helpern: `_stop_radio_for_takeover` (Radio-Vorspann), `_extract_info_or_report` (yt_dlp-Fetch + Fehlermeldung, Sentinel `_YTDLP_FAILED`), `_search_and_enqueue` (ytsearch3-Zweig; Insert-Politik/Eviction/i18n-Keys als Parameter — Semantik-Unterschiede der drei Befehle stehen in der Docstring), `_fetch_single_track_info` (URL-Zweig von `!next`/`!now`; `!p` behält seinen eigenen wegen Playlist-Support + Duplikat-Warnung). Radio/stats/queue-persistence commands are split into mixins — same instance state, registered as cog commands via the MRO:
  - **`cogs/music_radio.py`** — `RadioMixin`: internet-radio streaming (`_play_radio_stream`, reconnect, `!radio`), `RADIO_STATIONS_FILE`
  - **`cogs/music_stats.py`** — `StatsMixin`: `!score` (play counts), `!stats` (process metrics)
  - **`cogs/music_queue_io.py`** — `QueuePersistenceMixin`: `!saveq`/`!loadq`/`!lists`, `PLAYLISTS_DIR`
- **`cogs/downloader.py`** — `Downloader` (`self.dl`): five yt_dlp instances (`ydl`, `search_ydl`, `url_ydl`, `playlist_ydl`, `autoplay_ydl` — all via `self.dl.*`), `_url_cache`, `resolve_track()`, `prefetch_next()`, `prefetch_autoplay()`. Also defines `DOWNLOAD_DIR`. Invariante „pro URL maximal ein schreibender Download" mit In-Flight-Registry `_inflight` → [ADR 0002](adr/0002-inflight-registry.md).
- **`cogs/dm_bridge.py`** — `DMBridge`: aiohttp server (`DM_BRIDGE_HOST`/`DM_BRIDGE_PORT`/`DM_BRIDGE_SECRET` in `config.py`) so a separate "Bot B" (AI dungeon master) can make this bot speak. Default `127.0.0.1` + empty secret = classic localhost path mode; set host to a LAN/Tailscale IP + a shared `DM_BRIDGE_SECRET` for remote mode. `GET /health`, `POST /speak` in **two transport modes**: (1) JSON `{"path", "guild_id"?}` → path mode (shared disk, localhost); (2) body with `Content-Type: audio/wav` (or `application/octet-stream`) + optional `X-DM-Guild-Id` header → byte mode (WAV bytes written to a `tempfile`, auto-deleted after playback). Non-loopback requests must pass the secret via HMAC (`hmac.compare_digest`). Both modes play via `FFmpegOpusAudio` (same pattern as `_play_radio_stream`). `!dm` shows status. Blocking-`/speak`, `bot.dm_speaking` und der bewusst fehlende Rück-Callback → [ADR 0008](adr/0008-dm-bridge-blocking-speak-ohne-callback.md).
- **`utils/shutdown.py`** — `install_sigint_handler(bot)`, in `main.py` vor `bot.run()` registriert: Strg+C im Terminal ist zweistufig. 1× fragt nur nach (Fenster `CONFIRM_WINDOW` = 5 s, danach meldet ein `loop.call_later`-Timer „Abbruch“ und entschärft), 2× fährt geordnet über `bot.close()` herunter (entlädt alle Cogs → `cog_unload` flusht Scores/Cache und stoppt die DM-Bridge, danach Voice-Disconnect; vorher `_stopped_by_user = True` wie bei `!restart`), 3× bricht mit `os._exit(0)` hart ab. Ohne laufenden Loop (Strg+C während des Starts) fällt der Handler auf `KeyboardInterrupt` zurück. Ein Fehler in `close()` wird geloggt und beendet den Prozess mit Code 1, statt still im Task zu verschwinden; `main.py` protokolliert nach `bot.run()` `[Shutdown] Bot beendet.` — fehlt die Zeile, hing das Herunterfahren. Deshalb startet `start.bat` den Bot per `start` in einem **eigenen** Konsolenfenster: in einer Batch-Datei würde cmd.exe jedes Strg+C abfangen und `Terminate batch job (Y/N)?` fragen.
- **`views/music_controls.py`** — `MusicControlView`: Pause/Resume/Skip/Autoplay buttons. Only current song keeps buttons — previous get `view=None`. Resume: (1) paused → resume, (2) queue has songs → `play_next`, (3) autoplay on → `autoplay()`. Pause-Button bestätigt ephemer mit Position (`Pausiert bei m:ss` via `_elapsed_seconds()`; ohne `track_start_time`, z. B. Radio, ohne Position). `SearchAutoplayView`: first search result plays immediately, alternatives as buttons (30 s timeout). Stale-Buttons-Fallback nach Neustart (`custom_id`-Schema, `StaleControlsFallback`-DynamicItem) → [ADR 0006](adr/0006-stale-buttons-dynamicitem.md).
- **`views/queue_view.py`** — `QueueView`: paginated queue embed with Prev/Next buttons; receives `queue_snapshot`, `current_track`, `loop_mode` at construction. Footer summiert Dauern **nur aus dem Metadaten-Cache** (kein yt_dlp-Lookup); fehlen welche → `≥ Summe (n ohne Angabe)` als Untergrenze.

## Music Playback Flow

`play_next` ist ein Orchestrator in `PlaybackMixin` (`cogs/music_playback.py`) und delegiert an vier Untermethoden → [ADR 0009](adr/0009-music-cog-mixin-zerlegung.md): `_handle_queue_empty` (Leere-Queue-Fall + Autoplay-Handoff), `_build_audio_source` (FFmpeg-Options + stderr-Temp + Source, synchron), `_snapshot_track_state` (Track-Zustand vor `vc.play()`, synchron) und `_make_after_playing` (Factory, die den `after_playing`-Callback baut).

1. `after` callback (aus `_make_after_playing`) uses `run_coroutine_threadsafe()` to call `play_next`. Errors skip via `asyncio.create_task()` — not direct recursion (avoids stack overflow on many bad URLs).
2. `last_queue.json` written after each track — intentionally not reloaded on startup.
3. Queue empty → `play_next` delegiert an `_handle_queue_empty`: sets `is_playing = False`, saves `current_track` → `last_played`, clears `current_track`, triggers autoplay if enabled. Bot stays in voice channel (but see idle-leave below).

**Channel-leave timers & voice resilience** (added to survive Discord idle-drops):

- `AUTO_LEAVE_SECONDS` (300 s): `on_voice_state_update()` starts this when the last human leaves → disconnect.
- `IDLE_LEAVE_SECONDS` (7200 s / 2 h): started in `play_next()` when the queue empties and autoplay is off; if nothing plays for 2 h the bot leaves. `_cancel_idle_timer()` clears it the moment playback resumes (also on radio start). This is a safety net — Discord drops silent voice connections (code **1006**) after ~80 min anyway.
- `_voice_watchdog` (`@tasks.loop(seconds=30)`): after a 1006 auto-reconnect, playback can hang (queue non-empty but `is_playing` False, vc not playing/paused). If that state persists ≥2 ticks (~60 s) it calls `play_next()` to recover. Suppressed by `_stopped_by_user` (set on `!stop`/`!x`) and skipped during radio.
- 1006 reconnect tracebacks are downgraded to a quiet INFO line — see **Logging**.

**Race-Schutz & Stream-Retry:** `play_next` setzt den Track-Zustand synchron in `_snapshot_track_state` vor `vc.play()` und prüft danach nach jedem Post-Play-await (im Orchestrator, nicht in den Untermethoden) den Generationszähler `_track_generation` → [ADR 0007](adr/0007-track-zustand-vor-play-generationszaehler.md). FFmpeg-stderr läuft in eine Temp-Datei; der `after_playing`-Callback (gebaut von `_make_after_playing`) loggt bei Fehler/Kurzläufer (<2 s) die letzten 20 Zeilen und klassifiziert sie (`_classify_ffmpeg_error`: `input` vs. `filter`). Stream-Track <1 s + Input-Fehler (z. B. 403 vom CDN) → genau **ein** Retry mit frischer URL (`_stream_retry_url`, `dl.invalidate()`), kein doppelter Play-Count; schlägt auch der fehl, folgt **ein** letzter Anlauf als lokaler Download (`_force_download_url` → `resolve_track(force_download=True)`; yt_dlp-eigener HTTP-Client, den CDN-403s gegen FFmpeg nicht treffen; Meldung `error.stream_download_fallback`). Liefert der Fallback keine Datei (>20-min-Tracks werden nie heruntergeladen) oder stirbt auch die Datei sofort → Aufgeben mit `error.stream_giveup`. Alle Retry-/Resume-Marker (`_stream_retry_url`, `_force_download_url`, `_progressive_resume_url`/`-count`) leben bis zum normalen Track-Ende bzw. Trackwechsel; der Marker-Reset läuft **nach** dem Progressive-Resume-Block (sonst griffe dessen Kappe nie). Diese Kaskade ist heute nur noch Fallback — Primärpfad für ungecachte kurze Tracks ist der progressive Download → [ADR 0001](adr/0001-progressiver-download-statt-cdn-stream.md). Der Stream-Pfad reicht `info["http_headers"]` via `-headers` an FFmpeg durch.

**Now-Playing-Karte:** Embed samt Buttons gibt es **nur für den aktuellen Song**. Jede Nachricht, die ein Track hinterlässt, geht durch `_retire_np_message(msg, title)` (`PlaybackUiMixin`) und wird zur Textzeile `🎶 Titel` — beim Trackwechsel, beim Queue-Ende (`_ended_np`), bei der Radio-Übernahme und bei der 429-Abschaltung (`_disable_np_edits` übergibt die Nachricht an `_ended_np`, statt sie zu vergessen). Zwei Fallstricke, die dabei je eine Karte für immer stehen ließen: `now_playing_msg` und `_ended_np` können **gleichzeitig** belegt sein (beide zurückbauen, nicht `elif`), und der Inhalt darf nie leer werden — eine Nachricht ohne Text, Embed und Anhang lehnt Discord mit 400 ab, der Rückbau schlägt still fehl. Der Titel dafür steht in `_np_title` (mit der Karte gesetzt), nicht in `last_played`/`prev_track`, die zum Rückbauzeitpunkt schon geleert sein können.

Background tasks: `prefetch_task` downloads the next two queued songs sequentially (`_prefetch_next(0)` then `_prefetch_next(1)` — sequential because yt_dlp is not thread-safe); `_autoplay_prefetch_task` searches + downloads next autoplay song while current plays.

`_url_cache` on `self.dl`: URL → yt_dlp info-dict. All three callers cache the **full** info-dict (with `ext`, `webpage_url`) so `prepare_filename()` works. `autoplay_ydl` yields shallow playlist entries; `prefetch_autoplay()` upgrades via `ydl.extract_info(url, download=True)`. `update_ydl()` keeps only entries still in queue/`current_track`; `clear()` wipes entirely. Persisted to `metadata_cache.json` — survives bot restarts; persistiert wird ein reduzierter Feldsatz → [ADR 0005](adr/0005-persistenz-debouncing.md).

## Persistenz & Dauerbetrieb (schwache Hardware)

Grundsatz „kein blockierendes File-I/O im Event-Loop", Debounce-Loop, Flush-Garantien und reduzierter Metadaten-Cache → [ADR 0005](adr/0005-persistenz-debouncing.md). Windows-sichere Löschungen (`safe_unlink`, Pending-Delete-Liste, `.inprogress`-Sidecar) und die `!restart`-Mechanik → [ADR 0003](adr/0003-windows-native-zielplattform.md).

- **`DOWNLOADS_MAX_MB`** (`.env`, Default 0 = aus = heutiges Verhalten): nach jedem Download löscht `dl.cleanup_downloads()` (to_thread) die ältesten Dateien (mtime) aus `downloads/`, bis das Limit passt. Tabu: Songs in Queue/`current_track` (`protected_provider` aus `music.py`; Schutz über Cache→`prepare_filename` **und** Titel-Stem), `dl.last_resolved_file` (aktive FFmpeg-Quelle, aus dem letzten `resolve_track`-Ergebnis) und der frisch geladene Autoplay-Song (`extra_protected`). Fehler werden geschluckt — Cleanup löscht im Zweifel lieber nichts.
- **HTTP:** `lyrics_cmd` nutzt die geteilte `self._http_session` (cog_load→cog_unload; `_http()` erstellt bei geschlossener Session eine neue).

## Audio Configuration

Default format: `webm`. Default EQ preset: `punchy`. Filter chains in `cogs/presets.py` (`EQ_PRESETS`).
Presets: `bassboost`, `flat`, `vocalboost`, `superbass`, `punchy`, `nightcore`, `karaoke`, `8d`.
`!eq` mid-song: restarts current track with new filter (prepends to queue, calls stop).
`!format mp3|webm` or `!eq <preset>` → `update_ydl()` → `self.dl.rebuild()` recreates all five instances.
FFmpeg filter notes and "do not add" list → comments at top of `cogs/presets.py`.

## Streaming vs. Download

`STREAM_THRESHOLD_SECONDS = 20 * 60` (in `downloader.py`). `resolve_track()` returns a direct CDN URL (`str`, not `Path`) in two cases — **this is intentional, not a bug**:

- Duration > 20 min → always stream, never download.
- File not locally cached **und der progressive Pfad greift nicht** (Gates → [ADR 0001](adr/0001-progressiver-download-statt-cdn-stream.md)) → stream; `prefetch_next()` downloads queue songs in background.

Der progressive Download ist der Primärpfad für ungecachte Tracks ≤ 20 min; Puffer, Gates, Buchhaltung, Resume, `_stop_for_advance`, Blockliste und Schnellstart → [ADR 0001](adr/0001-progressiver-download-statt-cdn-stream.md).

**Sofortstart des ersten Treffers:** Läuft gerade nichts, stößt `prime_first_hit()` den progressiven Download des ersten Suchtreffers (bzw. der `!p <url>`-Eingabe) sofort an — er läuft damit parallel zu den Discord-Nachrichten statt hinter ihnen; `resolve_track` adoptiert den laufenden Task über die In-Flight-Registry → [ADR 0010](adr/0010-cookielos-zuerst-cookie-fallback.md).

**Prefetch-Kick:** Titel, die mitten im Song eingereiht werden (`!p`/`!next`/`!loadq`), starten via `_kick_prefetch()` sofort den Vorlade-Task (`_prefetch_upcoming`, von `play_next` wiederverwendet) — sonst entstünde beim Übergang die volle Extraktions-+Pufferlatenz. Bewusst kein Kick bei `!now` (spielt sofort; ein Prefetch auf denselben Song würde `resolve_track` auf den kompletten Download warten lassen statt progressiv zu starten).

**Kein globales Idle-Warten:** `_prefetch_upcoming` gönnt dem progressiven Download des laufenden Songs nur eine kurze Startphase (`wait_progressive_idle(timeout=15.0)`), dann lädt der Queue-Prefetch sequenziell los. Das frühere unbegrenzte Warten (bis 300 s) parkte den Task praktisch immer — der laufende Song lädt fast immer progressiv — und machte alle `_kick_prefetch`-Aufrufe zu No-Ops.

**Queue-Umbau:** `!shuffle`/`!move`/`!remove` und der Alternativ-Button (Queue-Ersatz) rufen `_restart_prefetch()` — laufenden Sammel-Task canceln (ein bereits schreibender Download-Worker wird adoptiert und läuft zu Ende → [ADR 0002](adr/0002-inflight-registry.md), Cancel-Pfad) und neu kicken, weil der alte Task auf die alte Reihenfolge zielt. `_kick_prefetch` allein wäre wegen seines No-Op-Guards wirkungslos. Die Sequenzialität der Prefetch-Downloads (yt_dlp ist nicht thread-safe) sichert ein Lock um den Download-Aufruf in `_prefetch_one`.

`play_next` detects streams via `isinstance(filename, str)` → adds FFmpeg reconnect options, skips `codec=copy`. Wachsende Dateien (`is_growing` via `dl.is_incomplete`): Wiedergabe- und Resume-Regeln → [ADR 0001](adr/0001-progressiver-download-statt-cdn-stream.md).
`prefetch_next()` skips download for videos > 20 min.

## YouTube Authentication (Cookies)

Cookie config read from `.env` via `update_ydl()`. `cookiefile` takes priority over `cookiesfrombrowser`. Details → `SETUP.md`.

**Cookielos im Normalbetrieb** (`_cookie_mode = False`): Eine angemeldete YouTube-Session bekommt Pre-Roll-Werbung, deren Skip-Zeit yt_dlp vor dem ersten Byte abwarten muss (`format["available_at"]`, sonst CDN-403) — das kostete 5–6 s Startlatenz pro ungecachtem Track. Cookies schaltet erst `enable_cookie_mode()` zu: automatisch bei Auth-Fehlern (`COOKIE_ERROR_MARKERS` → Alterssperre, Bot-Check, Mitglieder-Video; dann ein Zweitversuch derselben Abfrage) oder manuell per `!reloadcookies`. Danach bleibt der Modus für den Rest des Prozesses aktiv und der Metadaten-Cache wird geleert. Alle Extraktionen laufen über `dl.extract_info_async(query, kind)` → [ADR 0010](adr/0010-cookielos-zuerst-cookie-fallback.md).

## Logging

`utils/logger.py` — console + `bot.log`. `config.py` must NOT call `logging.basicConfig()` — silently disables the file handler. Import `logger` from `utils.logger`.

**Terminal-Modi:** `LOG_MODE` aus `.env` (`quiet` Default | `debug`) setzt den Start-Modus des Console-Handlers: `quiet` = wesentliche Ereignisse + alle echten Probleme, `debug` = volle Diagnose. `quiet` filtert dabei über den `[Tag]` am Zeilenanfang, nicht über das Handler-Level: Songwechsel, Queue und Radio laufen als INFO, ein Level-Filter auf WARNING würde den kompletten Normalbetrieb verschlucken. Maßgeblich sind `_ESSENTIAL_TAGS` (sichtbar) und `_QUIET_TAGS` (still) in `utils/logger.py`; WARNING+ passiert den Filter immer, auch bei stillen Tags. `tests/test_logger_mode.py` erzwingt, dass jeder im Code benutzte INFO-Tag in genau einer der beiden Listen steht — sonst verschwände eine neue Meldung stillschweigend aus dem Terminal. `!debug on|off` (`cogs/basic.py`) schaltet zur Laufzeit via `set_console_mode()` um. **`bot.log` bekommt immer die volle Diagnose** — der File-Handler wird nie gefiltert.
`_VoiceReconnectFilter` (attached to the `discord.voice_state` logger) downgrades the noisy code-1006 reconnect message from ERROR to INFO and strips its traceback — discord.py auto-reconnects on idle channels are expected, so they're logged quietly instead of as a red stack trace.

## Autoplay

Toggled via `🔁 Autoplay` button. `_prefetch_autoplay` starts at song-begin (only if queue empty): fetches YouTube Mix (`list=RD{video_id}`) via `autoplay_ydl` (max 10 entries), wählt via `choose_autoplay_candidate()` **gewichtet vorne** (Gewichte 4/3/2/1 über die ersten vier gefilterten Kandidaten — YouTubes Mix-Ranking ist die Genre-Kohärenz, Platz 1 ist bewusst wieder erlaubt), downloads chosen candidate, appends to queue. On song end `play_next` waits up to 60 s for the prefetch task; falls back to `autoplay()` (same lookup, no pre-download) if needed. Der aufgelöste Kandidat erscheint in `!q` als Vorschau-Zeile „🔮 Als Nächstes (Autoplay)" statt als nummerierter Eintrag.

Reference track: `current_track` → `last_played`. Autoplay stays on until button pressed again — not one-shot.

Die Kandidatenauswahl (nur Videos, `is_seen`-Filter, Fallback-Kaskade, gewichtete Wahl) lebt einmal als Modul-Funktionen in `downloader.py` (`entry_url`, `is_video`, `is_seen`, `select_autoplay_candidates`, `choose_autoplay_candidate`) und wird von beiden Pfaden genutzt — `MusicCommands.autoplay()` (Sofort-Pfad) und `Downloader.prefetch_autoplay()` (Hintergrund-Pfad). Kaskadenstufe 2 (alles gesehen) schließt neben der Video-ID des Referenz-Tracks auch dessen **Varianten** aus — sonst hebelte die Kaskade den Varianten-Filter wieder aus.

`_autoplay_queued_url`: URL last added by autoplay; cleared when popped by `play_next` or evicted by `_evict_autoplay_song()`.

`_recently_played`: `deque(maxlen=15)` of URLs. `_recently_played_titles`: `deque(maxlen=15)` of normalized titles via `normalize_title()` (defined in `utils/text.py`, dependency-free/unit-tested, re-exported from `downloader.py`): bevorzugt das `" | "`-Segment mit `" - "` (Artist-Trenner), sonst den **ganzen** Titel; strippt Klammer-Suffixe wie "(Official Video)" und `feat.…`, Sonderzeichen → Trenner, lowercase, Wörter alphabetisch sortiert — "AHA Take on Me" und "Take on Me AHA" ergeben denselben Key. Der Duplikat-Check (`is_seen`) matcht per **bidirektionalem Overlap** `|A∩B|/min(|A|,|B|) ≥ 0,6` gegen die Historie; zusätzlich gelten Kandidaten mit Varianten-Schlagwort im Roh-Titel (`cover|live|remix|sped up|nightcore|slowed|reverb|acoustic|instrumental|karaoke|8d`, auch in Klammern → `has_variant_keyword`/`title_core_words`) als Duplikat, wenn ihre Kern-Wortmenge (inkl. Klammer-Inhalt, dort steht der Original-Künstler) mit einem Historien-Titel überlappt — „Africa (Toto Cover) - Alex Melton" blockt nach „Toto - Africa". Fallback-Kaskade wie oben.

**`!p` with autoplay active** — `_evict_autoplay_song()` cancels prefetch, removes autoplay URL from queue, inserts new song at front (`appendleft`). Playlist additions evict but append at end.

## Radio

Radio code lives in `cogs/music_radio.py` (`RadioMixin`). `RADIO_STATIONS_FILE` = `radio_stations.json` (key → `{name, url}`).
State: `is_radio`, `radio_station_name`, `radio_stream_url`, `_radio_reconnect_count`.
`_play_radio_stream()` plays via FFmpeg directly (no yt_dlp); `after_radio` reconnects up to 3× on error.
`!radio <Nr|Name>` → aus Liste. `!radio <url> [Name]` → spielt + speichert automatisch (kein Duplikat).
`!stop` beendet Radio oder aktuelle Wiedergabe (Queue bleibt erhalten). Radio-Modus und Song-Modus schließen sich gegenseitig aus.

## Security

Grundsatz (Flag-Defaults = Altverhalten, `allowed_mentions=none()`-Ausnahme, Owner-Garantie, Check-Verhalten in Tests) → [ADR 0004](adr/0004-security-flags-default-altverhalten.md). Die Flags:

- **`URL_VALIDATION`** = `off` | `warn` (Default) | `block` — SSRF-Schutz (`utils/url_check.py`) für `!radio <url>` und `!next url||titel`. Nur http/https; Hostname darf nach DNS-Resolve nicht auf private/loopback/link-local IPs zeigen. `warn` spielt wie bisher + Warnung in Log/Channel; `block` lehnt mit i18n-Meldung ab. Nicht auflösbare Hosts gelten als ok (Stream scheitert ohnehin). `!radio` persistiert neue Sender erst **nach** erfolgreichem Stream-Start (`_play_radio_stream` → `bool`).
- **`REQUIRE_SAME_VOICE`** = `false` (Default) | `true` — Wiedergabe-steuernde Commands (`!s`, `!stop`, `!clear`, `!eq`, `!seek`, `!now`, `!remove`, `!move`, `!shuffle`) erfordern denselben Voice-Channel wie der Bot (`utils/checks.py` → `require_same_voice()`). Bot nicht in Voice → kein Check.
- **`ADMIN_ROLE_ID`** = leer (Default = kein Gating) | Rollen-ID — `!radio delete/rename` (inline `check_admin()`), `!reloadcookies`, `!format` nur für diese Rolle oder den Owner.
- **`!loadq`** validiert ohne Flag (nur Fehlerpfad geändert): Datei muss Liste von `[url, titel]`-String-Paaren sein (`_is_valid_playlist` in `music_queue_io.py`), max. `HARD_PLAYLIST_LIMIT` Einträge — sonst i18n-Fehlermeldung statt Traceback. Valide saveq-Dateien laden unverändert.

## Sprachsteuerung ("yo bot, spiel mal ...")

Details und Begründungen → [ADR 0011](adr/0011-sprachsteuerung-ein-parser-zwei-eingaenge.md).

**Ein Parser, zwei Eingänge.** `VoiceListen.handle_text()` (`cogs/voice_listen.py`)
ist die einzige Stelle, durch die ein gesprochener Satz läuft:

```
Bot B im Voice-Channel?  ──ja──▶  Bot B transkribiert ohnehin
                                  └─▶ POST /command ──┐
                                                      ├──▶ handle_text() ──▶ !p / !s / !x / ...
   ──nein──▶  eigenes Zuhören (voice-recv + Whisper)  ──┘
```

Sitzt Bot B im Channel, hört dieser Bot **nicht** selbst zu: er transkribiert
bereits mit `faster-whisper-medium`, ein zweites Modell daneben wäre doppelte
Arbeit und doppelter VRAM. Erkannt wird er über die konfigurierte
`DM_BOT_USER_ID`, nie per Heuristik.

**Parser** (`utils/nl_parser.py`, rein und ohne Discord-Import): Weckwort-Regex
(tolerant gegenüber Whispers Schreibweisen — "Yo, Bot!", "Jo Bott.", "Yobot"),
dann eine Intent-Tabelle von spezifisch nach generisch. Die Songsuche steht als
Catch-all ganz unten, weil fast jeder Steuersatz ebenfalls ein Spiel-Verb
enthält ("mach mal aus", "spiel weiter"). Die Rückgabe ist ein Tupel
`(weckwort_gefunden, intent)` — ohne Weckwort schweigt der Bot, mit Weckwort
aber ohne Intent stellt er eine Rückfrage.

**Ausführung:** synthetische Message nach dem Muster aus `views/help_view.py`
(Referenz-Nachricht kopieren, Autor auf den Sprecher, Inhalt auf den Befehl,
dann `bot.get_context` + `bot.invoke`). Damit laufen alle bestehenden Checks
mit — Sprache ist kein zweiter, laxerer Weg in den Bot hinein.

**Flags:** `VOICE_CONTROL` (Master, Default aus — ohne ihn existiert
`/command` gar nicht), `VOICE_OWN_LISTEN` (eigenes Zuhören, braucht Neustart),
`DM_BOT_USER_ID`, `VOICE_WAKE_WORDS`, `VOICE_BLOCKED_USER_IDS`. Zusätzlich
muss `!listen on` zur Laufzeit geschaltet werden; die Nachricht dient als
Referenz für die Command-Ausführung.

**Eigenes Zuhören** (`VOICE_OWN_LISTEN=true`, Neustart nötig): `SpeechSink`
(`cogs/voice_sink.py`) puffert dekodiertes PCM pro Sprecher in
`SpeakerBuffers` (`utils/speech_buffer.py`). Ein Segment gilt als fertig,
wenn 800 ms kein Paket mehr kam — bewusst über die Paketlücke statt über die
`speaking`-Events, die Clients unzuverlässig setzen. `_flush_loop` (200 ms)
holt fertige Segmente ab, ein Rauschgate bei −50 dBFS verwirft Tastaturklicks
vor der GPU, `utils/pcm.py` rechnet 48 kHz Stereo auf 16 kHz Mono float32 um
(Faktor exakt 3, reine numpy-Mittelung), und `utils/stt.py` transkribiert in
`asyncio.to_thread`.

**Die Weiche:** Betritt der DM-Bot den Channel, wird sofort abgeschaltet und
das Modell aus dem VRAM freigegeben. Geht er, wird erst nach 10 s Karenz
wieder geladen — asymmetrisch, weil Abschalten gratis ist und Anschalten
~20 s Ladezeit und 2,5 GB VRAM kostet.

**Logging:** `[Sprachbefehl]` (essential, mit Quellen-Präfix `(bridge)`/`(eigen)`)
für alles mit Weckwort, `[STT]` (quiet) für Transkripte ohne Weckwort.

## Key Bot Commands

Full list via `!help`. Non-obvious:

- **`!loop`** — cycles `loop_mode`: `None` → `"song"` → `"queue"` → `None`; handled in `after_playing`
- **`!now <n>`** — integer argument moves queue entry at position `n` to front and skips current song; non-integer falls through to search/URL logic
- **`!text`** — lyrics via lyrics.ovh, parses "Artist - Title" from YouTube title
