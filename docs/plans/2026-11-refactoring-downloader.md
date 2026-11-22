# Plan: `cogs/downloader.py` zerlegen und vereinfachen

> **Für die ausführende Session:** Dieser Plan entstand am 22.11.2026 aus einer Lese-Analyse der Datei
> (ein Analyse-Agent, keine Tests ausgeführt, kein Netzwerk) plus fünf Fragerunden mit dem Nutzer.
> Alle Datei-/Zeilenangaben beziehen sich auf HEAD `ababaed`. Zeilennummern vor der Umsetzung per grep
> verifizieren — sie verschieben sich mit jeder Etappe. Etappen strikt in Reihenfolge abarbeiten. Nach
> jeder Etappe: `ruff check .`, der bei der Etappe genannte gezielte Testlauf, Commit direkt auf main
> (deutsche Commit-Message im Stil der Historie, keine KI-Tagline). Die volle `pytest`-Suite und die
> manuelle `docs/smoke-checklist.md` laufen **einmal am Ende des Plans**, nicht nach jeder Etappe.
> `main.py` nie importieren (startet den Bot).
>
> **Reihenfolge der zwei Refactoring-Pläne:** dieser Plan zuerst, danach
> [`2026-11-refactoring-music-playback.md`](2026-11-refactoring-music-playback.md).
>
> **Stand 22.11.2026:** Etappen 1–5 sind umgesetzt (`6e649c5`, `b5f094b`, `0a86d70`, `1c4f3d4`, `4ad1358`),
> volle Suite und `ruff` grün. Ergebnis: `downloader.py` 315 Zeilen, Mixins `downloader_cache.py` 241,
> `downloader_ydl.py` 287, `downloader_cleanup.py` 152, `downloader_prefetch.py` 200,
> `downloader_progressive.py` 346. Offen: die manuelle Smoke-Checkliste (Abschnitte Download und Prefetch),
> Etappe 6 (Einzelfreigabe) und die Bugs aus dem Anhang.

## Ziel

`cogs/downloader.py` (1104 Zeilen, zweitgrößte Datei) soll lesbarer und kleiner werden, ohne dass sich das
Verhalten des Bots ändert. Die Datei ist inhaltlich in gutem Zustand: Sie enthält fünf klar trennbare
Themen, die nur deshalb schwer zu überblicken sind, weil sie in einer Klasse hintereinander stehen. Der
Umbau trennt die Themen in Mixins und glättet danach sieben verschachtelte Funktionen.

Ergebnis nach Etappe 5: `downloader.py` etwa 300 Zeilen (nur noch die `resolve_track`-Phasen), vier neue
Module mit etwa 190, 290, 135 und 175 Zeilen.

## Nutzer-Entscheidungen (bindend)

| Thema | Entscheidung |
|---|---|
| Prioritäten | Lesbarkeit, Laufzeit-Effizienz, kleinere Dateien. Testbarkeit ist kein eigenes Ziel |
| Verhalten | **Strikt verhaltensgleich** in den Etappen 1–5. Alles, was Timing oder Verhalten ändert, steht in Etappe 6 und braucht eine Einzelfreigabe |
| Struktur | **Erst Variante A** (Mixins, wie `downloader_progressive.py`). Variante B (eigene Klassen, Komposition) ist als Folgeprojekt ausgearbeitet, siehe unten |
| Umfang | `downloader.py` plus neue Module. Zusätzlich freigegeben: Progressive-Zustand nach `downloader_progressive.py` umziehen (Etappe 5). Andere Dateien sonst nur Import-Zeilen |
| Tests | Nur Import- und Patch-Pfade anpassen. Keine inhaltlichen Teständerungen, Testdateien nicht aufteilen |
| Schrittgröße | Wenige größere Etappen, jede mit eigenem gezielten Testlauf |
| Zielgröße | Kein festes Zeilenlimit, geschnitten wird entlang der Themen |
| Bugs | Gefundene Bugs stehen im Anhang. Behoben werden sie **nach** dem Refactoring, je ein Fix-Commit mit Test |
| Effizienz | Befunde, die Timing ändern, kommen in die optionale Etappe 6; vorher wird gemessen |
| Absicherung | Gezielte Tests pro Etappe, volle Suite und Smoke-Checkliste einmal am Ende |
| Umsetzer | Claude in neuen Sessions |

## Ausgangslage

Herkunft der Angaben: Zieldatei, `downloader_progressive.py`, `utils/files.py`, die Aufrufer in
`cogs/music*.py` und `views/`, `CLAUDE.md`, `docs/architecture.md`, `docs/architecture/playback.md` und die
ADRs 0001, 0002, 0003, 0005, 0009, 0010, 0013 wurden gelesen. Die Tests wurden nur nach Patch- und
Import-Pfaden durchsucht. Die Methodenliste wurde zusätzlich per grep gegengeprüft und stimmt. Zwei
Zeitmessungen liefen offline gegen `yt_dlp` 2026.10.26 auf Python 3.12.10.

### Strukturkarte (Stand `ababaed`)

**Modulebene (1–110)**

| Zeilen | Inhalt |
|---|---|
| 1–21 | Imports, Re-Export der `PROGRESSIVE_*`-Konstanten |
| 23–27 | `DOWNLOAD_DIR` (mit `mkdir` beim Import), `METADATA_CACHE_FILE`, `CACHE_TTL` |
| 29–66 | `COOKIE_ERROR_MARKERS`, `FAST_PLAYER_CLIENTS`, `CLIENT_FALLBACK_MARKERS` |
| 68–91 | `PERSISTED_CACHE_FIELDS`, `_ID_SUFFIX` |
| 94–109 | Dataclass `_Resolution` (Zwischenstand der resolve-Phasen) |
| 112–165 | Klassenkopf, Klassen-Defaults, `__init__` |

