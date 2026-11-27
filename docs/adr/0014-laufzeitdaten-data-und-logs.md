# ADR 0014: Laufzeitdaten in `data/` und `logs/`, relativ zum Arbeitsverzeichnis

- **Status:** Akzeptiert
- **Datum:** 2026-11-22

## Kontext

Alles, was der Bot zur Laufzeit schreibt, lag direkt im Arbeitsverzeichnis: `bot.log` plus bis zu zehn rotierte Läufe, `last_queue.json`, `metadata_cache.json`, `play_counts.json`, `downloads/` und `playlists/`. Von 33 Dateien im Repo-Root waren nur 18 versioniert. Die Pfade standen als einzelne Konstanten in fünf Modulen. `tools/multi_bots.py` trennt die Instanzen darüber, dass jede in ihrem eigenen Ordner `multi_bots/botN/` als Arbeitsverzeichnis läuft.

## Entscheidung

- `utils/paths.py` definiert `DATA_DIR` (`data/`) und `LOG_DIR` (`logs/`). Queue-Snapshot, Metadaten-Cache, Play-Counts, Favoriten (seit 27.11.2026), Playlists und Downloads liegen unter `data/`, `bot.log` und `bot.log.N` unter `logs/`.
- Beide Pfade bleiben **relativ zum Arbeitsverzeichnis**, nicht zu `__file__`: sonst teilten sich alle Multi-Bot-Instanzen einen `downloads/`-Ordner und hebelten die In-Flight-Registry aus ([ADR 0002](0002-inflight-registry.md)).
- `migrate_legacy_layout()` zieht beim Start um, was noch im alten Layout liegt. `main.py` ruft es vor allen anderen Imports auf, weil `utils.logger` beim Import das Log rotiert und die Cogs beim Import `data/downloads` und `data/playlists` anlegen. Überschrieben wird nie etwas: was am Ziel schon existiert oder gesperrt ist, bleibt liegen, erscheint als `[INIT]`-Warnung und wird beim nächsten Start erneut versucht. Ordner mit vorhandenem Ziel werden dateiweise gemischt.
- `radio_stations.json` bleibt im Root: die Datei ist versioniert (Senderliste zum Ausliefern) und wird von `!radio <URL>` zugleich ergänzt.
- `utils.paths` importiert `utils.logger` nicht, `atomic_write_text` legt den Zielordner bei Bedarf an.

## Konsequenzen

- Neue Laufzeitdateien gehören unter `DATA_DIR`/`LOG_DIR`; ein neuer Pfad direkt im Arbeitsverzeichnis bräuchte zusätzlich einen `.gitignore`-Eintrag und wäre beim Multi-Bot-Betrieb leicht falsch.
- `main.py` hat Code vor den Imports (ruff `E402` ist für die Datei abgeschaltet, `pyproject.toml`).
- Tests, die mit `monkeypatch.chdir(tmp_path)` arbeiten, finden die Dateien unter `tmp_path/data/`.
- Der Umzug ist eine Einmal-Aktion, läuft aber bei jedem Start mit (wenige `exists()`-Aufrufe). Er kann entfallen, sobald keine Installation mit altem Layout mehr existiert.
