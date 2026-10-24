# Wiedergabe, Queue & Download

[← Architektur-Übersicht](../architecture.md)

## Music Playback Flow

`play_next` ist ein Orchestrator in `PlaybackMixin` (`cogs/music_playback.py`) und delegiert an
Untermethoden → [ADR 0009](../adr/0009-music-cog-mixin-zerlegung.md): `_handle_queue_empty` (Leere-Queue-Fall
+ Autoplay-Handoff), `_build_audio_source` (FFmpeg-Options + stderr-Temp + Source, synchron),
`_snapshot_track_state` (Track-Zustand vor `vc.play()`, synchron), `_make_after_playing` (Factory, die den
`after_playing`-Callback baut), `_build_np_embed` (Now-Playing-Embed, synchron), `_schedule_background_work`
(Wartung alle 50 Songs, Prefetch, Autoplay-Prefetch, synchron) und `_skip_broken_track` (Fehlerpfad:
merken, melden, nächsten Track anstoßen). Alle Generationschecks bleiben im Orchestrator.

1. `after` callback (aus `_make_after_playing`) uses `run_coroutine_threadsafe()` to call `play_next`. Errors
   skip via `asyncio.create_task()` — not direct recursion (avoids stack overflow on many bad URLs).
2. `last_queue.json` written after each track — intentionally not reloaded on startup.
3. Queue empty → `play_next` delegiert an `_handle_queue_empty`: sets `is_playing = False`, saves
   `current_track` → `last_played`, clears `current_track`, triggers autoplay if enabled. Bot stays in voice
   channel (but see idle-leave below).

**Race-Schutz & Stream-Retry:** `play_next` setzt den Track-Zustand synchron in `_snapshot_track_state` vor
`vc.play()` und prüft danach nach jedem Post-Play-await (im Orchestrator, nicht in den Untermethoden) den
Generationszähler `_track_generation` → [ADR 0007](../adr/0007-track-zustand-vor-play-generationszaehler.md).
FFmpeg-stderr läuft in eine Temp-Datei; der `after_playing`-Callback (gebaut von `_make_after_playing`) loggt
bei Fehler/Kurzläufer (<2 s) die letzten 20 Zeilen und klassifiziert sie (`_classify_ffmpeg_error`: `input`
vs. `filter`). Stream-Track <1 s + Input-Fehler (z. B. 403 vom CDN) → genau **ein** Retry mit frischer URL
(`_stream_retry_url`, `dl.invalidate()`), kein doppelter Play-Count; schlägt auch der fehl, folgt **ein**
letzter Anlauf als lokaler Download (`_force_download_url` → `resolve_track(force_download=True)`;
yt_dlp-eigener HTTP-Client, den CDN-403s gegen FFmpeg nicht treffen; Meldung
`error.stream_download_fallback`). Liefert der Fallback keine Datei (>20-min-Tracks werden nie
heruntergeladen) oder stirbt auch die Datei sofort → Aufgeben mit `error.stream_giveup`. Alle
Retry-/Resume-Marker (`_stream_retry_url`, `_force_download_url`, `_progressive_resume_url`/`-count`) leben
bis zum normalen Track-Ende bzw. Trackwechsel; der Marker-Reset läuft **nach** dem Progressive-Resume-Block
(sonst griffe dessen Kappe nie). Diese Kaskade ist heute nur noch Fallback — Primärpfad für ungecachte kurze
Tracks ist der progressive Download → [ADR 0001](../adr/0001-progressiver-download-statt-cdn-stream.md). Der
Stream-Pfad reicht `info["http_headers"]` via `-headers` an FFmpeg durch.

**Now-Playing-Karte:** Embed samt Buttons gibt es **nur für den aktuellen Song**. Jede Nachricht, die ein
Track hinterlässt, geht durch `_retire_np_message(msg, title)` (`PlaybackUiMixin`) und wird zur Textzeile `🎶
Titel` — beim Trackwechsel, beim Queue-Ende (`_ended_np`), bei der Radio-Übernahme und bei der 429-Abschaltung
(`_disable_np_edits` übergibt die Nachricht an `_ended_np`, statt sie zu vergessen). Zwei Fallstricke, die
dabei je eine Karte für immer stehen ließen: `now_playing_msg` und `_ended_np` können **gleichzeitig** belegt
sein (beide zurückbauen, nicht `elif`), und der Inhalt darf nie leer werden — eine Nachricht ohne Text, Embed
und Anhang lehnt Discord mit 400 ab, der Rückbau schlägt still fehl. Der Titel dafür steht in `_np_title` (mit
der Karte gesetzt), nicht in `last_played`/`prev_track`, die zum Rückbauzeitpunkt schon geleert sein können.

Background tasks: `prefetch_task` downloads the next two queued songs sequentially (`_prefetch_next(0)` then
`_prefetch_next(1)` — sequential because yt_dlp is not thread-safe); `_autoplay_prefetch_task` searches +
downloads next autoplay song while current plays.

`_url_cache` on `self.dl`: URL → yt_dlp info-dict. All three callers cache the **full** info-dict (with `ext`,
`webpage_url`) so `prepare_filename()` works. `autoplay_ydl` yields shallow playlist entries;
`prefetch_autoplay()` upgrades via `ydl.extract_info(url, download=True)`. `update_ydl()` keeps only entries
still in queue/`current_track`; `clear()` wipes entirely. Persisted to `metadata_cache.json` — survives bot
restarts; persistiert wird ein reduzierter Feldsatz → [ADR 0005](../adr/0005-persistenz-debouncing.md).