**Klasse `Downloader`**

| Thema | Methode | Zeilen | Länge | Verantwortung |
|---|---|---|---|---|
| Instanzen | `rebuild` | 171–186 | 16 | synchroner Neuaufbau (`!format`, `!reloadcookies`) |
| Instanzen | `rebuild_async` | 188–231 | 44 | Neuaufbau im Betrieb: Zustand auf dem Loop, Bauen im Thread |
| Cache | `clear_cache`, `invalidate` | 233–240 | 7 | Cache leeren, einen Eintrag verwerfen |
| Cache | `_usable_cached_info` | 242–264 | 23 | Eintrag, falls TTL gültig oder Datei vollständig vorhanden |
| Cache | `_load_cache`, `_drop_cache_entries_without_id` | 266–307 | 41 | `metadata_cache.json` tolerant laden, Einträge ohne `id` verwerfen |
| Cache | `_serialize_cache`, `_save_cache`, `flush_cache_now`, `flush_cache` | 309–349 | 38 | Snapshot auf dem Loop, Write, Shutdown-Flush, gedebouncter Flush |
| Cache | `_fetch_info`, `_start_resolve`, `warmup` | 351–396 | 44 | extrahieren und cachen, Metadaten vorwärmen, JS-Player-Cache vorwärmen |
| yt_dlp | `_cookies_configured`, `_cookie_opts`, `_needs_cookies` | 398–418 | 21 | Cookie-Quelle, Optionen, Fehlererkennung |
| yt_dlp | `enable_cookie_mode` | 420–437 | 18 | dauerhaft umschalten, Cache leeren, neu bauen |
| yt_dlp | `_YDL_KINDS`, `extract_info_async` | 439–481 | 42 | Extraktion im Thread mit Cookie- und Client-Rückfall |
| yt_dlp | `_player_opts`, `_needs_client_fallback`, `_build_base_opts` | 483–519 | 36 | schneller Client, Rückfall-Erkennung, Basis-Optionen |
| yt_dlp | `_make_progressive_ydl`, `_build_ydl`, `_init_ydl` | 521–597 | 75 | Wegwerf-Instanz, Instanz je Art, alle fünf bauen |
| Pflege | `cleanup_downloads` | 603–665 | 63 | Pending-Deletes nachholen, Schutzmengen bauen, Cleanup anstoßen |
| Pflege | `_cleanup_downloads_sync`, `_purge_stale_progressive` | 667–713 | 46 | älteste Dateien löschen (Thread), Sidecar-Reste beim Start löschen |
| resolve | `resolve_track` | 719–751 | 33 | Orchestrator der vier Phasen |
| resolve | `_stream_result`, `_file_result` | 753–761 | 9 | Ergebnis-Tupel, pflegen `last_resolved_file` |
| resolve | `_resolve_metadata` | 763–808 | 46 | Phase 1: Metadaten und Zieldatei |
| resolve | `_adopt_inflight` | 810–853 | 44 | Phase 2: lebenden Writer adoptieren oder abwarten |
| resolve | `_clear_corpse` | 855–870 | 16 | Phase 3: Leiche wegräumen |
| resolve | `_acquire_file` | 872–939 | 68 | Phase 4: Datei, Download-Fallback, progressiv oder Stream |
| Prefetch | `prefetch_next` | 941–972 | 32 | eigenen Task je URL registrieren und shielden |
| Prefetch | `_prefetch_one` | 974–1028 | 55 | Queue-Download unter dem Lock |
| Prefetch | `prefetch_autoplay` | 1030–1104 | 75 | Kandidat suchen, wählen, laden, cachen |

Zeilenanteile: Cache rund 190, yt_dlp/Cookies/Extraktion rund 240, Rebuild 60, Pflege 115, resolve rund 230,
Prefetch 165.

### Brennpunkte

**Lange und verschachtelte Funktionen**

1. `_acquire_file` (`:872–939`): fünf Ebenen, `filename.exists()` dreimal hintereinander als
   Zustandsautomat, zwei `else`-Zweige 60 Zeilen unter ihrem `if`.
2. `prefetch_autoplay` (`:1030–1104`): drei Aufgaben in einem `try` mit innerem `try/finally`.
3. `cleanup_downloads` (`:603–665`): zwei fast gleiche Schutzmengen-Schleifen (`:619–625`, `:651–658`) mit
   leicht verschiedener Fehlerbehandlung, dazwischen vier Abbruchbedingungen.
4. `_build_ydl` (`:535–592`): fünf Options-Dicts mit denselben vier bis fünf Schlüsseln; der Unterschied
   zwischen den Arten geht im Gleichen unter.
5. `_prefetch_one` (`:974–1028`): `try` in `try`, Lock mit Doppel-Check, zwei identische
   „Bereits im Cache"-Zweige (`:1014`, `:1021`).
6. `_adopt_inflight` (`:810–853`): `prog_task` und `entry` werden über Kreuz belegt (`:822–826`).
7. `_resolve_metadata` (`:763–808`): drei Ebenen `if/else` (`:769–785`), danach der Zieldatei-Block doppelt
   (`:788–796`, `:804–807`).

**Duplizierte Logik**

- Cache-Eintrag ablegen (Playlist-Wrapper entpacken, Dict, Timestamp, Dirty-Flag): `:356–360`, `:389–393`,
  `:1080–1084`.
