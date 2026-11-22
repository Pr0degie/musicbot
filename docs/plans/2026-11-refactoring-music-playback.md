# Plan: `cogs/music_playback.py` zerlegen und vereinfachen

> **Für die ausführende Session:** Dieser Plan entstand am 22.11.2026 aus einer Lese-Analyse der Datei
> (ein Analyse-Agent, keine Tests ausgeführt) plus fünf Fragerunden mit dem Nutzer. Alle
> Datei-/Zeilenangaben beziehen sich auf HEAD `ababaed`. Zeilennummern vor der Umsetzung per grep
> verifizieren — sie verschieben sich mit jeder Etappe. Etappen strikt in Reihenfolge abarbeiten. Nach
> jeder Etappe: `ruff check .`, der bei der Etappe genannte gezielte Testlauf, Commit direkt auf main
> (deutsche Commit-Message im Stil der Historie, keine KI-Tagline). Die volle `pytest`-Suite und die
> manuelle `docs/smoke-checklist.md` laufen **einmal am Ende des Plans**, nicht nach jeder Etappe.
> `main.py` nie importieren (startet den Bot).
>
> **Reihenfolge der zwei Refactoring-Pläne:** zuerst
> [`2026-11-refactoring-downloader.md`](2026-11-refactoring-downloader.md), danach dieser Plan.
>
> **Stand 22.11.2026:** Etappen 1–6 sind umgesetzt (`e73f3df`, `856e90e`, `e6f5fca`, `126b511`, `8f8a432`,
> `99cfa34`), volle Suite und `ruff` grün. Ergebnis: `music_playback.py` 537 Zeilen, `music_enqueue.py` 241,
> `music_track_end.py` 275, `music_prefetch.py` 98. Abweichungen vom Plan: `_run_on_loop` und
> `_LOOP_HANDOFF_TIMEOUT` heißen in `utils/tasks.py` `run_on_loop` und `LOOP_HANDOFF_TIMEOUT`; für Etappe 6
> kam ein Test dazu (`tests/test_radio_station_switch.py`, Stream-Ende aus einem fremden Thread); die
> `!loadq`-Stelle aus 5a bleibt inline, weil sie vorher den ersten Titel primt. Offen: die manuelle
> Smoke-Checkliste (besonders der Radio-Abschnitt wegen Etappe 6), Etappe 7 (Einzelfreigabe) und die Bugs
> aus dem Anhang.

## Ziel

`cogs/music_playback.py` (1119 Zeilen, größte Datei im Repo) soll lesbarer und kleiner werden, ohne dass
sich das Verhalten des Bots ändert. Die Datei enthält drei Themen, die nicht zusammengehören: den
`play_next`-Kern, die Track-Ende-Logik und den Such-/Einreih-Flow von `!p`/`!next`/`!now`. Dazu kommt ein
kleines Prefetch-Subsystem. Der eigentliche Brennpunkt ist `on_track_end`: 164 Zeilen als Closure in einer
Factory mit elf Positionsparametern.

Ergebnis nach Etappe 4: `music_playback.py` etwa 480–500 Zeilen, drei neue Module mit etwa 300, 240 und
100 Zeilen.

## Nutzer-Entscheidungen (bindend)

