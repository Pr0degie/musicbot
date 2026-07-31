# Smoke-Checkliste (manuell, End-to-End)

Nach jedem substanziellen Prompt/Refactoring manuell durchgehen — die Tests decken Playback/Voice nicht ab. Headless-Variante: A/B-Worktree-Diff (`.env` in den Worktree mitkopieren!).

## Grundfunktionen

- [ ] `!j` → Bot joint den Voice-Channel; `!l` → verlässt ihn
- [ ] `!p <Suchbegriff>` → erster Treffer spielt sofort, Alternativen erscheinen als Buttons (`SearchAutoplayView`, 30 s Timeout)
- [ ] Klick auf einen Alternativen-Button → Alternative spielt
- [ ] `!p <URL>` → Track wird eingereiht; erneutes `!p` derselben URL → Duplikat-Warnung
- [ ] `!p <Playlist-URL>` → mehrere Einträge landen in der Queue
- [ ] Songwechsel → die Karte des vorherigen Songs wird zur Zeile `🎶 Titel` (kein Embed, keine Buttons mehr); **genau eine** Karte im Channel, die des laufenden Songs
- [ ] Auch nach Queue-Ende + neuem `!p` und nach `!radio` (Übernahme von Musik) bleibt keine alte Karte mit Buttons stehen
- [ ] `!q` → paginierte Queue (Prev/Next-Buttons); Footer-Dauersumme, fehlende Dauern als `≥ Summe (n ohne Angabe)`
- [ ] Skip-/Pause-/Resume-Buttons funktionieren; Pause bestätigt ephemer mit Position („Pausiert bei m:ss")
- [ ] `!eq <preset>` mid-song → aktueller Track startet mit neuem Filter neu
- [ ] `!next <Suche/URL>` → Track landet vorn in der Queue
- [ ] `!now <n>` → Queue-Eintrag an Position n rückt nach vorn + aktueller Song wird geskippt
- [ ] Fortschrittsbalken aktualisiert im **5-Sekunden-Takt** (21 Schritte, Endsprung ans Songende); kein 429-Fallback im Log
- [ ] Skip/`!stop` in den ersten 2 s eines Tracks → **keine** gelbe FFmpeg-stderr-Warnung im Terminal (nur echte Fehler warnen)

## Übergänge & Prefetch

- [ ] Während Song N läuft: Queue-Songs N+1/N+2 werden wirklich vorgeladen (`[Prefetch] Lade vor` nach spätestens ~15 s Startphase) → Übergang ohne hörbare Lücke
- [ ] `!shuffle`/`!move`/`!remove` mid-song → Prefetch zielt auf die **neue** Reihenfolge (Log zeigt neuen Titel), Übergang bleibt nahtlos
- [ ] `!x` (Pause) → `!resume` → Song zu Ende hören: Autoplay feuert am Songende, Balken finalisiert, Watchdog aktiv (das `_stopped_by_user`-Leck ist zu)

## Autoplay & Radio

- [ ] Autoplay-Button an, Queue leer laufen lassen → Nachfolger aus dem YouTube-Mix spielt automatisch (kein Titel aus `_recently_played`/`_recently_played_titles`)
- [ ] Autoplay über mehrere Songs: keine Cover-/Live-/Remix-/Sped-up-Variante eines kürzlich gespielten Songs, kein Genre-Bruch („kein Jazz nach Techno"), keine Schleifen
- [ ] `!q` bei aktivem Autoplay mit vorgeladenem Kandidaten → Vorschau-Zeile „🔮 Als Nächstes (Autoplay): …" statt nummeriertem Eintrag
- [ ] `!p` während Autoplay → vorgeladener Autoplay-Song wird evicted, neuer Song spielt als Nächstes
- [ ] `!radio <Nr>` → Sender spielt; `!stop` → Radio aus, Queue bleibt erhalten

## Persistenz & Neustart

- [ ] `!saveq <name>` / `!lists` / `!loadq <name>` → Roundtrip lädt die Queue unverändert
- [ ] `!loadq last` → lädt die Queue der letzten Session (`last_queue.json`); `!saveq last` wird abgelehnt (reserviert); Start bleibt bewusst mit leerer Queue
- [ ] `!debug on`/`!debug off` → Terminal wechselt zwischen voller Diagnose und „nur echte Probleme"; `bot.log` enthält in beiden Modi alles; `!debug` ohne Argument zeigt den Status
- [ ] Bot-Start mit veraltetem yt-dlp → eine WARNING-Zeile `[yt-dlp] Version … verfügbar` (auch im Quiet-Modus); kein Auto-Update
- [ ] `!loop`-Zyklus: `None` → `song` → `queue` → `None` (Anzeige + Verhalten am Trackende)
- [ ] Bot-Neustart → zuletzt gespielte Songs sofort wieder spielfähig (Metadaten-Cache greift, keine Neu-Extraktion nötig)
- [ ] Buttons einer Now-Playing-Nachricht von **vor** dem Neustart klicken → ephemere Erklärung statt „Interaktion fehlgeschlagen", tote Buttons werden entfernt (ADR 0006)
- [ ] `!restart` aus laufender `start.bat` → neues Konsolenfenster öffnet sich, altes endet am `pause`; `!score`-Zähler haben den Neustart überlebt (ADR 0003/0005)

## Progressiver Download & Registry (ADR 0001/0002)

- [ ] Ungecachter kurzer Track (≤ 20 min, webm-Modus) → progressiver Start: Puffer-Log erscheint (`_wait_for_buffer`), Wiedergabe beginnt nach dem Startpuffer aus der wachsenden Datei (kein CDN-Stream)
- [ ] Resume bei überholtem Download: endet die wachsende Datei vorzeitig, setzt die Wiedergabe ~1 s vor der Hörposition fort (`_seek_offset`), Play-Count zählt nicht doppelt
- [ ] Skip während eines progressiven Tracks → nächster Track spielt, der geskippte wird **nicht** wieder vorn eingereiht (`_stop_for_advance`)
- [ ] Kein Doppel-Download: während ein progressiver Download läuft, denselben Track erneut anstoßen (Loop/`!replay`/`!eq`-Restart) → es startet kein zweiter Download; die INFO-Zeile `[Download] Bereits in Arbeit über <pfad> – kein zweiter Start` bleibt im Normalbetrieb aus (erscheint sie, wurde ein Kollisionsversuch abgewehrt — Ursache prüfen)
- [ ] Langer Track (> 20 min) → streamt direkt (kein Download startet, `downloads/` wächst nicht)

## Startlatenz & Cookie-Modus (ADR 0010)

- [ ] `!p <Suchbegriff>` bei leerer Queue → Ton nach ~3 s. Im Log steht **kein** `Sleeping 5.00 seconds as required by the site` und der Puffer ist nach ~1 s erreicht (`[Progressiv] Puffer erreicht (… nach …s)`)
- [ ] Reihenfolge im Log: `[Progressiv] Starte Hintergrund-Download` erscheint **vor** `[Progressiv] Laufenden Download adoptiert` — der Frühstart per `prime_first_hit` greift und `resolve_track` adoptiert ihn (kein zweiter Download)
- [ ] Alterssperre: bekanntes altersbeschränktes Video per `!p <URL>` → einmal `[Cookies] YouTube verlangt eine angemeldete Session (…)` + `[Cookies] Zweitversuch mit Cookies`, danach spielt der Track
- [ ] Nach diesem Umschalten laufen Folge-Tracks weiter (im Cookie-Modus, also wieder mit Werbepause — erwartet); erst ein Bot-Neustart geht zurück auf cookielos
- [ ] `!reloadcookies` → Bestätigung nennt die Cookie-Quelle; danach steht der Cookie-Modus (nächster ungecachter Track wartet die Werbepause ab)