- Cache leeren oder ausdünnen samt `_pending_resolves.clear()`: `:179–185`, `:204–210`, `:432–434`.
- „[Cache] Speichern fehlgeschlagen" mit eigenem `try/except`: `:200–202`, `:216–219`, `:324–327`, `:344–349`.
- Leiche wegräumen (unlink, Sidecar, Set): `:860–864`, `:998–1005`.
- `prepare_filename` mit einmaligem Frisch-Extrahieren: `:790–807`, `:987–997`.
- `try: await asyncio.shield(task) / except Exception: pass`: `:829–832`, `:841–844`, `:885–888`, mit Timeout
  `:1065–1068`.

**Verstreuter Zustand**

- Fünf Progressive-/Registry-Attribute (`_progressive`, `_progressive_files`, `_incomplete_files`,
  `_progressive_blocked`, `_inflight`) gehören fachlich dem Progressive-Mixin, werden aber in
  `downloader.py` deklariert (`:133–140`) und angelegt (`:157–161`).
- `last_resolved_file` wird in Phase 1 und in den Ergebnis-Helfern geschrieben und im Cleanup gelesen.
- `_pending_resolves` wird an fünf Stellen geleert oder gepflegt, zwei davon in `downloader_progressive.py`.
- `_prefetch_dl_lock` entsteht lazy mitten in `_prefetch_one` (`:1007–1008`).

**Schwer nachvollziehbare Fehlerpfade**

- `extract_info_async` (`:463–481`): Der Cookie-Zweig fällt aus dem `except` heraus auf das letzte
  `return`, der Client-Zweig kehrt im `except` zurück, der dritte wirft neu.
- Ob eine Ausnahme geschluckt wird, hängt vom Aufrufer ab: Prefetch schluckt alles, `resolve_track` reicht
  Download-Fallback-Fehler durch, Cleanup schluckt alles. Das ist Absicht, steht aber nur in Kommentaren.

## Zielbild (Variante A)

```python
class Downloader(PrefetchMixin, DownloadsCleanupMixin, MetadataCacheMixin, YdlMixin, ProgressiveDownloadMixin):
```

Die Methodennamen der Mixins überschneiden sich nicht.

| Modul | Inhalt | Zeilen etwa |
|---|---|---|
| `cogs/downloader_cache.py` (`MetadataCacheMixin`) | `METADATA_CACHE_FILE`, `CACHE_TTL`, `PERSISTED_CACHE_FIELDS`; alles von `clear_cache` bis `warmup`; Default `_cache_dirty` | 190 |
| `cogs/downloader_ydl.py` (`YdlMixin`) | drei Marker-Konstanten, `DOWNLOAD_DIR` samt `mkdir`, Cookie-Methoden, `_YDL_KINDS`, `extract_info_async`, Options- und Bau-Methoden, `rebuild`, `rebuild_async` | 290 |
| `cogs/downloader_cleanup.py` (`DownloadsCleanupMixin`) | `_ID_SUFFIX`, `cleanup_downloads`, `_cleanup_downloads_sync`, `_purge_stale_progressive`; Default `protected_provider` | 135 |
| `cogs/downloader_prefetch.py` (`PrefetchMixin`) | `prefetch_next`, `_prefetch_one`, `prefetch_autoplay`; Default `_prefetch_dl_lock`; Import aus `utils.autoplay` | 175 |
| `cogs/downloader.py` (bleibt) | `_Resolution`, Klassenkopf, `last_resolved_file`, `__init__`, alle resolve-Phasen, Re-Exports | 300 |

Neue Module liegen flach in `cogs/`, kein Unterpaket: `tests/test_no_raw_vc_stop.py`,
`tests/test_voice_guard_move.py` und `tests/test_i18n_keys.py` suchen per `cogs/*.py`. Cogs werden in
`main.py:45–50` explizit registriert, neue Module werden also nicht versehentlich als Cog geladen.

## Regeln für alle Etappen

1. **Gepatchte Namen werden am alten Ort nicht re-exportiert.** `monkeypatch.setattr(dlmod, "DOWNLOAD_DIR",
   tmp_path)` wirkt nur in dem Modul, in dem der Code den Namen nachschlägt. Bliebe `DOWNLOAD_DIR` nach dem
   Umzug als Name in `cogs.downloader` stehen, liefe der alte Patch still ins Leere, und
   `tests/test_progressive_download.py:378–392` sowie `tests/test_filename_video_id.py:105 ff.` würden
   `_purge_stale_progressive()` gegen das echte `downloads/` aufrufen. Ohne Re-Export scheitert jeder
   vergessene Patch laut mit `AttributeError`. Das gilt für `DOWNLOAD_DIR`, `DOWNLOADS_MAX_MB`,
   `YDL_COOKIES_FILE`, `YDL_BROWSER`.
2. **Nur gelesene Konstanten dürfen re-exportiert werden** (`CACHE_TTL`, `METADATA_CACHE_FILE`,
   `PERSISTED_CACHE_FIELDS`, `FAST_PLAYER_CLIENTS`), so wie heute die `PROGRESSIVE_*`.
3. **Alle Log-Texte und Tags bleiben wörtlich.** `utils/logger.py:178–196` filtert nach den Tags,
   `tests/test_logger_mode.py` klassifiziert jeden INFO-Tag.
4. **Hilfs-Koroutinen werden nur direkt awaitet, nie als Task gestartet**, weil `_register_inflight` mit
   `asyncio.current_task()` arbeitet.
5. **Klassen-Defaults wandern mit ihren Methoden.** Kein neues Instanz-Attribut ohne Klassen-Default: Tests
   bauen Instanzen per `Downloader.__new__` ohne `__init__` (16 Aufbauten in 13 Testdateien) und weisen
   `_url_cache`, `_cache_timestamps`, `_pending_resolves`, `ydl` direkt zu.
