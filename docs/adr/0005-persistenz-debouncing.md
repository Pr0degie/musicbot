# ADR 0005: Persistenz-Debouncing für Dauerbetrieb auf schwacher Hardware

- **Status:** Akzeptiert
- **Datum:** 2026-07-11 (Commits `3b07a3b`, `5714cab`, `397a2de`)

## Kontext

Der Bot läuft im Dauerbetrieb auf schwacher Hardware; blockierendes File-I/O im Event-Loop (Play-Counts, Metadaten-Cache nach jedem Song) kostet dort spürbar.

## Entscheidung

Kein blockierendes File-I/O im Event-Loop; alle wiederkehrenden Writes sind gedebounct.

## Konsequenzen

- **`_persist_flush_loop`** (`@tasks.loop(seconds=30)` in `music.py`): schreibt `play_counts.json` (`_score_dirty`, gesetzt von `_record_play`) und `metadata_cache.json` (`dl._cache_dirty`, via `dl.flush_cache()`) gebündelt — Serialisierung/Snapshot auf dem Loop, Write in `asyncio.to_thread`. **Trade-off (im Code dokumentiert):** bei hartem Crash fehlen bis zu 30 s Play-Counts bzw. Cache-Einträge (Letzteres = nur Cache-Miss).
- **Flush-Garantien:** `cog_unload` und `!restart` (in `basic.py`) rufen `_flush_scores_now()` + `dl.flush_cache_now()` synchron auf (Restart-Mechanik → [ADR 0003](0003-windows-native-zielplattform.md)).
- **Einmalige Writes** (`radio_stations.json`, `!saveq`-Playlists) laufen ohne Debounce via `asyncio.to_thread` (`_write_stations`/`_write_playlist`). Der `last_queue.json`-Write in `after_playing` bleibt synchron — der Callback läuft ohnehin im FFmpeg-Thread, nicht im Event-Loop.
- **Reduzierter Metadaten-Cache:** Persistiert wird reduziert — nur `PERSISTED_CACHE_FIELDS` (title, ext, duration, url, webpage_url, thumbnail, uploader, http_headers — die einzigen Felder, die je aus dem Cache gelesen werden; ~1,3 MB → ~7 KB bei 4 Songs). In-Memory bleibt das volle Dict. Alte Dateien im vollen Format laden weiterhin; unlesbare werden geloggt verworfen (Kaltstart). Scheitert ein aus der Datei geladener Eintrag bei `prepare_filename`, wird er verworfen und frisch extrahiert (Cache-Miss-Pfad, nie ein User-Fehler).

**Stand 2026-11-22:** Alle JSON-Writes (`play_counts.json`, `metadata_cache.json`, `radio_stations.json`, Playlists, `last_queue.json`) laufen atomar über `utils/files.atomic_write_text` (Temp-Datei im selben Ordner, dann `os.replace`). `PERSISTED_CACHE_FIELDS` enthält zusätzlich `id` – die Video-ID steckt im Dateinamen (`%(title)s [%(id)s].%(ext)s`); Einträge ohne `id` aus einer älteren Datei werden beim Start verworfen. Die `after_playing`-Logik läuft inzwischen auf dem Event-Loop ([ADR 0009](0009-music-cog-mixin-zerlegung.md)); der `last_queue.json`-Write bleibt trotzdem im Player-Thread (`_write_last_queue`), nur der Snapshot entsteht auf dem Loop. Die Wartung alle 50 Songs nutzt `dl.rebuild_async` (Instanzen im Worker-Thread bauen, Zustand auf dem Loop ändern); der In-Memory-Cache beachtet `CACHE_TTL` auch zur Laufzeit (`_usable_cached_info`).

**Stand 2026-11-22 (Ablageort):** Die vom Bot geschriebenen Dateien (`play_counts.json`, `metadata_cache.json`, `last_queue.json`, Playlists, seit 27.11.2026 `favorites.json`) liegen unter `data/` (`utils/paths.DATA_DIR`, relativ zum Arbeitsverzeichnis); `atomic_write_text` legt den Zielordner bei Bedarf an. `radio_stations.json` bleibt im Root ([ADR 0014](0014-laufzeitdaten-data-und-logs.md)).
