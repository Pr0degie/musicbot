# ADR 0009: Music-Cog-Mixin-Zerlegung + `play_next`-Orchestrator

- **Status:** Akzeptiert
- **Datum:** 2026-07-13

## Kontext

`cogs/music.py` war auf ~1915 Zeilen angewachsen, dominiert von einem `play_next`-Monolithen (~512 Zeilen) mit tief verschachteltem `after_playing`-Callback. Die Datei mischte Queue-/EQ-/Autoplay-Kern, den kompletten Playback-Flow, Voice-Lifecycle (Join/Leave/Idle/Watchdog) und den Fortschrittsbalken. Das erschwerte Navigation und Review; jede Änderung am Playback berührte dieselbe Riesenfunktion.

Erschwernis: `play_next` ist race-kritisch (ADR 0007). Der komplette Track-Zustand wird **synchron, ohne await dazwischen** vor `vc.play()` gesetzt; nach jedem Post-Play-await folgt ein Generationscheck. Der `after_playing`-Callback hält eine feste interne Reihenfolge (Progressive-Resume-Block **vor** dem Retry-/Resume-Marker-Reset, ADR 0001). Eine naive Zerlegung, die await-Grenzen verschiebt oder einen Generationscheck in eine Untermethode zieht, würde die Race-Semantik still brechen.

## Entscheidung

- **Mixin-Schnitt:** Der Playback-Kern zieht in `cogs/music_playback.py` (`PlaybackMixin`), Voice-Lifecycle und Fortschrittsbalken in `cogs/music_voice_ui.py` (`VoiceLifecycleMixin`, `PlaybackUiMixin`). `MusicCommands` in `music.py` erbt alle Mixins und hält weiterhin den Instanz-State und die statischen Helfer.
- **Klassen-Aliase als Test-Patch-Punkte:** `music.py` behält `_ffmpeg_header_opts`/`_stderr_tail`/`_classify_ffmpeg_error`/`_progress_bar` als `staticmethod`-Aliase auf die Funktionen in `utils/`. So bleiben bestehende `self.`-Aufrufe und Test-Patch-Punkte (`MusicCommands.<name>`) stabil.
- **Verbatim-erst-dann-Zerlegen in zwei Prompts:** Zuerst wurde `play_next` unverändert in das neue Mixin verschoben (Prompt 01–03), dann intern zerlegt (Prompt 04). Der `play_next`-Orchestrator delegiert an vier Untermethoden:
  - `_handle_queue_empty` (async) — Leere-Queue-Fall inkl. Autoplay-Handoff; wird sofort awaitet.
  - `_build_audio_source` (synchron) — FFmpeg-Options, stderr-Temp-Datei, Source-Konstruktion.
  - `_snapshot_track_state` (synchron) — der Track-Zustand-vor-`play()`-Block (ADR 0007); gibt `(generation, is_loop_repeat, prev_track)` zurück.
  - `_make_after_playing` (synchron) — Factory, die den `after_playing`-Callback verbatim baut; vormalige Closure-Variablen sind jetzt Parameter.

## Konsequenzen

- **`play_next`-Untermethoden dürfen await-Grenzen nicht verschieben.** Ein synchroner Block darf nur in eine synchrone Methode wandern (normaler Call); ein bereits awaiteter Block nur in eine async-Methode, die an exakt derselben Stelle awaitet wird. Die drei Post-Play-Generationschecks bleiben im Orchestrator an ihren Positionen.
- **Neue Post-Play-Schritte gehören in den Orchestrator, nicht in die Untermethoden** — nur dort ist der Generationscheck nach dem vorangehenden await sichtbar und erzwingbar.
- Die Untermethoden sind bewusst dünn und ohne eigene Race-Logik: `_snapshot_track_state` und `_build_audio_source` bleiben synchron, `_make_after_playing` erzeugt nur die Closure (kein Event-Loop-Kontakt).

**Stand 2026-10-25:** `play_next` delegiert zusätzlich an `_build_np_embed` (synchron, Now-Playing-Embed; seit 2026-11-13 `_build_np_view`, Components-V2-Karte), `_schedule_background_work` (synchron, Wartung/Prefetch/Autoplay-Prefetch, nach dem letzten Generationscheck) und `_skip_broken_track` (async, aus den `except`-Zweigen). Die drei Post-Play-Generationschecks stehen weiterhin im Orchestrator.

**Stand 2026-11-22:** `_make_after_playing` baut zwei Closures: `after_playing` (der Callback im Player-Thread von discord.py) reicht nur noch weiter, `on_track_end` enthält die unveränderte Logik und läuft auf dem Event-Loop (`_run_on_loop`). Im Thread bleiben nur das Lesen des FFmpeg-stderr-Puffers und der `last_queue.json`-Write. Queue, `_seek_offset` und die Retry-Marker werden damit nicht mehr aus einem Fremd-Thread verändert. Ein pausierter Player, den `play_next` durch einen neuen Track ersetzt, wird über `_discard_paused_player` gestoppt; sein Callback erkennt das am `PlayerToken` (`utils/voice.py`) und fasst keinen Wiedergabe-Zustand mehr an. `_seek_offset` ist an die URL seines Tracks gebunden (`_seek_offset_url`, konsumiert in `_take_seek_offset` beim Entnehmen aus der Queue).

**Stand 2026-11-22 (Zerlegung von `music_playback.py`):** `PlaybackMixin` erbt drei weitere Mixins, `MusicCommands` bindet weiterhin nur `PlaybackMixin` ein: `EnqueueMixin` (`cogs/music_enqueue.py`, Such-/URL-Flow von `!p`/`!next`/`!now`), `PrefetchMixin` (`cogs/music_prefetch.py`) und `TrackEndMixin` (`cogs/music_track_end.py`). Die elf Positionsparameter von `_make_after_playing` ersetzt das Wertobjekt `TrackRun` (frozen dataclass), das `play_next` direkt vor `vc.play()` baut. `_make_after_playing(run)` erzeugt nur noch den dünnen Thread-Callback. Die Logik ist die Methode `_on_track_end(run, error, ended_at, stderr_tail)` mit benannten Schritten in fester Reihenfolge: `_log_early_death` → `_resume_growing_file` → `_retry_dead_stream` → `_clear_markers_after_clean_end` → `_snapshot_queue` → `_requeue_for_loop` → `_start_next`. Der Resume-Schritt steht vor dem Marker-Reset (ADR 0001); der `elif`-Zweig in `_retry_dead_stream` läuft wie zuvor auch dann, wenn schon ein Resume eingereiht hat. Die zwei Stellen, die alle vier Retry-Marker zurücksetzen, rufen `_clear_retry_markers()`; die zwei Stellen, die nur `_force_download_url` und `_stream_retry_url` zurücksetzen, bleiben inline. In `play_next` liefert `_advance_block_reason()` den Grund der drei Gates (Radio, DM-Bridge, Voice-Reparatur, gleiche Reihenfolge und Log-Texte). `_snapshot_track_state` gibt nur noch `(generation, is_loop_repeat)` zurück, der dritte Wert `prev_track` war unbenutzt. Den Karten-Übergang erledigen `_reset_card_for_loop_repeat` und `_retire_previous_cards`, beide an derselben Stelle awaitet; die drei Generationschecks stehen weiterhin im Orchestrator. Der erste Check folgt jetzt ohne Bedingung auf `_retire_previous_cards`: Gibt es keine alte Karte, läuft die Methode ohne await durch, und der Zähler kann sich nicht geändert haben.