6. **Verschiebe-Etappen prüfen mit `git diff --color-moved=dimmed-zebra`:** außer Importen und
   Klassenköpfen darf keine Zeile inhaltlich geändert sein.

---

## Etappe 1 — Cache und yt_dlp-Fabrik auslagern (wörtlich verschieben)

**Inhalt**

- Neu: `cogs/downloader_cache.py` und `cogs/downloader_ydl.py`. Methoden unverändert verschieben,
  Klassen-Defaults mitnehmen.
- `rebuild` und `rebuild_async` ziehen ins ydl-Modul und importieren `METADATA_CACHE_FILE` und
  `atomic_write_text` vorerst von dort, wo sie liegen. Dieser Übergangs-Import fällt in Etappe 3 weg.
- `DOWNLOAD_DIR` samt `mkdir` wird im ydl-Modul definiert. `downloader.py` importiert den Namen in dieser
  Etappe noch, weil der Cleanup noch dort liegt.
- `downloader.py`: `YDL_BROWSER` und `YDL_COOKIES_FILE` aus dem `config`-Import streichen; `CACHE_TTL`,
  `METADATA_CACHE_FILE`, `PERSISTED_CACHE_FIELDS`, `FAST_PLAYER_CLIENTS` re-exportieren.

**Testanpassungen (nur Pfade)**

- `tests/test_cookie_mode.py:13`, `tests/test_fast_player_client.py:12`: `dlmod` wird nur für `YDL_*` benutzt,
  also je eine Import-Zeile (`import cogs.downloader_ydl as dlmod`).
- `tests/test_filename_video_id.py:29–30, 61–62`, `tests/test_metadata_cache.py:59–60`: String-Pfad auf
  `cogs.downloader_ydl.…`.

**Invarianten:** Instanzen nur über `_build_ydl()` (ADR 0013), Extraktion nur über `extract_info_async`
(ADR 0010), gedebouncte Writes (ADR 0005). Nichts davon ändert sich inhaltlich.

**Testlauf**

```bash
pytest tests/test_downloader_cache.py tests/test_metadata_cache.py tests/test_cache_ttl_runtime.py \
  tests/test_atomic_write.py tests/test_rebuild_on_loop.py tests/test_cookie_mode.py \
  tests/test_fast_player_client.py tests/test_filename_video_id.py tests/test_prime_first_hit.py \
  tests/test_resolve_track.py -q
```

**Doku:** `docs/architecture.md:47–58`, Landkarte in `CLAUDE.md` (Zeilen 46–48), Dateibaum in
`README.md:98–99` und `README_DE.md:98–99`.

**Abnahme:** `ruff check .` sauber, Testlauf grün, Diff zeigt nur verschobene Zeilen.

## Etappe 2 — Cleanup und Prefetch auslagern (wörtlich verschieben)

**Inhalt**

- Neu: `cogs/downloader_cleanup.py` und `cogs/downloader_prefetch.py`.
- `downloader.py` trägt danach weder `DOWNLOAD_DIR` noch `DOWNLOADS_MAX_MB`.
- `cogs/music_stats.py:16`: Import-Zeile auf den neuen Ort von `DOWNLOAD_DIR`.
- Docstring in `cogs/downloader_progressive.py:6–8` anpassen: Der Satz „`_purge_stale_progressive` bleibt
  dort, weil es `DOWNLOAD_DIR` braucht" wird mit dieser Etappe falsch.

**Vor dem ersten Testlauf prüfen**

```bash
grep -n "DOWNLOAD_DIR\|DOWNLOADS_MAX_MB" cogs/downloader.py     # nur noch Kommentare
grep -rn 'dlmod, "DOWNLOAD' tests/                               # kein Treffer, dessen dlmod auf cogs.downloader zeigt
```

**Testanpassungen (nur Pfade)**

- `tests/test_inflight_registry.py:16`, `tests/test_downloads_cleanup.py:13`: `dlmod` auf
  `cogs.downloader_cleanup` umstellen. `dlmod.PROGRESSIVE_SIDECAR_SUFFIX` (`test_downloads_cleanup.py:150`)
  existiert dort, weil der Purge es importiert.
- `tests/test_progressive_download.py`: zwölf Patch-Zeilen auf das Cleanup-Modul;
  `dlmod.PROGRESSIVE_BLOCK_TTL` (`:308`) bleibt bei `cogs.downloader`, also ein zweiter Alias.
- `tests/test_filename_video_id.py:87–88, 106`: auf das Cleanup-Modul.
- `tests/test_downloader_cache.py:13`: `yt_video_id` aus `utils.autoplay` importieren.

**Invarianten:** kein nacktes `unlink` (die `safe_unlink`-Aufrufe ziehen unverändert um, ADR 0003), Lock in
`_prefetch_one` (`CLAUDE.md`, `playback.md:114–116`), Deregistrierung erst im `finally` des Workers
(ADR 0002).

**Testlauf**

```bash
pytest tests/test_downloads_cleanup.py tests/test_filename_video_id.py tests/test_progressive_download.py \
  tests/test_inflight_registry.py tests/test_autoplay_selection.py tests/test_cache_ttl_runtime.py \
  tests/test_safe_unlink.py tests/test_optional_voice_deps.py -q
```

**Doku:** wie Etappe 1; zusätzlich der Satz „`_purge_stale_progressive` bleibt im Downloader" in
`docs/architecture.md:58`.

**Abnahme:** wie Etappe 1; zusätzlich liefern die zwei grep-Befehle oben das erwartete Ergebnis.

## Etappe 3 — Vereinfachungen außerhalb des race-kritischen Kerns

**V4 Cache-Helfer**

