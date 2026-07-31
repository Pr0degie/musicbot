# Plan: Beste Version — Regressions-Fixes + Wunsch-Features

> **Für die ausführende Session:** Dieser Plan ist das destillierte Ergebnis eines vollständigen
> Historien-Audits (alle 162 Commits, vier parallele Analyse-Agenten) plus fünf Interview-Runden
> mit dem Nutzer am 31.07.2026. Alle Datei-/Zeilenangaben beziehen sich auf HEAD `3b0c291`.
> Zeilennummern vor der Umsetzung per grep verifizieren — sie verschieben sich mit jedem Block.
> Blöcke strikt in Reihenfolge abarbeiten, nach jedem Block: `ruff check .` + `pytest` + Commit
> direkt auf main (deutsche Commit-Messages im Stil der Historie). Push am Ende.

## Ziel & Prioritäten des Nutzers

Der Bot soll „die bestmögliche Version seiner selbst" werden, ohne weitere Funktionalität zu
zerschießen. Die zwei Top-Prioritäten (explizit gewählt):

1. **Nahtlose Übergänge** — keine Lücke zwischen Songs, Skip ohne Warten. Harter Schnitt ok, kein Crossfade nötig.
2. **Autoplay-Qualität** — stundenlang passende Songs, keine Cover/Varianten-Wiederholungen.

Nutzung: allein + kleiner Freundeskreis + DM-Sessions. Beide Hörmodi (Queue / Autoplay) gleich wichtig.

## Nutzer-Entscheidungen aus dem Interview (bindend)

