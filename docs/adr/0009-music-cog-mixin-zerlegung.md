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