```python
def _store_info(self, url, info):            # ersetzt :356–360, :389–393, :1080–1084
    if "entries" in info: info = info["entries"][0]
    self._url_cache[url] = info; self._cache_timestamps[url] = time.time(); self._cache_dirty = True
    return info
def _prune_cache(self, keep_urls=None): …    # ersetzt :179–185, :204–210, :432–434
def _snapshot_cache(self) -> str | None: …   # serialisieren, Fehler → None + Debug-Log
```

`warmup` und `prefetch_autoplay` behalten ihr `if info:` davor, `_fetch_info` bleibt ohne. `rebuild_async`
schrumpft damit von 44 auf rund 25 Zeilen; seine drei Abschnitte Loop, Thread, Loop bleiben. Danach den
Übergangs-Import aus Etappe 1 entfernen.

**V5 `_build_ydl` mit gemeinsamem Sockel (`:535–592`)**

```python
def _quiet_opts(self):
    return {"quiet": True, "no_warnings": True, "socket_timeout": 15,
            "js_runtimes": {"node": {}}, **self._cookie_opts()}
# search:   {**q, "extract_flat": True, "skip_download": True}
# autoplay: wie search + "noplaylist": False, "playlistend": 10
# url/playlist: {**q, "format": "bestaudio/best", "default_search": "ytsearch", **self._player_opts(fast), …}
```

Absicherung: **vor** und **nach** dem Umbau `sorted(ydl.params.items())` für alle Arten × `fast` ×
Cookie-Modus × Format plus die Progressiv-Instanz ausgeben und vergleichen. Aufbau wie `bare_downloader()` in
`tests/test_cookie_mode.py:17–30`, also per `__new__`, ohne `__init__`. Das Vergleichsskript ist ein
Wegwerf-Skript außerhalb des Repos, es wird nicht committet.

**V7 `cleanup_downloads` (`:603–665`):** `_drain_pending()` und `_protected_paths(urls, extra)` herausziehen,
die vier Abbruchbedingungen stehen dann untereinander. Die unterschiedliche Fehlerbehandlung der zwei
Schleifen (`OSError` gegen `Exception`, Sidecar nur in der zweiten) bleibt erhalten.

**V10 `extract_info_async` (`:449–481`):** nur die doppelte `wait_for(to_thread(…extract_info…))`-Zeile als
`attempt(ydl=None)` und die dreiteilige Bedingung als `_wants_client_fallback(e, kind)` benennen. Die Stelle,
an der jeder Zweitversuch awaitet wird, bleibt: `logger.exception` in `cogs/music_playback.py:419, 984`
schreibt Ausnahmeketten ins Log, ein Verschieben würde den Traceback-Text ändern.

**Invarianten:** Zustand von `rebuild_async` nur auf dem Loop (`tests/test_rebuild_on_loop.py` prüft genau
das), `defer=False` nur im Größen-Cleanup, Timeouts nicht wiederholen.

**Testlauf:** der Lauf aus Etappe 1 plus `tests/test_downloads_cleanup.py`.

**Doku:** keine, die Methodennamen der öffentlichen Oberfläche bleiben.

**Abnahme:** Testlauf grün, Parameter-Vergleich von V5 ohne Unterschied.

## Etappe 4 — Vereinfachungen im Kern (resolve-Phasen, Prefetch)

Reihenfolge und `await`-Grenzen der vier Phasen bleiben, ebenso die Stellen, an denen `last_resolved_file`
gesetzt wird.

**V1 `_acquire_file` flach (`:872–939`)**

```python
# vorher: if not exists: [if force: warten]; if not exists and force: laden;
#         if not exists: {progressiv | Stream} else: log  /  else: log
# nachher:
if r.filename.exists():
    logger.info("[Wiedergabe] Verwende vorhandene Datei: …"); return r.result(r.filename)
if r.force_download:
    await self._last_resort_download(r)      # fremden Writer abwarten, sonst selbst laden
if r.filename.exists():
    logger.info("[Wiedergabe] Prefetch erfolgreich – starte sofort: …"); return r.result(r.filename)
if not r.force_download and self._may_start_progressive(r.url, r.info):
    result = await self._play_progressive(r)
    if result is not None: return result
return self._stream_result(r, "[Stream] Nicht gecacht – …")
```

Gleich bis auf einen wegfallenden dritten `exists()`-Aufruf im Pfad ohne `force_download`, zwischen dem heute
kein `await` liegt. Die Rückgabe am Ende bleibt ohne erneutes Setzen von `last_resolved_file`.

**V2 `_adopt_inflight` teilen (`:810–853`)**

```python
prog_task = self.progressive_task_for(r.url)
if prog_task is None and (entry := self.inflight_download(r.url)):
    task, quelle = entry
    if quelle == "progressiv": prog_task = task
    else: logger.info(…); await self._settle(task)
return None if prog_task is None else await self._join_progressive(r, prog_task)
```

`_join_progressive` enthält die vier Ausgänge als frühe Rückgaben: Puffer steht, Task tot, zu langsam ohne
`force`, zu langsam mit `force`.

**V3 `_metadata_for` aus `_resolve_metadata` (`:769–785`)**

```python
info = self._usable_cached_info(url)
if info is not None: logger.info("[Resolve] Metadaten aus Cache: …"); return info
if (pending := self._pending_resolves.get(url)) is not None:
    logger.info("[Resolve] Warte auf laufenden Prefetch-Task: …"); await self._settle_plain(pending)
info = self._usable_cached_info(url)
if info is not None: logger.info("[Resolve] Metadaten aus Prefetch-Task: …"); return info
if url in self._url_cache: logger.info("[Resolve] Cache-Eintrag abgelaufen – …")
return await self._fetch_info(url)
```

Der Dauer-Vergleich (`:788`, `:804`) bleibt außerhalb des `try`, sonst ändert sich der Fehlerpfad.