| Thema | Entscheidung |
|---|---|
| Autoplay-Varianten | **Alle** Varianten desselben Songs innerhalb der letzten ~15 Tracks blocken: Cover, Live, Remix, Sped-up/Nightcore |
| Autoplay-Seed | Immer der **zuletzt gespielte Song** (kein Session-Mix, kein Anker) |
| Autoplay-Auswahl | Sinniger Nachfolger („kein Jazz nach Techno"), Mix-Top-Picks bevorzugen; etwas Genre-Drift ok; keine Schleifen |
| Queue-Anzeige | **Nächsten Autoplay-Song in `!q` anzeigen** (eine Vorschau-Zeile, nicht mehrere) |
| Terminal | **Zwei Modi**: ruhig (nur echte Probleme) / volle Diagnose. `LOG_MODE` in `.env` als Start-Default + `!debug`-Befehl zur Laufzeit. **`bot.log` schreibt immer volle Diagnose** |
| Chat-Hygiene | **Keine einzige neue Kanalnachricht.** Pause/Resume-Textbefehle bleiben bewusst stumm (die in `0484b44` verlorenen Bestätigungen NICHT wiederherstellen — Nutzer will das so) |
| Fortschrittsbalken | Auf **5-Sekunden-Takt** (gegen 429), 21 Schritte bleiben |
| EQ | `punchy` bleibt Default — nicht anfassen |
| Session-Queue | Neuer Befehl **`!loadq last`** lädt `last_queue.json`. KEIN Auto-Restore beim Start (bewusst leer starten bleibt) |
| yt-dlp | **Update-Check beim Start, nur Log-Hinweis.** Kein Auto-Update |
| Verschoben (NICHT bauen) | Stream-First-Experiment (cookieloser Direkt-Stream), DM-Bridge-Ducking, Crossfade |
| Absicherung | pytest + headless A/B-Worktree-Diff pro Block; Nutzer macht am Ende die manuelle `docs/smoke-checklist.md` |

---

## Block 1 — Quick-Wins

### 1.1 `_stopped_by_user`-Leck fixen (wichtigster Einzeiler des Plans)

**Befund:** `e553a92` überlud `_stopped_by_user` (gesetzt in `!stop` **und `!x`/Pause**) mit der
Autoplay-Unterdrückung, aber **`!resume` setzt es nie zurück**. Reset passiert nur in
`_snapshot_track_state` (`cogs/music_playback.py:632`) und `cogs/music_radio.py:69`.
Folgen nach Pause→Resume bis zum Songende: kein Autoplay am Songende (`music_playback.py:385`),
`_finalize_progress_bar` übersprungen (`:363`), **Progressive-Seek-Resume unterdrückt (`:505`,
Song kann abbrechen!)**, Reconnect-Watchdog inaktiv (`music_voice_ui.py:96`).

**Fix:** `self._stopped_by_user = False` in `resume()` (`cogs/music.py:562–576`) **und** im
Resume-Button (`views/music_controls.py:~65`). Test ergänzen (Pause→Resume→Songende → Autoplay feuert).

### 1.2 Skip-Fehlalarm im Terminal

**Befund:** `cogs/music_playback.py:479` — `if error or elapsed < 2.0:` löst die gelbe
FFmpeg-stderr-Warnung auch aus, wenn der Nutzer in den ersten 2 s skippt/stoppt.

**Fix:** Bedingung um die vorhandenen Marker ergänzen (User-Stop / `_stop_for_advance`-Marker —
im Code nachsehen, wie Skip vs. echter Fehler unterscheidbar ist; `suppress_resume` existiert).
Kein Warn-Log, wenn der Abbruch gewollt war.

### 1.3 Fortschrittsbalken 2 s → 5 s

`cogs/music_voice_ui.py` (~Zeile 200, `_progress_loop`-Intervall). 21 Schritte bleiben,
Endsprung-Verhalten (`7707951`) bleibt.

---

## Block 2 — Prefetch & Übergänge (Top-Priorität 1)

**Harte Invariante dabei:** yt_dlp ist nicht thread-safe → Prefetch-Downloads bleiben
**sequenziell**. Registry-Invariante (ADR 0002) gilt weiter — wir reparieren sie, wir lockern sie nicht.

### 2.1 `wait_progressive_idle()`-Parkplatz entschärfen (Hauptursache „Prefetch tot")

**Befund:** Seit `8c15622` ist die erste Zeile von `_prefetch_upcoming` ein
`await self.dl.wait_progressive_idle()` (`cogs/music_playback.py:60`) — globale Warteschleife
(bis 300 s), bis **kein** progressiver Download mehr läuft. Da der laufende Song fast immer
progressiv lädt, parkt der Queue-Prefetch bei jedem Songstart. Der Kick aus `afc7560` wird
dadurch neutralisiert: Der geparkte Task lebt, `_kick_prefetch`s
`if self.prefetch_task and not self.prefetch_task.done(): return` macht alle Folge-Kicks zu No-Ops.
Asymmetrie: `prefetch_autoplay` wartet **nicht**.

**Fix-Richtung:** Nicht global warten. Vorschlag: nur warten, solange der progressive Download
des **aktuell spielenden** Tracks jünger als ~15–20 s ist (Startphase schützen, damit der erste
Puffer schnell steht), danach Prefetch sequenziell starten lassen — oder Wartezeit hart kappen
(z. B. 20 s statt 300 s). Beim Implementieren die `_progressive`-Registry in `cogs/downloader.py`
ansehen und die einfachste Variante wählen, die den Sofortstart des laufenden Songs nicht
verlangsamt. Ziel-Verhalten: Während Song N läuft, wird N+1 (und danach N+2) wirklich geladen.

### 2.2 Cancel-Loch der In-Flight-Registry schließen

**Befund:** `play_next` (`music_playback.py:300–301`), `!clear` (`music.py:722–723`) und
Radio-Start (`music_radio.py:65–66`) rufen `prefetch_task.cancel()`. `_prefetch_next` lädt via
`await asyncio.to_thread(self.ydl.download, …)`; Cancel beendet den Worker-Thread **nicht**,
löst aber `finally: self._inflight_done(url, …)` aus (`downloader.py:1128–1129`). Ergebnis:
Registry meldet „kein Writer", während der Thread weiterschreibt → zweiter Download auf dieselbe
Datei möglich — exakt die Kollision, die ADR 0002 ausschließen soll.

**Fix-Richtung:** Deregistrierung erst, wenn der Worker-Thread wirklich fertig ist — z. B. den
`to_thread`-Future shielden und `_inflight_done` in dessen Done-Callback verlagern, oder beim
Cancel den laufenden Download zu Ende laufen lassen (adoptieren statt killen — passt zur
bestehenden Adoptions-Philosophie von ADR 0002). **Test ergänzen:** Cancel-Fall in
`tests/test_inflight_registry.py` (fehlt dort komplett). ADR 0002 um den Cancel-Pfad ergänzen.

### 2.3 Songwechsel wartet auf den falschen Task

**Befund:** Der Registry-Eintrag (`entry[0]`) ist der **gesamte** `_prefetch_upcoming`-Task;
`resolve_track` macht `await asyncio.shield(entry[0])` (`downloader.py:957–967`). Der Übergang
zu Song N+1 kann so am Download von Song N+2 hängen.

**Fix:** Pro URL den **einzelnen** Download-Task/-Future registrieren, nicht den Sammel-Task.
`resolve_track` wartet dann nur auf die konkret gebrauchte URL.

### 2.4 Verlorene Prefetch-Trigger nachrüsten

- `!shuffle` / `!move` / `!remove` (`cogs/music.py:730–797`): bauen die Queue um, stoßen aber nie
  Prefetch neu an — und der alte Prefetch zielt auf die alte Reihenfolge. Hier reicht kein
  `_kick_prefetch` (No-Op-Guard!): laufenden Task **sauber** canceln (erst nach Fix 2.2!) und neu kicken.
- Alternativ-Buttons B/C (`views/music_controls.py:145–190`, `_make_callback`): ersetzen/queuen
  eine URL ohne Kick und ohne `prime_first_hit` → nachrüsten.
- `!loadq` (`cogs/music_queue_io.py:79–81`): startet bei leerer Wiedergabe ohne
  `prime_first_hit`-Schnellstart (den `3b0c291` nur in die `!p`-Pfade gebaut hat; vgl.
  `music_playback.py:799`, `music.py:450`) → nachrüsten.

---

## Block 3 — Autoplay-Qualität (Top-Priorität 2)

**Ist-Zustand (funktioniert, nur zu schwach):** Seed-URL → YouTube-Mix `list=RD{id}` via
`autoplay_ydl` (extract_flat; Kommentar sagt „max. 6", Code sagt `playlistend: 10` —
`downloader.py:483/490`, Kommentar fixen) → `select_autoplay_candidates` (`downloader.py:110–135`,
dreistufige Kaskade) → `random.choice(pool)` mit `pool = candidates[1:]` (`downloader.py:1157`).
Historie: `_recently_played` (URLs) + `_recently_played_titles`, beide `maxlen=15`
(Appends: `music_playback.py:314, 329, 661`).

### 3.1 Duplikat-/Varianten-Erkennung verschärfen

**Befund:** `is_seen` (`downloader.py:104–106`) nutzt einen **einseitigen** Subset-Test —
ein Kandidat mit auch nur einem Zusatzwort („Africa (Toto Cover) — **Alex Melton**") gilt nie
als Duplikat. Zusätzlich kollabiert `normalize_title` (`utils/text.py:19–20`, aus `1c6c9c6`)
Titel mit `" | "` ohne `" - "` auf `segments[0]` → Mini-Wortmengen, gegen die nichts mehr matcht.

**Fix:**
1. `normalize_title`: Segment-Wahl reparieren — gibt es kein Segment mit `" - "`, den **ganzen**
   Titel normalisieren statt nur `segments[0]` (Idempotenz-Test in `tests/test_text.py:60` beachten).
2. `is_seen` ersetzen durch **bidirektionalen Overlap**: Duplikat, wenn
   `|A ∩ B| / min(|A|, |B|)` ≥ ~0,6 gegen irgendeinen Historien-Titel (Schwellwert per Tests mit
   den Beispielen aus dem Audit kalibrieren: „Toto - Africa" vs. „Africa (Toto Cover) - Alex Melton"
   → muss blocken; zwei echte verschiedene Songs desselben Künstlers → darf nicht blocken).
3. **Varianten-Keywords**: Enthält der Roh-Kandidatentitel `cover|live|remix|sped up|nightcore|
   slowed|reverb|acoustic|instrumental|karaoke|8d` (case-insensitive, auch in Klammern — vor dem
   Klammer-Strip prüfen!) **und** die Kern-Wortmenge überlappt mit einem Historien-Titel → blocken.
4. Kaskadenstufe 2 (`downloader.py:110–135`) verschärfen: auch dort Varianten des Referenz-Tracks
   ausschließen, nicht nur die exakte Video-ID (sonst hebelt die Kaskade den Filter wieder aus).
5. **Tests aktualisieren:** `tests/test_autoplay_selection.py` zementiert die alte einseitige
   Subset-Semantik (`test_prefetch_filters_by_title_word_subset`) — bewusst umschreiben, nicht
   drumherum arbeiten. Neue Fälle für Cover/Live/Remix/Sped-up ergänzen.

### 3.2 Auswahl: Mix-Top-Picks bevorzugen

**Befund:** `178cfc0` überspringt YouTubes Platz 1 (`candidates[1:]`) und würfelt gleichverteilt —
greift gezielt in den Cover-/Live-Bereich des Mixes.

**Fix:** Nach dem (jetzt scharfen) Dedup aus den verbleibenden Kandidaten **gewichtet vorne**
wählen: Platz 1 wieder zulassen, z. B. Gewichte 4/3/2/1 über die ersten vier ungesehenen
Kandidaten. Das liefert „sinnigen Nachfolger" (Mix-Ranking = Genre-Kohärenz), die 15er-Historie
verhindert Schleifen. Charakterisierungs-Tests entsprechend anpassen.

### 3.3 Autoplay-Vorschau in `!q`

Wenn `_prefetch_autoplay` seinen Kandidaten aufgelöst hat, `(url, title)` am Cog halten
(z. B. `self.autoplay_next`), bei Songwechsel/Autoplay-Off zurücksetzen. `views/queue_view.py`
(`build_content`) zeigt bei aktivem Autoplay und vorhandenem Kandidaten eine Zeile
„🔮 Als Nächstes (Autoplay): {title}". **i18n-Key in BEIDEN `locales/*.json`** anlegen
(`tests/test_i18n_keys.py` erzwingt Paritität). Keine neue Kanalnachricht — nur `!q`-Inhalt.

---

## Block 4 — Neue Features

### 4.1 Zwei Terminal-Modi (`LOG_MODE` + `!debug`)

- `config.py`: `LOG_MODE` aus `.env` lesen (`quiet`|`debug`, Default `quiet`).
  **Invariante: `config.py` ruft NIE `logging.basicConfig()`** — Logger kommt aus `utils.logger`.
- `utils/logger.py`: Console-Handler bekommt Level nach Modus — `quiet` = nur WARNING+ („nur echte
  Probleme"), `debug` = alles wie heute. **File-Handler (`bot.log`) bleibt immer voll.**
  Umschalt-Funktion `set_console_mode(mode)` bereitstellen.
- `cogs/basic.py`: `!debug on|off` (bzw. `!debug` = Status anzeigen) ruft `set_console_mode`.
  Antwort kurz halten (eine Zeile) — Chat-Hygiene.
- Wechselwirkung: Die INFO-Nachweis-Logs (`400d83b`, Download-Kollisionen) und Cache-/Song-Zeilen
  verschwinden im Quiet-Modus automatisch — gewollt.

### 4.2 `!loadq last`

`cogs/music_queue_io.py`: Sondername `last` in `loadq` → lädt `last_queue.json`
(Schreibstelle: `music_playback.py:581–587`; Format prüfen — Validierung wie `c176c56`:
Liste von `[str, str]`-Paaren, Limit). Der Name `last` ist damit für gespeicherte Playlists
reserviert — in `saveq` abfangen und in `!help`-Text (`help.text`, beide Locales) erwähnen.
Auto-Restore beim Start bleibt bewusst aus (Memory + `docs/architecture.md:24` bestätigen das).

### 4.3 yt-dlp-Update-Check beim Start

Nach `on_ready` als Hintergrund-Task (kein blockierendes I/O im Event-Loop — Invariante!):
installierte Version (`yt_dlp.version.__version__`) gegen PyPI
(`https://pypi.org/pypi/yt-dlp/json`, aiohttp, Timeout ~5 s, Fehler still schlucken) vergleichen →
bei neuerer Version **eine** Log-Zeile (WARNING, damit sie auch im Quiet-Modus sichtbar ist:
„yt-dlp X.Y verfügbar, installiert ist A.B — Update empfohlen bei YouTube-Problemen").

---

## Absicherung (nach JEDEM Block)

1. `ruff check .` und `pytest` — beides muss grün sein.
2. Headless A/B-Worktree-Diff gemäß Projekt-Memory `project_smoke_checkliste`
   (**`.env` in den Worktree mitkopieren!**): alten Stand vs. neuen Stand headless starten,
   Log-Diff prüfen.
3. Commit direkt auf `main` (kein Branch — Nutzer-Vorgabe), deutsche Message im Historien-Stil
   (`music: …` / `fix: …` / `feat: …`), Co-Authored-By-Zeile. Push am Ende aller Blöcke.
4. Nach Block 4: Doku nachziehen —
   - `docs/architecture.md`: `normalize_title`-Beschreibung (~Zeile 94) ist veraltet (beschreibt
     weder Pipe-Split noch Subset) → auf neue Semantik aktualisieren; Logging-Abschnitt um
     LOG_MODE/!debug ergänzen; Prefetch-Abschnitt (kein globales Idle-Warten mehr).
   - ADR 0002 um den Cancel-Pfad ergänzen.
   - `CLAUDE.md`-Invarianten prüfen/ergänzen (Registry-Formulierung bleibt gültig; ggf.
     `autoplay_next`-Lifecycle als Invariante).
   - `docs/smoke-checklist.md` um neue Checks erweitern: Pause→Resume→Autoplay, `!debug`,
     `!loadq last`, Autoplay-Vorschau in `!q`, 5-s-Balken.
5. Dem Nutzer am Ende die manuelle Smoke-Checkliste ansagen (er testet selbst in Discord).

## Gefahrenzonen (Invarianten, die diese Blöcke berühren)

- Queue-Tupel `(url, title)` vs. `current_track` 3-Tupel — bei `!loadq last` und Re-Add entpacken.
- `_inflight`-Registry ist die einzige Wahrheit über lebende Downloads (ADR 0002) — Block 2 ändert
  ihre Mechanik, nicht ihre Semantik.
- Track-Zustand vor `vc.play()`; nach jedem Post-Play-await `_track_generation` prüfen (ADR 0007) —
  betrifft 2.1/2.4, wenn Code um `play_next` bewegt wird.
- `after_playing`: Retry-/Resume-Marker-Reset NACH dem Progressive-Resume-Block — bei 1.2 nicht
  versehentlich umsortieren.
- Neue `t()`-Keys immer in beiden Locales (3.3, 4.2).
- Kein `logging.basicConfig()` in `config.py` (4.1); kein blockierendes I/O im Event-Loop (4.3).
- Prefetch bleibt sequenziell — yt_dlp nicht thread-safe (2.1).

## Kickoff-Prompt für die frische Session

> Setze `docs/plans/2026-08-beste-version.md` um. Arbeite die Blöcke strikt in Reihenfolge ab
> (Block 1 zuerst), nach jedem Block ruff + pytest + Commit direkt auf main. Halte dich an die
> Nutzer-Entscheidungen und Gefahrenzonen im Plan.
