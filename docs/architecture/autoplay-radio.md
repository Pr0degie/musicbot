# Autoplay & Radio

[← Architektur-Übersicht](../architecture.md)

## Autoplay

Toggled via `🔁 Autoplay` button. `_prefetch_autoplay` starts at song-begin (only if queue empty): fetches
YouTube Mix (`list=RD{video_id}`) via `autoplay_ydl` (max 10 entries), wählt via `choose_autoplay_candidate()`
**gewichtet vorne** (Gewichte 4/3/2/1 über die ersten vier gefilterten Kandidaten — YouTubes Mix-Ranking ist
die Genre-Kohärenz, Platz 1 ist bewusst wieder erlaubt), downloads chosen candidate, appends to queue. On song
end `play_next` waits up to 60 s for the prefetch task; falls back to `autoplay()` (same lookup, no
pre-download) if needed. Der aufgelöste Kandidat erscheint in `!q` als Vorschau-Zeile „🔮 Als Nächstes
(Autoplay)" statt als nummerierter Eintrag.

Reference track: `current_track` → `last_played`. Autoplay stays on until button pressed again — not one-shot.

Die Kandidatenauswahl (nur Videos, `is_seen`-Filter, Fallback-Kaskade, gewichtete Wahl) lebt einmal als
Modul-Funktionen in `downloader.py` (`entry_url`, `is_video`, `is_seen`, `select_autoplay_candidates`,
`choose_autoplay_candidate`) und wird von beiden Pfaden genutzt — `MusicCommands.autoplay()` (Sofort-Pfad) und
`Downloader.prefetch_autoplay()` (Hintergrund-Pfad). Kaskadenstufe 2 (alles gesehen) schließt neben der
Video-ID des Referenz-Tracks auch dessen **Varianten** aus — sonst hebelte die Kaskade den Varianten-Filter
wieder aus.

`_autoplay_queued_url`: URL last added by autoplay; cleared when popped by `play_next` or evicted by
`_evict_autoplay_song()`.

`_recently_played`: `deque(maxlen=15)` of URLs. `_recently_played_titles`: `deque(maxlen=15)` of normalized
titles via `normalize_title()` (defined in `utils/text.py`, dependency-free/unit-tested, re-exported from
`downloader.py`): bevorzugt das `" | "`-Segment mit `" - "` (Artist-Trenner), sonst den **ganzen** Titel;
strippt Klammer-Suffixe wie "(Official Video)" und `feat.…`, Sonderzeichen → Trenner, lowercase, Wörter
alphabetisch sortiert — "AHA Take on Me" und "Take on Me AHA" ergeben denselben Key. Der Duplikat-Check
(`is_seen`) matcht per **bidirektionalem Overlap** `|A∩B|/min(|A|,|B|) ≥ 0,6` gegen die Historie; zusätzlich
gelten Kandidaten mit Varianten-Schlagwort im Roh-Titel (`cover|live|remix|sped
up|nightcore|slowed|reverb|acoustic|instrumental|karaoke|8d`, auch in Klammern →
`has_variant_keyword`/`title_core_words`) als Duplikat, wenn ihre Kern-Wortmenge (inkl. Klammer-Inhalt, dort
steht der Original-Künstler) mit einem Historien-Titel überlappt — „Africa (Toto Cover) - Alex Melton" blockt
nach „Toto - Africa". Fallback-Kaskade wie oben.

**`!p` with autoplay active** — `_evict_autoplay_song()` cancels prefetch, removes autoplay URL from queue,
inserts new song at front (`appendleft`). Playlist additions evict but append at end.

## Radio

Radio code lives in `cogs/music_radio.py` (`RadioMixin`). `RADIO_STATIONS_FILE` = `radio_stations.json` (key →
`{name, url}`).
State: `is_radio`, `radio_station_name`, `radio_stream_url`, `_radio_reconnect_count`.
`_play_radio_stream()` plays via FFmpeg directly (no yt_dlp); `after_radio` reconnects up to 3× on error.
`!radio <Nr|Name>` → aus Liste. `!radio <url> [Name]` → spielt + speichert automatisch (kein Duplikat).
`!stop` beendet Radio oder aktuelle Wiedergabe (Queue bleibt erhalten). Radio-Modus und Song-Modus schließen
sich gegenseitig aus.