**V6 `_remove_corpse` und `_settle`**

```python
def _remove_corpse(self, filename) -> bool:   # True = keine Leiche (mehr); ersetzt :860–864, :998–1005
async def _settle(self, task):                # try: await asyncio.shield(task) / except Exception: pass
```

Beide entstehen in dieser Etappe in `downloader.py`, der Prefetch ruft sie über `self`. Etappe 5 zieht sie
ins Progressive-Mixin um.

**V8 `prefetch_autoplay` (`:1030–1104`):** `_autoplay_query(ref_url, ref_title)` (rein),
`_pick_autoplay(…)` (Suche und Auswahl) und `_download_autoplay(url, title)` (Laden, Cachen, Cleanup mit
`extra_protected`). Registrierung und `finally` bleiben im Rumpf.

**V9 `_prefetch_one` (`:974–1028`):** `_prefetch_target(url, title)` liefert `(info, filename | None)`,
danach frühe Rückgaben. Der Lock mit Doppel-Check bleibt in `_prefetch_one`, weil `CLAUDE.md`, `playback.md`
und ADR 0002 ihn dort verorten.

**Invarianten:** pro URL ein Writer, lebendig oder tot nur über die Registry, wachsende Datei nie unlinken
(ADR 0002), Lock in `_prefetch_one`.

**Testlauf**

```bash
pytest tests/test_resolve_track.py tests/test_inflight_registry.py tests/test_progressive_download.py \
  tests/test_cache_ttl_runtime.py tests/test_metadata_cache.py tests/test_autoplay_selection.py \
  tests/test_prime_first_hit.py tests/test_music_playback.py -q
```

**Doku:** `docs/architecture/playback.md:74–75, 82–83` nennt die vier Phasen namentlich; sie bleiben. Neue
Untermethoden dort ergänzen.

**Abnahme:** Testlauf grün; in `resolve_track` stehen die vier Phasen in unveränderter Reihenfolge.

## Etappe 5 — Progressive-Zustand ins Progressive-Mixin (vom Nutzer freigegebener Zusatzschnitt)

Diese Etappe ändert `cogs/downloader_progressive.py` über Import-Zeilen hinaus. Der Nutzer hat das
ausdrücklich freigegeben.

**Inhalt**

- Die fünf Klassen-Defaults `_progressive`, `_progressive_files`, `_incomplete_files`,
  `_progressive_blocked`, `_inflight` samt ihren Kommentaren (`cogs/downloader.py:131–140`) ziehen in die
  Klasse `ProgressiveDownloadMixin`.
- Ihre Anlage (`:157–161`) wird eine Methode `_init_progressive_state()` im Mixin, die `__init__` an
  derselben Stelle aufruft. Die Reihenfolge in `__init__` bleibt: Zustand anlegen, dann
  `_purge_stale_progressive()`, dann Cache laden, dann `_init_ydl()`.
- `_remove_corpse` und `_settle` (aus Etappe 4) ziehen ins Progressive-Mixin.
- Modul-Docstring von `downloader_progressive.py` auf den neuen Stand bringen.

**Invarianten:** In-Flight-Registry als einzige Quelle für lebendig oder tot (ADR 0002); Klassen-Defaults
bleiben `None`, alle Leser bleiben None-tolerant.

**Vor dem Umbau prüfen:** ob Tests die Defaults über die Klasse `Downloader` lesen oder patchen
(`grep -rn "Downloader\._progressive\|Downloader\._inflight" tests/`). Über die MRO bleiben sie erreichbar;
ein Patch auf Klassenebene wirkt weiterhin.

**Testlauf:** `pytest tests/test_progressive_download.py tests/test_inflight_registry.py
tests/test_resolve_track.py tests/test_downloads_cleanup.py tests/test_prime_first_hit.py -q`

**Doku:** `docs/architecture.md` (Abschnitt Downloader), ADR 0002 nur dann, wenn dort der Ort der
Deklaration genannt wird (per grep prüfen).

**Abnahme:** Testlauf grün; in `downloader.py` steht kein Progressive-Attribut mehr außer
`last_resolved_file`.

## Abschluss der Etappen 1–5

1. Volle Suite: `pytest` und `ruff check .` (entspricht der CI in `.github/workflows/tests.yml`).
2. Der Nutzer geht die Abschnitte Download und Prefetch der `docs/smoke-checklist.md` von Hand durch.
3. Doku systematisch nachziehen: `docs/architecture.md`, `docs/architecture/playback.md`, Landkarte in
   `CLAUDE.md`, Dateibaum in `README.md` und `README_DE.md`. Für Variante A ist keine neue ADR nötig.
4. Push.

---

## Etappe 6 — Effizienz (optional, **nicht verhaltensgleich**, Einzelfreigabe nötig)

Beide Punkte ändern Timing, nicht das Ergebnis. Jeder Punkt braucht vor der Umsetzung ein ausdrückliches
Ja des Nutzers und eine Messung vorher und nachher.

**6a: `_prefetch_one` wartet auf einen laufenden Resolve.**
`cogs/music_playback.py:1055` startet `_start_resolve(url)`, kurz danach kickt `cogs/music.py:487` den
Prefetch. `_prefetch_one` (`:979–983`) prüft nur den Cache, nicht `_pending_resolves`, und extrahiert parallel
ein zweites Mal auf derselben `self.ydl`-Instanz. `_resolve_metadata` wartet an der gleichen Stelle auf den
laufenden Resolve (`:773–778`). Kosten heute: eine Extraktion (laut ADR 0013 rund 1,3 s) im Thread, keine
hörbare Verzögerung. Der Frisch-Pfad loggt nichts, deshalb fehlt bisher der Log-Beleg; vor der Umsetzung
einen Debug-Log einbauen und den Fall im echten Betrieb nachweisen. Empfehlung der Analyse: umsetzen.

