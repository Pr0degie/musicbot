# Smoke-Checkliste (manuell, End-to-End)

Nach jedem substanziellen Prompt/Refactoring manuell durchgehen — die Tests decken Playback/Voice nicht ab. Headless-Variante: A/B-Worktree-Diff (`.env` in den Worktree mitkopieren!).

## Grundfunktionen

- [ ] `!j` → Bot joint den Voice-Channel; `!l` → verlässt ihn
- [ ] `!p <Suchbegriff>` → erster Treffer spielt sofort, Alternativen erscheinen als Buttons (`SearchAutoplayView`, 30 s Timeout)
- [ ] Klick auf einen Alternativen-Button → Alternative spielt
- [ ] `!p <URL>` → Track wird eingereiht; erneutes `!p` derselben URL → Duplikat-Warnung
- [ ] `!p <Playlist-URL>` → mehrere Einträge landen in der Queue
- [ ] `!q` → paginierte Queue (Prev/Next-Buttons); Footer-Dauersumme, fehlende Dauern als `≥ Summe (n ohne Angabe)`
- [ ] Skip-/Pause-/Resume-Buttons funktionieren; Pause bestätigt ephemer mit Position („Pausiert bei m:ss")
- [ ] `!eq <preset>` mid-song → aktueller Track startet mit neuem Filter neu
- [ ] `!next <Suche/URL>` → Track landet vorn in der Queue
- [ ] `!now <n>` → Queue-Eintrag an Position n rückt nach vorn + aktueller Song wird geskippt

## Autoplay & Radio

- [ ] Autoplay-Button an, Queue leer laufen lassen → Nachfolger aus dem YouTube-Mix spielt automatisch (kein Titel aus `_recently_played`/`_recently_played_titles`)
- [ ] `!p` während Autoplay → vorgeladener Autoplay-Song wird evicted, neuer Song spielt als Nächstes
- [ ] `!radio <Nr>` → Sender spielt; `!stop` → Radio aus, Queue bleibt erhalten

## Persistenz & Neustart

- [ ] `!saveq <name>` / `!lists` / `!loadq <name>` → Roundtrip lädt die Queue unverändert
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
