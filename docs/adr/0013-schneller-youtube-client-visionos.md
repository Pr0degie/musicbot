# ADR 0013: Schneller YouTube-Client `visionos` statt TV-Client

- **Status:** Akzeptiert
- **Datum:** 2026-11-22

## Kontext

Neue Songs brauchten wieder ~9–10 s bis zum ersten Ton, obwohl der Bot seit [ADR 0010](0010-cookielos-zuerst-cookie-fallback.md)
cookielos startet. Log eines echten Laufs (`!p miau biti bong`, 22.11.2026 00:21):

| Abschnitt | Dauer |
|---|---|
| Suche | 1,5 s |
| Metadaten (`extract_info`) | 1,4 s |
| Progressiver Download bis Startpuffer (512 KB) | 6,5 s |
| **bis zum ersten Ton** | **9,5 s** |

Dasselbe Muster wie in ADR 0010, nur ohne Cookies: yt_dlp (2026.10.26) nimmt mit Node-JS-Runtime die Clients
`tv_samsung`, `visionos`, `web` und wählt die URL des TV-Clients (`c=TVHTML5`). Die trägt ein `available_at` ~4–5 s in
der Zukunft; yt_dlps Downloader schläft so lange („Sleeping 4.00 seconds as required by the site"), FFmpeg im Stream-Pfad
bekam vorher 403 (siehe Nachtrag in [ADR 0001](0001-progressiver-download-statt-cdn-stream.md)).

Messung je Client (22.11.2026, gleiche Videos):

| Client | Extraktion | Sperrfrist | sofort abrufbar | Format |
|---|---|---|---|---|
| Default (TV gewinnt) | 2,1 s | ~4,3 s | nein (403) | 251 Opus |
| `tv_samsung` | 1,9 s | ~4,3 s | nein (403) | 251 Opus |
| **`visionos`** | **1,3 s** | **keine** | **ja** | **251 Opus, gleiche Bitrate** |
| `web` | — | — | — | „Requested format is not available" |
| `visionos` + `tv_samsung` | 2,2 s | keine (visionos gewinnt) | ja | 251 Opus |

Abruf bis 512 KB Puffer, drei Videos: **alt 6,8–8,0 s** (ein Video scheiterte im TV-Pfad sogar mit 403), **neu 1,8–1,9 s**.

## Entscheidung

Im cookielosen Modus fragt der Bot YouTube nur mit `visionos` ab (`FAST_PLAYER_CLIENTS`, `Downloader._player_opts()`,
gesetzt in `_build_base_opts` und den URL-/Playlist-Instanzen; der progressive Download übernimmt das Info-Dict und damit
die visionos-URL). yt_dlp selbst nutzt ohne JS-Runtime ebenfalls nur `visionos` als Default.

Scheitert eine Extraktion mit einem Fehler, der auf den Client deutet (`CLIENT_FALLBACK_MARKERS`: „Requested format is not
available", „No video formats found", PO-Token, 403 …), wiederholt `extract_info_async()` sie **einmal** mit einer frischen
Instanz ohne Client-Vorgabe (`_build_ydl(kind, fast=False)`, also yt_dlp-Standard). Beide Clients gleichzeitig anzugeben
wäre robuster, kostet aber bei jedem Song ~0,9 s Extraktion zusätzlich.

Im Cookie-Modus bleibt es bei yt_dlps Defaults für angemeldete Sessions — `visionos` ist dort nicht vorgesehen.

## Konsequenzen

- Erster Ton bei neuen Songs: **~9,5 s → ~3,5 s** (Suche 1,5 s + Extraktion 1,3 s + Puffer 0,5 s).
- Lange Tracks (Stream-Pfad) laufen ohne die Wartezeit aus `stream_wait_seconds`; die bleibt als Sicherung für den Rückfall.
- Ändert YouTube/yt_dlp das Verhalten von `visionos`, greift der Rückfall automatisch (Log: `[yt-dlp] visionos scheiterte …`).
  Taucht die Zeile häufig auf, `FAST_PLAYER_CLIENTS` neu messen (Messskript-Vorgehen siehe Kontext).
- Prefetch-/Autoplay-Downloads (`self.ydl.extract_info(download=True)`) nutzen visionos, haben aber keinen eigenen Rückfall —
  sie fallen bei Fehlern wie bisher auf den normalen Auflösepfad zurück.
- Tests: `tests/test_fast_player_client.py`.