**6b: `rebuild` für `!format` und `!reloadcookies` asynchron.**
Der yt_dlp-Konstruktor läuft auf dem Event-Loop (gemessen 24 ms je Instanz, die erste 71 ms):

- `enable_cookie_mode` (`:435`): fünf Instanzen, rund 120 ms, einmal pro Prozess.
- `rebuild` (`:177–186`): fünf Instanzen plus synchroner Datei-Write; `!reloadcookies` baut über
  `cogs/music.py:289–290` zweimal.
- Client-Rückfall (`:475`): eine Instanz.
- `_make_progressive_ydl` je Songstart: eine Instanz (`downloader_progressive.py:236`).

Der Ton läuft im Player-Thread und ist nicht betroffen. `rebuild` verletzt dem Wortlaut nach „kein
blockierendes I/O im Event-Loop". Die Behebung ändert die Aufrufer in `cogs/music.py`. Empfehlung der
Analyse: nur umsetzen, wenn der Bot bei diesen Befehlen merklich hängt.

**Nicht einplanen, nur notiert:** `downloader_progressive.py:135` (`prime_first_hit`) nimmt
`_url_cache.get(url)` ohne TTL-Prüfung; bei einem über zwei Stunden alten Eintrag ohne Datei folgen ein
gescheiterter Schnellstart und zwei frische Extraktionen. Enger Fall.

**Geprüft und ohne Befund (gemessen):** `prepare_filename` 0,1 ms, `deepcopy` eines vollen Info-Dicts
0,08 ms, die `exists()`- und `stat()`-Aufrufe im resolve-Pfad, der 0,2-s-Poll in `_wait_for_buffer`,
`_serialize_cache` alle 30 s nur bei Änderung.

---

## Nicht anfassen

- **Vier Phasen und ihre Reihenfolge** in `resolve_track` (`:741–751`): Registry zuerst, Leiche erst danach
  (ADR 0002).
- **`prefetch_next`** (`:959–972`): eigener Task je URL, Shield, `worker.cancel()` bevor der Worker eine
  Zeile lief (ADR 0002, `tests/test_inflight_registry.py:225–266`).
- **Lock mit Doppel-Check und lazy Anlage** in `_prefetch_one` (`:1007–1017`).
- **Dreiteilung von `rebuild_async`** und die Prüfung `self.ydl is not replaced` (`:225–229`, ADR 0005).
- **Späte Auflösung über `_YDL_KINDS`** und `except asyncio.TimeoutError: raise` vor `except Exception`
  (`:459–467`, ADR 0010).
- **Klassen-Defaults mit `None`** und None-tolerante Leser.
- **`_make_progressive_ydl`**: Wegwerf-Instanz, `nopart`, identisches `outtmpl` (`:521–533`, ADR 0001,
  ADR 0003).
- **Cleanup**: doppelter Schutz über Pfad und Titel-Stem, Drain vor der Flag-Prüfung, alles schlucken,
  `defer=False` (`:614–696`, ADR 0003).
- **`last_resolved_file` schon in Phase 1** (`:792–796`) und die Rückgabe ohne erneutes Setzen (`:938–939`).
- **Direkter Aufruf `self.ydl.extract_info(url, download=True)`** im Autoplay-Prefetch (`:1075–1078`):
  dokumentierte Ausnahme (ADR 0013, `playback.md:57–58`).
- **„Task nicht canceln"** nach Puffer-Timeout (`:920–923`, ADR 0001).
- **`_serialize_cache` auf dem Loop**, `PERSISTED_CACHE_FIELDS`, tolerantes `_load_cache` (ADR 0005).

Ausdrücklich gut, so wie es ist: `_Resolution` mit den vier Phasen, `_usable_cached_info`, die Trennung
Registry gegen Datei-Heuristik, und die Kommentare, die das Warum erklären.

---

## Folgeprojekt: Variante B (eigene Klassen, Komposition)

Nicht Teil dieses Plans. Variante A verbaut B nicht: Die Mixin-Grenzen sind dieselben Nähte, ein Mixin lässt
sich später einzeln zur Komponente machen.

**Zielklassen**

| Klasse | Eigener Zustand | Entsteht aus |
|---|---|---|
| `YdlPool` | Format, Cookie-Modus, fünf Instanzen | `YdlMixin` |
| `MetadataCache` | Einträge, Timestamps, Dirty-Flag, wartende Resolves | `MetadataCacheMixin` |
| `DownloadsJanitor` | keine eigenen Daten, liest Cache und Registry | `DownloadsCleanupMixin` |
| `Downloader` | `last_resolved_file`, Registry | behält resolve und Prefetch, delegiert |

**Was dagegen steht**

- 16 `Downloader.__new__`-Aufbauten in 13 Testdateien weisen den Zustand direkt zu; in den Tests stehen
  rund 60 Zugriffe auf `_url_cache`, 36 auf `_cache_timestamps`, 27 auf `_pending_resolves`, 45 auf `.ydl`.
- `tests/test_rebuild_on_loop.py:16–40` überwacht per `__setattr__` am Downloader, in welchem Thread Zustand
  geschrieben wird. Liegt der Zustand in Komponenten, sieht der Test nichts mehr.
- `cogs/downloader_progressive.py` liest `self._url_cache`, `self._pending_resolves`, `self.ydl`,
  `self.audio_format` und ruft `_fetch_info`, `_make_progressive_ydl`, `enable_cookie_mode`,
  `cleanup_downloads`; `cogs/music.py:838` liest `self.dl._url_cache`.