## Streaming vs. Download

`STREAM_THRESHOLD_SECONDS = 20 * 60` (in `downloader.py`). `resolve_track()` returns a direct CDN URL (`str`,
not `Path`) in two cases — **this is intentional, not a bug**:

- Duration > 20 min → always stream, never download.
- File not locally cached **und der progressive Pfad greift nicht** (Gates → [ADR
  0001](../adr/0001-progressiver-download-statt-cdn-stream.md)) → stream; `prefetch_next()` downloads queue
  songs in background.

Der progressive Download ist der Primärpfad für ungecachte Tracks ≤ 20 min; Puffer, Gates, Buchhaltung,
Resume, `_stop_for_advance`, Blockliste und Schnellstart → [ADR
0001](../adr/0001-progressiver-download-statt-cdn-stream.md).

**Sofortstart des ersten Treffers:** Läuft gerade nichts, stößt `prime_first_hit()` den progressiven Download
des ersten Suchtreffers (bzw. der `!p <url>`-Eingabe) sofort an — er läuft damit parallel zu den
Discord-Nachrichten statt hinter ihnen; `resolve_track` adoptiert den laufenden Task über die
In-Flight-Registry → [ADR 0010](../adr/0010-cookielos-zuerst-cookie-fallback.md).

**Prefetch-Kick:** Titel, die mitten im Song eingereiht werden (`!p`/`!next`/`!loadq`), starten via
`_kick_prefetch()` sofort den Vorlade-Task (`_prefetch_upcoming`, von `play_next` wiederverwendet) — sonst
entstünde beim Übergang die volle Extraktions-+Pufferlatenz. Bewusst kein Kick bei `!now` (spielt sofort; ein
Prefetch auf denselben Song würde `resolve_track` auf den kompletten Download warten lassen statt progressiv
zu starten).

**Kein globales Idle-Warten:** `_prefetch_upcoming` gönnt dem progressiven Download des laufenden Songs nur
eine kurze Startphase (`wait_progressive_idle(timeout=15.0)`), dann lädt der Queue-Prefetch sequenziell los.
Das frühere unbegrenzte Warten (bis 300 s) parkte den Task praktisch immer — der laufende Song lädt fast immer
progressiv — und machte alle `_kick_prefetch`-Aufrufe zu No-Ops.

**Queue-Umbau:** `!shuffle`/`!move`/`!remove` und der Alternativ-Button (Queue-Ersatz) rufen
`_restart_prefetch()` — laufenden Sammel-Task canceln (ein bereits schreibender Download-Worker wird adoptiert
und läuft zu Ende → [ADR 0002](../adr/0002-inflight-registry.md), Cancel-Pfad) und neu kicken, weil der alte
Task auf die alte Reihenfolge zielt. `_kick_prefetch` allein wäre wegen seines No-Op-Guards wirkungslos. Die
Sequenzialität der Prefetch-Downloads (yt_dlp ist nicht thread-safe) sichert ein Lock um den Download-Aufruf
in `_prefetch_one`.

`play_next` detects streams via `isinstance(filename, str)` → adds FFmpeg reconnect options, skips
`codec=copy`. Wachsende Dateien (`is_growing` via `dl.is_incomplete`): Wiedergabe- und Resume-Regeln → [ADR
0001](../adr/0001-progressiver-download-statt-cdn-stream.md).
`prefetch_next()` skips download for videos > 20 min.

## Persistenz & Dauerbetrieb (schwache Hardware)

Grundsatz „kein blockierendes File-I/O im Event-Loop", Debounce-Loop, Flush-Garantien und reduzierter
Metadaten-Cache → [ADR 0005](../adr/0005-persistenz-debouncing.md). Windows-sichere Löschungen (`safe_unlink`,
Pending-Delete-Liste, `.inprogress`-Sidecar) und die `!restart`-Mechanik → [ADR
0003](../adr/0003-windows-native-zielplattform.md).

- **`DOWNLOADS_MAX_MB`** (`.env`, Default 0 = aus = heutiges Verhalten): nach jedem Download löscht
  `dl.cleanup_downloads()` (to_thread) die ältesten Dateien (mtime) aus `downloads/`, bis das Limit passt.
  Tabu: Songs in Queue/`current_track` (`protected_provider` aus `music.py`; Schutz über
  Cache→`prepare_filename` **und** Titel-Stem), `dl.last_resolved_file` (aktive FFmpeg-Quelle, aus dem letzten
  `resolve_track`-Ergebnis) und der frisch geladene Autoplay-Song (`extra_protected`). Fehler werden
  geschluckt — Cleanup löscht im Zweifel lieber nichts.
- **HTTP:** `lyrics_cmd` nutzt die geteilte `self._http_session` (cog_load→cog_unload; `_http()` erstellt bei
  geschlossener Session eine neue).

## Audio Configuration

Default format: `webm`. Default EQ preset: `punchy`. Filter chains in `cogs/presets.py` (`EQ_PRESETS`).
Presets: `bassboost`, `flat`, `vocalboost`, `superbass`, `punchy`, `nightcore`, `karaoke`, `8d`.
`!eq` mid-song: restarts current track with new filter (prepends to queue, calls stop).
`!format mp3|webm` or `!eq <preset>` → `update_ydl()` → `self.dl.rebuild()` recreates all five instances.
FFmpeg filter notes and "do not add" list → comments at top of `cogs/presets.py`.