| Thema | Entscheidung |
|---|---|
| Prioritäten | Lesbarkeit, Laufzeit-Effizienz, kleinere Dateien. Testbarkeit ist kein eigenes Ziel |
| Verhalten | **Strikt verhaltensgleich** in den Etappen 1–5. Etappe 6 ändert Timing im Radio-Pfad und ist vom Nutzer freigegeben. Etappe 7 ist optional und braucht eine Einzelfreigabe |
| Struktur | **Erst Variante A** (Mixin-Schnitt plus Wertobjekt `TrackRun`). Variante B (eigene Klassen, Komposition) ist als Folgeprojekt ausgearbeitet, siehe unten |
| Modulnamen | `cogs/music_enqueue.py`, `cogs/music_track_end.py`, `cogs/music_prefetch.py` |
| Umfang | `music_playback.py` plus neue Module. Zusätzlich freigegeben (Etappen 5 und 6): Helfer für `cogs/music.py`, Umzüge nach `music_queue_io.py` und `views/music_controls.py`, `after_radio` auf den Loop |
| Tests | Nur Import- und Patch-Pfade anpassen. Keine inhaltlichen Teständerungen, Testdateien nicht aufteilen |
| Schrittgröße | Wenige größere Etappen, jede mit eigenem gezielten Testlauf |
| Zielgröße | Kein festes Zeilenlimit, geschnitten wird entlang der Themen |
| Docstrings | Umbau-Historie („verbatim aus play_next herausgezogen" usw.) entfernen, Docstrings beschreiben den Ist-Zustand. Die Geschichte bleibt in ADR 0009 |
| Bugs | Gefundene Bugs stehen im Anhang. Behoben werden sie **nach** dem Refactoring, je ein Fix-Commit mit Test |
| Effizienz | Befunde, die Verhalten ändern, kommen in die optionale Etappe 7; vorher wird gemessen |
| Absicherung | Gezielte Tests pro Etappe, volle Suite und Smoke-Checkliste einmal am Ende |
| Umsetzer | Claude in neuen Sessions |

## Ausgangslage

Herkunft der Angaben: Zieldatei, die Nachbarn `cogs/music.py`, `cogs/music_*.py`, `utils/voice.py`,
`views/music_controls.py`, `CLAUDE.md`, `docs/architecture/playback.md` und die ADRs 0001, 0007, 0009, 0012
wurden gelesen. Die Methodenliste wurde zusätzlich per grep gegengeprüft und stimmt. Kein Test wurde
ausgeführt; die Angaben zu Testlücken stammen aus grep, nicht aus einem Coverage-Lauf.

### Strukturkarte (Stand `ababaed`)

**Modulebene**

| Zeilen | Name | Verantwortung |
|---|---|---|
| 1–25 | Docstring | zählt Methoden und State auf, teils veraltet („nested after_playing") |
| 45–64 | Konstanten | `PROGRESSIVE_MAX_RESUMES`, `_YTDLP_FAILED`, `_BACKGROUND_TASKS`, `_LOOP_HANDOFF_TIMEOUT`, `_LAST_QUEUE_LOCK` |
| 67–91 | `_run_on_loop` (25) | Übergabe Player-Thread → Event-Loop mit Timeout |
| 94–96 | `search_query` (3) | baut `ytsearch3:` |
| 99–107 | `start_early` (9) | Task sofort starten, Ergebnis schlucken |

**`PlaybackMixin`, nach Themen**

| Thema | Zeilen | Methode (Länge) | Verantwortung |
|---|---|---|---|
| Kern | 230–421 | `play_next` (192) | Orchestrator: Gates, Resolve, Start, Karte, Generationschecks |
| Kern | 853–906 | `_snapshot_track_state` (54) | Track-Zustand synchron vor `vc.play()` |
| Kern | 563–616 | `_build_audio_source` (54) | FFmpeg-Optionen, stderr-Temp, Source |
| Kern | 505–561 | `_handle_queue_empty` (57) | Cleanup, `_ended_np`, Idle-Timer, Autoplay-Handoff |
| Kern | 423–456 | `_build_np_view` (34) | Now-Playing-Karte bauen |
| Kern | 458–486 | `_schedule_background_work` (29) | Wartung alle 50 Songs, Prefetch-Tasks starten |
| Kern | 488–503 | `_skip_broken_track` (16) | Fehlerpfad: merken, melden, weiter |
| Kern | 119–129 | `_take_seek_offset` (11) | URL-gebundenen Seek-Offset konsumieren |
| Kern | 187–214 | `_stop_for_advance` (11), `_discard_paused_player` (16) | die zwei Stopp-Pfade |
| Kern | 216–228 | `_resolve_track` (13) | dünner Wrapper um `dl.resolve_track` |
| Track-Ende | 618–828 | `_make_after_playing` (211) | Factory; enthält `on_track_end` (630–793, 164) und `after_playing` (795–826, 32) |
| Track-Ende | 835–851 | `_write_last_queue` (17) | `last_queue.json` im Player-Thread schreiben |
| Prefetch | 131–185 | `_prefetch_next` (3), `_prefetch_upcoming` (13), `_kick_prefetch` (14), `_restart_prefetch` (10), `_prefetch_autoplay` (11) | Queue- und Autoplay-Vorladen |
| Prefetch | 908–930 | `_evict_autoplay_song` (23) | Autoplay-Song aus der Queue nehmen, Task abbrechen |
| Einreihen | 932–948 | `_stop_radio_for_takeover` (17) | Radio beenden, bevor ein Song übernimmt |
| Einreihen | 950–1007 | `_extract_info_or_report` (41), `_report_after_status` (7), `_delete_status` (8) | yt_dlp-Abfrage mit Status- und Fehlermeldung |
| Einreihen | 1009–1093 | `_search_and_enqueue` (85) | Suchzweig: Treffer einreihen, Alternativen anbieten |
| Einreihen | 1095–1119 | `_fetch_single_track_info` (25) | URL-Zweig von `!next`/`!now` |

### Brennpunkte

1. **`on_track_end`, `:630–793`.** Sieben Aufgaben hintereinander in einer Closure: Log bei Frühtod,
   Progressive-Resume, Stream-Retry-Kaskade, Marker-Reset, Queue-Snapshot, Loop-Requeue, nächsten Track
   anstoßen. Bis zu drei `if`-Ebenen in Closure in Methode (694–711). Das `elif` in 740 hängt an der
   Bedingung von 717 und läuft auch, wenn `retry_scheduled` schon wahr ist; das übersieht man leicht.
2. **Parameterliste der Factory, `:618–619` und Aufruf `:336–339`.** Elf Positionsparameter, zweimal in
   identischer Reihenfolge zu pflegen.
3. **`play_next`, `:230–421`.** Drei fast gleiche Gate-Blöcke (232–254), zwei fast gleiche Abbruch-Blöcke
   (341–357, nur einer erhöht die Generation), dann der Karten-Übergang mit zwei von drei
   Generationschecks (372–408).
4. **Verstreuter Zustand.** Die Datei liest oder schreibt rund 35 Attribute auf `self`. Auffällig:
   - Die vier Retry-Marker werden an vier Stellen zurückgesetzt, zweimal alle vier (752–755, 900–903),
     zweimal nur zwei (319–320, 742–743).
   - Klassen-Defaults stehen an zwei Stellen, einmal mitten in der Klasse (114–117, 832–833).
   - „Task abbrechen, falls er läuft" steht viermal mit kleinen Abweichungen (170–172, 477–478, 484–485,
     916–918).
5. **Reihenfolge der Methoden.** Sie folgt nicht dem Aufruf: `_snapshot_track_state` (853) steht hinter dem
   Track-Ende, `_handle_queue_empty` zwischen Fehlerpfad und Source-Bau.
6. **Historie in Docstrings.** „Verbatim aus play_next herausgezogen", „vormalige Closure-Variablen", „wie
   zuvor inline" (256–258, 333–334, 509–512, 567–569, 622–628, 636–638, 839, 856).
7. **Totes und Redundantes.**
   - `prev_track` wird in `:361` entpackt und nie benutzt.
   - `_bar != self._np_last_desc` in `:374` ist immer wahr, weil `:892` den Wert kurz davor ohne await
     dazwischen auf `None` setzt.
   - `isinstance(filename, str)` wird zweimal ausgewertet (298, 309).
   - In `_build_audio_source` sind zwei der vier Konstruktor-Aufrufe identisch (599–601, 606–608).

## Zielbild (Variante A)

```python
class PlaybackMixin(TrackEndMixin, PrefetchMixin, EnqueueMixin):
```

Die neuen Mixins hängen über `PlaybackMixin` ein. So bleibt die Basenliste in `cogs/music.py:39–41`
unberührt, `music.py` ändert nur die Import-Zeile 34. Die MRO ist unkritisch, die neuen Klassen tragen keine
Commands.

| Modul | Zieht mit | Zeilen etwa |
|---|---|---|
| `cogs/music_enqueue.py` (`EnqueueMixin`) | 932–1119 plus `_YTDLP_FAILED`, `_BACKGROUND_TASKS`, `search_query`, `start_early` | 300 |
| `cogs/music_prefetch.py` (`PrefetchMixin`) | 131–185, 908–930 | 100 |
| `cogs/music_track_end.py` (`TrackEndMixin`) | 618–851 plus `_run_on_loop`, `PROGRESSIVE_MAX_RESUMES`, `_LOOP_HANDOFF_TIMEOUT`, `_LAST_QUEUE_LOCK`, Klassen-Defaults `_last_queue_seq`/`_last_queue_written` | 240 |
| `cogs/music_playback.py` (bleibt) | der Kern aus der Strukturkarte | 480–500 |

**Wertobjekt `TrackRun`** (frozen dataclass in `music_track_end.py`): `ctx, url, title, duration, filename,
seek_offset, is_stream, is_growing, prog_task, stderr_buf, token`. Es ersetzt die elf Parameter und hat
keinen eigenen veränderlichen Zustand.

Neue Module liegen flach in `cogs/`, kein Unterpaket: `tests/test_no_raw_vc_stop.py`,
`tests/test_voice_guard_move.py` und `tests/test_i18n_keys.py` suchen per `cogs/*.py`. Cogs werden in
`main.py:45–50` explizit registriert, neue Module werden also nicht versehentlich als Cog geladen.

## Regeln für alle Etappen

1. **`discord.FFmpegOpusAudio` immer als Attribut ansprechen**, nie `from discord import …`; die Tests
   patchen das Attribut am Modul `discord`.
2. **`self._stderr_tail`, `self._classify_ffmpeg_error`, `self.play_next`, `self._progress_bar`,
   `self._up_next_title` weiter über `self` rufen**; Tests überschreiben sie auf der Instanz.
3. **Klassen-Defaults wandern mit ihren Methoden.** Kein neues Instanz-Attribut ohne Klassen-Default: Tests
   bauen den Cog per `MusicCommands.__new__` ohne `__init__` (`tests/conftest.py:177`,
   `tests/test_music_playback.py:154`).
4. **Gepatchte Namen liegen im selben Modul wie ihre Nutzer.** `_LOOP_HANDOFF_TIMEOUT`, `_run_on_loop` und
   der Import von `atomic_write_text` müssen dort stehen, wo der Code sie nachschlägt, sonst greifen die
   Patches nicht. Der Fehlschlag wäre laut, nicht still.
5. **Log-Texte und -Tags bleiben wörtlich.** `tools/logsummary.py:85` liest `[Wiedergabe] Starte: `,
   `tests/test_logger_mode.py` klassifiziert jeden INFO-Tag.
6. **Verschiebe-Etappen prüfen mit `git diff --color-moved=dimmed-zebra`:** außer Importen und
   Klassenköpfen darf keine Zeile inhaltlich geändert sein.
7. **Klassen-Aliase in `cogs/music.py:69–73` bleiben**, sie sind Patch-Punkte der Tests.

**Lücken in der Absicherung** (per grep geprüft): Für `_skip_broken_track` und die `except`-Zweige, den
Konkurrenz-Guard `:267–277`, die Radio- und DM-Gates in `play_next`, `_stop_radio_for_takeover` und
`_prefetch_autoplay` wurde kein Test gefunden. Dort trägt nur die Diff-Kontrolle. Die Vereinfachung S1
berührt die Gates, deshalb dort besonders genau lesen.

---

## Etappe 1 — Einreih-Flow und Prefetch verschieben (wörtlich)

**Inhalt**

- `cogs/music_enqueue.py` und `cogs/music_prefetch.py` anlegen, `PlaybackMixin` erbt beide.
- Import in `cogs/music.py:34` und `tests/test_search_status_message.py:13` (`_BACKGROUND_TASKS`) anpassen.

**`self`-Abhängigkeiten der neuen Mixins** (zur Kontrolle, nichts davon ändert sich)

- `EnqueueMixin`: `dl`, `queue`, `is_playing`, `autoplay_enabled`, `is_radio`, `_playback_done`,
  `_stop_radio`, `_evict_autoplay_song`.
- `PrefetchMixin`: `dl`, `queue`, `is_playing`, `current_track`, `prefetch_task`, `_autoplay_prefetch_task`,
  `_autoplay_queued_url`, `_recently_played*`.

**Invarianten:** `stop_playback` in `_stop_radio_for_takeover` (wird von `tests/test_no_raw_vc_stop.py`
automatisch mitgeprüft, weil der Test `cogs/*.py` durchsucht).

**Testlauf**

```bash
pytest tests/test_search_status_message.py tests/test_search_prime.py tests/test_p_single_url_fallback.py \
  tests/test_queue_ops.py tests/test_music_playback.py tests/test_rebuild_on_loop.py \
  tests/test_np_card_controls.py tests/test_queue_select.py tests/test_no_raw_vc_stop.py \
  tests/test_i18n_keys.py tests/test_logger_mode.py -q
```

**Doku:** `docs/architecture.md:28–35`, Landkarte in `CLAUDE.md:48`.

**Abnahme:** `ruff check .` sauber (findet übrig gebliebene Importe), Testlauf grün, Diff zeigt nur
verschobene Zeilen.

## Etappe 2 — Track-Ende verschieben (wörtlich)

**Inhalt**

- `cogs/music_track_end.py` mit unverändertem `_make_after_playing`, `_write_last_queue`, `_run_on_loop` und
  den Konstanten.
- `tests/test_after_playing_on_loop.py:20`: Modul-Alias umstellen (wirkt auf die Patches in Zeile 85, 93,
  186).

**Invarianten:** Marker-Reset nach dem Progressive-Resume-Block (`CLAUDE.md:26`), Datei-I/O außerhalb des
Loops (ADR 0005). Beides bleibt wörtlich.

**Testlauf**

```bash
pytest tests/test_after_playing_on_loop.py tests/test_loop_after_playing.py tests/test_music_playback.py \
  tests/test_paused_player_discard.py tests/test_seek_offset_scope.py tests/test_atomic_write.py \
  tests/test_voice_heal.py tests/test_np_card_transition.py -q
```

**Doku:** `docs/architecture/playback.md:7–17`.

**Abnahme:** wie Etappe 1.

## Etappe 3 — Track-Ende entflechten

**`TrackRun` einführen** und in `play_next` an der Stelle des bisherigen Factory-Aufrufs (`:336–339`) bauen.
`_make_after_playing(run)` baut nur noch den dünnen Thread-Callback.

**S3: `on_track_end` als Methode mit benannten Phasen**

```python
def _on_track_end(self, run, error, ended_at, stderr_tail):
    …                                                    # _playback_done, suppress_resume, elapsed, verdict
    self._log_early_death(…)
    retry = self._resume_growing_file(run, …)            # Invariante: VOR dem Marker-Reset
    retry = self._retry_dead_stream(run, …, retry)       # if/elif-Struktur von 717/740 unverändert
    self._clear_markers_after_clean_end(run, error, elapsed, retry)
    seq, snapshot = self._snapshot_queue()               # vor der Loop-Logik, wie bisher
    self._requeue_for_loop(retry)
    self._start_next(run.ctx)
    return seq, snapshot
```

Die Reihenfolge bleibt in einer Methode sichtbar. Die `if/elif`-Struktur von 717/740 wird nicht umgebaut:
Das `elif` läuft heute auch bei gesetztem `retry_scheduled`, und das muss so bleiben.

**S4: Marker-Reset.** Die zwei Vierer-Resets (752–755, 900–903) werden `self._clear_retry_markers()`. Die
zwei Zweier-Resets (319–320, 742–743) bleiben inline, sie setzen bewusst weniger zurück.

**Invarianten:** Marker-Reset nach dem Resume-Block (`CLAUDE.md:26`, ADR 0001), Queue-2-Tupel gegen
`current_track`-3-Tupel in der Loop-Logik, Trennung Thread/Loop (ADR 0005, ADR 0009 Stand 22.11.).

**Nicht ändern:** `run_coroutine_threadsafe` in `on_track_end` (`:737`, `:745`, `:779–790`) bleibt. Ein
Ersatz durch `spawn` würde Log-Texte ändern und den Start um einen Loop-Durchlauf verschieben.

**Testlauf:** wie Etappe 2.

**Doku:** ADR 0009 um einen „Stand"-Absatz ergänzen (`TrackRun`, neue Mixins), `playback.md` Zeile 10 und
15–17.

**Abnahme:** Testlauf grün; in `_on_track_end` steht der Resume-Schritt vor dem Marker-Reset.

## Etappe 4 — `play_next` und Helfer glätten

**S1: Gates in `play_next` (232–254)**

```python
# vorher: 3× if …: logger.info(…); self._track_generation += 1; return
# nachher
reason = self._advance_block_reason()   # Radio / DM spricht / Voice-Reparatur, gleiche Reihenfolge, gleiche Texte
if reason:
    logger.info(f"[play_next] {reason}")
    self._track_generation += 1
    return
```

**S2: `_build_audio_source` (571–613)**

```python
parts = []
if is_stream:
    parts.append("-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5")
    headers = self._ffmpeg_header_opts(info)
    if headers:
        parts.append(headers)
if seek_offset:
    parts.append(f"-ss {seek_offset}")
kwargs = {"before_options": " ".join(parts) or None, "stderr": stderr_buf}
if eq_filter:
    kwargs.update(bitrate=192, options=f"-vn {eq_filter}")
elif is_stream or is_growing:      # nie codec=copy (ADR 0001)
    kwargs["bitrate"] = 192
else:
    kwargs["codec"] = "copy"
source = discord.FFmpegOpusAudio(str(filename), **kwargs)
```

Die Kommentare zu den drei Fällen bleiben. Die Tests prüfen genau diese kwargs
(`tests/test_music_playback.py:507–510`); vor dem Umbau dort nachlesen, ob `before_options=None` und
fehlende Schlüssel so erwartet werden.

**S5: Karten-Übergang (372–408).** Der Loop-Repeat-Zweig wird
`await self._reset_card_for_loop_repeat(duration)`, die Zeilen 388–393 werden
`await self._retire_previous_cards()`. Beide werden an derselben Stelle awaitet, alle drei Generationschecks
bleiben im Orchestrator (ADR 0009). Dabei fällt der redundante Vergleich aus `:374` weg.

**S6: `_snapshot_track_state`.** Rückgabe `(generation, is_loop_repeat)` ohne das unbenutzte `prev_track`;
`if … : pass else:` (895–903) wird `if url not in (…):`.

**S7: `_search_and_enqueue` (1049–1092)** in drei benannte Schritte: Vorwärmen oder Primen, Einreihen nach
`insert`-Modus, Ansage mit Alternativen.

**S8: Kleinkram.** Ein Modul-Helfer `_cancel(task)` für die vier Abbruch-Stellen, `is_stream` einmal
berechnen, Methoden in Aufrufreihenfolge, Klassen-Defaults oben gesammelt.

**Docstrings:** Umbau-Historie an den Stellen aus Brennpunkt 6 entfernen, Modul-Docstring (1–25) auf den
Ist-Zustand bringen. Kommentare, die das Warum erklären, bleiben.

**Bewusst nicht vereinfachen:** die zwei Abbruch-Blöcke 341–357 (der Unterschied „Generation erhöhen" ist
Absicht und liest sich inline besser) sowie `_resolve_track` und `_prefetch_next` (dünn, aber harmlos).

**Invarianten:** Track-Zustand vor `vc.play()` und Generationschecks im Orchestrator (ADR 0007, ADR 0009),
Karte nur über `_retire_np_message` (ADR 0012), nie `codec=copy` bei wachsender Datei (ADR 0001).

**Testlauf**

```bash
pytest tests/test_music_playback.py tests/test_np_card_transition.py tests/test_np_card_retire.py \
  tests/test_card_title_link.py tests/test_np_card_link_button.py tests/test_seek_offset_scope.py \
  tests/test_paused_player_discard.py tests/test_player_clock.py tests/test_loop_after_playing.py \
  tests/test_after_playing_on_loop.py tests/test_search_prime.py tests/test_search_status_message.py \
  tests/test_voice_heal.py -q
```

**Doku:** ADR 0009 (Rückgabe von `_snapshot_track_state`).

**Abnahme:** Testlauf grün; `play_next` enthält weiterhin alle drei Generationschecks.

## Etappe 5 — Zusatzschnitte zu den Nachbarn (verhaltensgleich, vom Nutzer freigegeben)

Diese Etappe ändert `cogs/music.py`, `cogs/music_queue_io.py`, `cogs/music_radio.py`,
`cogs/music_voice_ui.py` und `views/music_controls.py` über Import-Zeilen hinaus.

**5a: Helfer „starten oder Prefetch anstoßen".** Das Muster steht sechsmal:

```python
if not self.is_playing:
    self.is_playing = True
    await self.play_next(ctx)
else:
    self._kick_prefetch()
```

Fundstellen: `cogs/music.py:428–432`, `:483–487`, `:524–528`, `:540–544`, `:554–558` und
`cogs/music_queue_io.py:100–102`. Nachgelesen wurde nur die erste Stelle. Vor dem Umbau jede Stelle
einzeln lesen: Nur Stellen, die exakt dieses Muster haben, werden zu `await self._start_or_prefetch(ctx)`.
Weicht eine Stelle ab (anderer Zweig, zusätzlicher Code dazwischen), bleibt sie inline. Der Helfer liegt in
`PlaybackMixin` neben `play_next`; die erklärenden Kommentare der Aufrufstellen wandern in seinen Docstring.

**5b: Helfer „auf `_playback_done` warten".** Fünf Stellen warten mit
`asyncio.wait_for(self._playback_done.wait(), timeout=3.0)`: `cogs/music_playback.py:267–277`, `:942–948`,
`cogs/music_radio.py:120–127`, `:351–354`, `cogs/music_voice_ui.py:296–299`. Sie reagieren unterschiedlich
auf den Timeout (Warnung und Event zurücksetzen, Fehlermeldung und `_stop_radio`, stilles Weitergehen).
Der Helfer kapselt deshalb nur das Warten:

```python
async def _wait_playback_done(self, timeout: float = 3.0) -> bool:
    """True, wenn der vorherige Player fertig ist; False bei Timeout."""
```

Die Reaktion auf `False` und das `self._playback_done.clear()` bleiben bei jedem Aufrufer wörtlich. Stellen,
deren Bedingung vor dem Warten abweicht, behalten ihre Bedingung.

**5c: `_write_last_queue` nach `cogs/music_queue_io.py`.** Mit `_LAST_QUEUE_LOCK` und den Klassen-Defaults
`_last_queue_seq`/`_last_queue_written`. `tests/test_after_playing_on_loop.py:212–216` ruft
`mc._write_last_queue` direkt auf der Instanz, das bleibt gültig. Patches auf `atomic_write_text` und
`_LAST_QUEUE_LOCK` müssen auf das Modul zeigen, in dem die Methode jetzt liegt (per grep in
`tests/test_after_playing_on_loop.py` und `tests/test_atomic_write.py` prüfen und Pfade anpassen).

**5d: Titelzeile der Karte nach `views/music_controls.py`.** Die Zeilen `cogs/music_playback.py:427–441`
(Künstler/Titel drehen, Uploader anhängen, Link-Label, Markdown-Escape) werden eine reine Funktion, zum
Beispiel `np_heading(title, info) -> str`. `_build_np_view` ruft sie auf. Der Import von `link_label` zieht
mit. Echtdaten-Fälle, die unverändert bleiben müssen: Titel mit ` - `, mit `|`, mit `[ ]`, Nur-Emoji-Titel
(ohne Link), fehlende `webpage_url`.

**Invarianten:** Karte ist Components V2, nie `content=`/`embed=`/`view=None` (ADR 0012); Musik-Stopp nur
über `_stop_for_advance()`; `last_queue.json`-Write bleibt im Player-Thread und gedebounct (ADR 0005).

**Testlauf**

```bash
pytest tests/test_music_playback.py tests/test_queue_ops.py tests/test_after_playing_on_loop.py \
  tests/test_atomic_write.py tests/test_card_title_link.py tests/test_np_card_link_button.py \
  tests/test_np_card_controls.py tests/test_voice_heal.py tests/test_paused_player_discard.py \
  tests/test_search_status_message.py tests/test_p_single_url_fallback.py -q
```

Dazu die Radio-Tests (per `grep -rln "radio" tests/` ermitteln).

**Doku:** `docs/architecture.md`, `docs/architecture/playback.md`, ADR 0009 „Stand"-Absatz.

**Abnahme:** Testlauf grün; jede ersetzte Aufrufstelle wurde vorher gelesen und stimmt mit dem Muster
überein.

## Etappe 6 — `after_radio` auf den Loop (**nicht verhaltensgleich**, vom Nutzer freigegeben)

**Ausgangslage:** `after_radio` (`cogs/music_radio.py:159–184`) läuft im Player-Thread und ändert dort
Zustand: `self.is_radio`, `self.is_playing`, und über `_radio_should_reconnect` den Reconnect-Zähler. Der
Song-Pfad macht das seit ADR 0009 (Stand 22.11.) nicht mehr: `after_playing` ist ein dünner Thread-Callback,
der die Logik per `_run_on_loop` auf dem Event-Loop ausführt.

**Inhalt**

- `_run_on_loop` und `_LOOP_HANDOFF_TIMEOUT` ziehen nach `utils/tasks.py`. Die Patches in
  `tests/test_after_playing_on_loop.py` (Zeilen 85, 93, 186) müssen danach auf das Modul zeigen, in dem die
  Namen nachgeschlagen werden.
- `after_radio` wird nach dem Vorbild von `after_playing` geteilt: ein dünner Thread-Callback, der
  `token.discarded` prüft, die Endzeit misst (`time.monotonic()` im Thread, nicht erst auf dem Loop) und
  dann `_on_radio_end(…)` per `_run_on_loop` auf dem Loop ausführt.
- `_on_radio_end` enthält die bisherige Logik unverändert in der Reihenfolge: `_playback_done.set()`, Log,
  Prüfung `is_radio`/`_radio_session is token`, Reconnect oder Abbruch mit Meldung.

**Was sich ändert:** Zustandsänderungen passieren einen Loop-Durchlauf später und nicht mehr parallel zum
Event-Loop. Der Player-Thread wartet bis zum Timeout auf die Übergabe, wie beim Song-Pfad.

**Invarianten:** Verbindungsabbau und Reconnect nur über die Helfer aus `utils/voice.py`; ein abgelöster
Sender (Senderwechsel, `token.discarded`) fasst keinen Zustand an (Fix `9d33af9`).

**Testlauf:** `tests/test_after_playing_on_loop.py`, `tests/test_paused_player_discard.py`, alle Radio-Tests
(`grep -rln "radio" tests/`).

**Absicherung von Hand:** Radio-Abschnitt der `docs/smoke-checklist.md`, insbesondere Senderwechsel,
`!radio` bei pausiertem Player, Song übernimmt laufendes Radio, Stream bricht ab und verbindet neu.

**Doku:** ADR 0009 „Stand"-Absatz (Radio-Pfad zieht nach), `docs/architecture/playback.md`.

**Abnahme:** Testlauf grün; in `after_radio` steht keine Zuweisung an `self.*` mehr außerhalb von
`_on_radio_end`.

## Abschluss der Etappen 1–6

1. Volle Suite: `pytest` und `ruff check .`.
2. Der Nutzer geht die `docs/smoke-checklist.md` von Hand durch.
3. Doku systematisch nachziehen: `docs/architecture.md`, `docs/architecture/playback.md`, ADR 0009, Landkarte
   in `CLAUDE.md`, Dateibaum in `README.md` und `README_DE.md`.
4. Push.

---

## Etappe 7 — Effizienz (optional, **nicht verhaltensgleich**, Einzelfreigabe nötig)

Innerhalb „verhaltensgleich" gibt es in dieser Datei keinen Befund, der sich lohnt: Suche, Voice-Join und
Statusnachricht laufen bereits parallel (`:971–990`, `cogs/music.py:407–410`), der Datei-Write liegt im
Player-Thread. Die zwei echten Befunde ändern Verhalten. Jeder braucht vor der Umsetzung ein ausdrückliches
Ja des Nutzers und eine Messung vorher und nachher.

**7a: FFmpeg erst starten, wenn feststeht, dass gespielt wird.** `_build_audio_source` läuft in `:330`, die
Prüfungen „Voice verbunden" und „DM spricht" erst in `:341–357`. `FFmpegOpusAudio` startet den Prozess im
Konstruktor (discord.py 2.7.1, `player.py:183`). In beiden Abbruchpfaden entstehen Prozess und Temp-Datei
umsonst. Zwischen 313 und 368 liegt kein await, die Prüfungen ließen sich also vorziehen. Im Randfall
„FFmpeg fehlt und Voice getrennt" ändert das den Ablauf (andere Fehlermeldung zuerst). Dieser Punkt
erledigt zugleich Bug 3 im Anhang für die zwei Abbruchpfade.

**7b: Blockierende Aufrufe pro Trackstart vom Loop nehmen.** `tempfile.TemporaryFile()` (`:585`) und
`subprocess.Popen` im Konstruktor laufen synchron in der Coroutine. Die Dauer ist nicht gemessen; zuerst
messen (Zeitstempel um `_build_audio_source`, zehn Trackstarts auf der Zielhardware). Ein `to_thread` würde
eine neue await-Grenze vor `vc.play()` einführen und damit die Invariante „Track-Zustand komplett vor
`vc.play()`, danach Generation prüfen" berühren (ADR 0007). Nur umsetzen, wenn die Messung einen spürbaren
Wert zeigt.

---

## Nicht anfassen

- **Reihenfolge in `on_track_end`:** Resume-Block vor Marker-Reset (`CLAUDE.md:26`, ADR 0001). S3 benennt die
  Schritte, vertauscht sie nicht.
- **`_snapshot_track_state` bleibt synchron** und steht direkt vor `vc.play()`; die drei Generationschecks
  bleiben in `play_next` (ADR 0007, ADR 0009).
- **`_stop_for_advance` und `_discard_paused_player`** sehen doppelt aus, sind aber absichtlich zwei Pfade:
  Das One-Shot-Flag darf nicht vom falschen Callback verbraucht werden (`:199–214`).
- **`after_playing` als dünner Thread-Callback mit `_run_on_loop` und Timeout** (ADR 0009 Stand 22.11.,
  `tests/test_after_playing_on_loop.py`).
- **`_write_last_queue` mit Lock und Folgenummer:** nötig, weil der Write nach dem Anstoßen des nächsten
  Tracks läuft. Etappe 5c verschiebt die Methode, ändert sie aber nicht.
- **`_take_seek_offset`:** klein und richtig, das Ergebnis des Fixes `d3f4d54`.
- **`_handle_queue_empty`:** lang, aber linear und gut kommentiert; `now_playing_msg` und `_ended_np` können
  gleichzeitig belegt sein (ADR 0012).
- **`_extract_info_or_report`:** die Parallelität von Senden, Abfrage und Löschen ist gemessen begründet
  (`playback.md:94–98`).
- **Zweite `dm_speaking`-Prüfung in `:351`:** kein Duplikat, sie deckt das Fenster während des Resolve ab.
- **`_BACKGROUND_TASKS`:** wird unverändert verschoben, nicht durch `utils.tasks.spawn` ersetzt (der Test
  müsste sonst eine andere Menge einsammeln).

---

## Folgeprojekt: Variante B (eigene Klassen, Komposition)

Nicht Teil dieses Plans. Variante A schneidet dieselben Nähte, aus denen B später entstehen kann.

**Kandidaten**

| Klasse | Eigener Zustand | Woran es hängt |
|---|---|---|
| `RetryState` | die vier Retry-Marker | `cogs/music.py:120–131` legt sie in `__init__` an; `tests/conftest.py:208–211` und `tests/test_music_playback.py:181–184` setzen sie direkt, rund 20 Asserts lesen sie |
| `PrefetchController` | `prefetch_task`, `_autoplay_prefetch_task`, `_autoplay_queued_url` | gelesen in `cogs/music.py:776–779` und `:834–835`, `cogs/music_radio.py:92–94` und `:129–130`, `conftest.cleanup_cog` |
| `TrackEndHandler` | Marker plus `_suppress_resume` | braucht über 15 Cog-Attribute per Rückreferenz, also ein Mixin mit Umweg |
| `LastQueueWriter` | Lock, Folgenummern | einziger sauberer Kandidat; Gewinn rund 20 Zeilen, dafür ein Delegat, weil `tests/test_after_playing_on_loop.py:212–216` `mc._write_last_queue` direkt ruft |

**Zwei Sperren gelten für alle vier**

- Tests bauen den Cog ohne `__init__` (`MusicCommands.__new__`). Zustandsobjekte müssten lazy entstehen.
- Tests und Nachbarmodule greifen direkt auf die Attribute zu. Ohne inhaltliche Teständerung bleiben nur
  Property-Weiterleitungen am Mixin; das ist mehr Indirektion als heute.

**Was ein echtes B braucht:** ein gemeinsames `PlaybackState`-Objekt, auf das `music.py`,
`music_voice_ui.py`, `music_radio.py`, `views/music_controls.py`, `utils/diagnostics.py`, `conftest.py` und
rund 15 Testdateien umgestellt werden, sowie eine neue ADR 0014, die ADR 0009 („`MusicCommands` hält
weiterhin den Instanz-State") teilweise ablöst.

**Voraussetzungen, die der Nutzer dafür lockern müsste:** Tests dürfen inhaltlich umgeschrieben werden, der
Umfang umfasst die ganze Music-Cog samt Views.

**Grober Ablauf**

1. Gemeinsamen Test-Aufbau-Helfer einführen, der die `__new__`-Aufbauten ersetzt (noch auf der
   Mixin-Struktur, damit die Tests vor dem Umbau grün sind).
2. `PlaybackState` als Dataclass einführen; `MusicCommands` hält es als `self.state` und leitet die alten
   Attributnamen vorübergehend per Property weiter.
3. Module nacheinander auf `self.state.…` umstellen (Playback, Track-Ende, Prefetch, Radio, Voice-UI,
   Views), je ein Commit mit gezieltem Testlauf.
4. Property-Weiterleitungen entfernen, Tests auf den Aufbau-Helfer und `state` umstellen.
5. `RetryState` und `PrefetchController` aus `PlaybackState` herauslösen, wo sie eigene Methoden bekommen.
6. ADR 0014 schreiben.

**Aufwand und Risiko:** mehrere Tage, mindestens fünf Etappen, Risiko hoch: Die Verhaltensgleichheit unter
Race-Bedingungen (Generationszähler, Player-Thread gegen Loop) muss neu nachgewiesen werden, während das
Sicherheitsnetz selbst umgebaut wird. Lohnt sich, wenn der Zustand auf `self` nach Variante A weiterhin der
Hauptgrund für Fehler ist; die Bugs 1 und 2 im Anhang sind genau diese Fehlerklasse.

---

## Anhang: Wahrscheinliche Bugs (nicht Teil des Refactorings)

Alle vier sind aus der Lektüre geschlossen, keiner ist reproduziert. Nach dem Refactoring beheben, je ein
Fix-Commit mit Test. Empfohlene Reihenfolge: 2, 3, 1, 4.

1. **`last_played` wird überschrieben, `:887`.** `self.last_played = prev_track` gilt ohne Bedingung. Nach
   `!seek`, `!eq` oder einer Voice-Reparatur ist `current_track` vorher `None` (`cogs/music.py:944`, `:983`,
   `cogs/music_voice_ui.py:278`), danach ist `last_played` leer und ⏮ sowie `!replay` kennen keinen Vorgänger
   mehr. Bei Loop-Song, Stream-Retry und Progressive-Resume zeigt `last_played` auf den laufenden Song
   selbst.
2. **`_skip_resolving` ist ein ungebundenes Flag, `:303–306`.** Es wird nur nach erfolgreichem Resolve
   verbraucht. Scheitert der Resolve, bleibt es stehen und der nächste Track wird übersprungen; das ist dann
   genau die Alternative, die der Nutzer gewählt hat. Gesetzt wird es außerdem auch, wenn gar kein Resolve
   läuft (`views/music_controls.py:446–450`). Dieselbe Fehlerklasse wie der Seek-Offset vor `d3f4d54`.
3. **FFmpeg-Prozess ohne Aufräumen in den Abbruchpfaden, `:341–357`,** ebenso wenn `vc.play()` wirft. Die
   Quelle wird nie mit `cleanup()` beendet. Seit `ababaed` hält `start_clock_at_first_packet` einen
   Referenzkreis (`utils/voice.py:52–61`); der Prozess lebt deshalb bis zum nächsten zyklischen GC-Lauf.
   Vermutung: Unter Windows hält er so lange die Datei offen.
4. **Retry-Zweige ohne Prüfung auf gewollten Abbruch, `:717` und `:740`.** Skippt der Nutzer den
   Download-Fallback-Anlauf in den ersten 2 Sekunden, kommt `error.stream_giveup`. Vermutung: Ein Skip eines
   Streams in der ersten Sekunde kann bei passendem stderr-Inhalt den Track wieder einreihen.

## Kickoff-Prompt für die frische Session

> Setze `docs/plans/2026-11-refactoring-music-playback.md` um, Etappen 1 bis 6 strikt in Reihenfolge. Prüfe
> Zeilennummern vorher per grep. Nach jeder Etappe `ruff check .`, den genannten gezielten Testlauf und
> einen Commit direkt auf main; die volle pytest-Suite nur einmal am Ende. Halte dich an die
> Nutzer-Entscheidungen, die Regeln für alle Etappen und den Abschnitt „Nicht anfassen". Etappe 7 nicht
> umsetzen, sondern mir am Ende zur Einzelfreigabe vorlegen.