- Die Komponenten brauchen einander: `_usable_cached_info` braucht `prepare_filename` und `is_incomplete`,
  `enable_cookie_mode` leert den Cache, der Cleanup braucht Cache, Instanz, `last_resolved_file` und
  `_progressive_files`.

**Voraussetzungen, die der Nutzer dafür lockern müsste:** Tests dürfen inhaltlich umgeschrieben werden
(Aufbau-Helfer in 13 Dateien), `downloader_progressive.py` und `music.py` dürfen sich ändern.

**Grober Ablauf**

1. Gemeinsamen Test-Aufbau-Helfer einführen, der die 16 `__new__`-Aufbauten ersetzt (noch auf der
   Mixin-Struktur, damit die Tests vor dem Umbau grün sind).
2. `MetadataCache` als erste Komponente herauslösen, `Downloader` delegiert über schmale Methoden.
3. `YdlPool` herauslösen; `tests/test_rebuild_on_loop.py` auf die Komponente umstellen und die
   Loop/Thread-Trennung von `rebuild_async` neu nachweisen.
4. `DownloadsJanitor` herauslösen.
5. Neue ADR 0014 „Downloader als Komposition", die den Schnitt und die Zugriffsregeln festhält.

**Aufwand und Risiko:** zwei bis drei Tage, Risiko hoch, weil die Loop/Thread-Trennung und die
Registry-Zugriffe neu nachgewiesen werden müssen. Lohnt sich, wenn weitere Aufrufer außerhalb der Cog den
Cache oder die yt_dlp-Instanzen einzeln brauchen oder wenn der Zustand auf `self` erneut unübersichtlich
wird.

---

## Anhang: Wahrscheinliche Bugs (nicht Teil des Refactorings)

Alle aus der Lektüre geschlossen, keiner reproduziert. Nach dem Refactoring beheben, je ein Fix-Commit mit
Test. Empfohlene Reihenfolge: 2, 1, 5, 4, dann der Rest.

1. **Autoplay-Prefetch meldet sich ab, während der Thread noch schreibt.** `prefetch_autoplay` registriert
   den aufrufenden Task und deregistriert im `finally` (`:1070–1096`). Wird der Task gecancelt
   (`cogs/music_playback.py:484–485, 916–918`, `cogs/music.py:778–779`, `cogs/music_radio.py:92–94`) oder
   läuft das 120-s-Timeout ab, schreibt der Worker-Thread weiter, die Registry meldet aber „kein Writer".
   Das ist die Lücke, die ADR 0002 für den Queue-Prefetch geschlossen hat. Auslöser zum Beispiel `!eq` kurz
   nach Songstart mit Autoplay und leerer Queue.
2. **mp3-Modus findet seine Dateien nie** (auch in yt_dlp `YoutubeDL.py:3468–3470` nachgelesen, nicht
   ausgeführt). `prepare_filename` liefert `….webm`, auf der Platte liegt nach dem Postprocessor `….mp3`;
   `final_ext` wird nirgends gesetzt. Folge: `resolve_track` streamt immer, `_prefetch_one` lädt und
   konvertiert bei jedem Kick neu, der Download-Fallback endet im Stream.
3. **`self.ydl` wird aus mehreren Threads gleichzeitig benutzt.** Der Lock serialisiert nur
   Queue-Prefetch-Downloads untereinander. Ohne Lock laufen der Download-Fallback (`:896`), der
   Autoplay-Download (`:1076`) und jede Extraktion über `_fetch_info`. Die Invariante „yt_dlp nicht
   thread-safe" ist enger umgesetzt, als sie klingt; ob das praktisch schadet, ist offen.
4. **`duration: None` (Vermutung).** `_Resolution.duration` (`:108–109`) und `_prefetch_one` (`:984`) nutzen
   `info.get("duration", 0)`; ist der Schlüssel mit `None` vorhanden (Livestreams, manche Extraktoren),
   wirft der Vergleich in `:788` einen `TypeError`. An zwei anderen Stellen steht die robuste Form
   `(info.get("duration") or 0)` (`:257`, `downloader_progressive.py:154`).
5. **`clear_cache()` leert nur halb.** `:233–234` leert `_url_cache`, nicht `_cache_timestamps`, und setzt
   `_cache_dirty` nicht. Nach `!clear` bleibt `metadata_cache.json` unverändert, nach einem Neustart sind die
   Einträge wieder da. `playback.md:59` sagt „wipes entirely".
6. **Gecancelter fremder Task reißt `resolve_track` mit (Vermutung, enger Fall).**
   `await asyncio.shield(entry[0])` fängt nur `Exception` (`:829–832`, `:885–888`); wird der adoptierte
   Autoplay-Prefetch gecancelt, trifft der `CancelledError` den wartenden `resolve_track`.
7. **`enable_cookie_mode` leert `_pending_resolves`, ohne die Tasks zu beenden** (geringe Wirkung). Laufende
   cookielose Resolves schreiben ihr Ergebnis danach in den frisch geleerten Cache (`:358`).

## Kickoff-Prompt für die frische Session

> Setze `docs/plans/2026-11-refactoring-downloader.md` um, Etappen 1 bis 5 strikt in Reihenfolge. Prüfe
> Zeilennummern vorher per grep. Nach jeder Etappe `ruff check .`, den genannten gezielten Testlauf und
> einen Commit direkt auf main; die volle pytest-Suite nur einmal am Ende. Halte dich an die
> Nutzer-Entscheidungen, die Regeln für alle Etappen und den Abschnitt „Nicht anfassen". Etappe 6 nicht
> umsetzen, sondern mir am Ende zur Einzelfreigabe vorlegen.
