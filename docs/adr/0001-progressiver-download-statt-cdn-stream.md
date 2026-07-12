# ADR 0001: Progressiver Download statt CDN-Stream für ungecachte Tracks

- **Status:** Akzeptiert
- **Datum:** 2026-07-12 (Commits `8c15622`, `afc7560`; Fallback-Kaskade davor: `08601d4` / `eb8c583`, 2026-07-11/12)

## Kontext

YouTube-CDN lehnt FFmpegs HTTP-Client oft mit 403 ab (auch mit frischer URL + `http_headers`) — yt_dlps eigener Client nie. Die zuvor gebaute Stream-Retry-Kaskade (ein Retry mit frischer URL, danach ein letzter Anlauf als lokaler Download — Details in `docs/architecture.md` → *Race-Schutz & Stream-Retry*) ist dadurch heute nur noch Fallback.

## Entscheidung

Primärpfad für ungecachte Tracks ≤ 20 min ist der **progressive Download**: `resolve_track()` startet sofort einen Hintergrund-Download (`_start_progressive` → Wegwerf-`YoutubeDL` mit `nopart` — das `.part`-Rename würde auf Windows an der offenen FFmpeg-Datei scheitern) und gibt die **wachsende Datei** als `Path` zurück, sobald der Startpuffer steht.

Gates: kein mp3-Modus, `ext` in webm/opus, URL nicht geblockt, kein `force_download`.

## Konsequenzen

- **Puffer:** `_wait_for_buffer`: 256 KB ≈ 13 s Opus + `min_buffer_seconds` fürs `-ss`-Ziel; Timeout 15 s → Stream-Fallback + TTL-Block 10 min.
- **Buchhaltung:** `_incomplete_files` (Set) + `.inprogress`-Sidecar (Crash-Marker; `_purge_stale_progressive` räumt beim Start auf); `is_incomplete()`/`progressive_task_for()` sind die Abfrage-API für `music.py`. Loop/`!replay`/`!eq`-Restart nehmen am laufenden Task teil statt die halbe Datei als Cache zu missdeuten. `prefetch_next` überspringt progressiv laufende URLs und wartet via `wait_progressive_idle()` (Bandbreite); `cleanup_downloads` schützt `_progressive_files`.
- **Wiedergabe:** Wachsende Dateien (`is_growing` via `dl.is_incomplete`) werden **immer transkodiert** (nie `codec=copy` — Cues/Duration stehen erst am Dateiende), ohne reconnect/headers.
- **Resume:** Endet die wachsende Datei vorzeitig (FFmpeg überholt den Download), reiht `after_playing` den Track mit `_seek_offset` = Hörposition−1 s wieder vorn ein (`_progressive_resume_url`, kein doppelter Play-Count, Kappe `PROGRESSIVE_MAX_RESUMES = 2`); ist der Download tot oder die Kappe erreicht → `block_progressive()` + Requeue in die Stream-/Retry-Kaskade.
- **Absichtliche Stopps** (Skip, `!now`, `!eq`, `!seek`, `!stop`, `!clear`, Auto-Leave, Skip-Button, Musik-weicht-Radio) laufen über `_stop_for_advance()` — One-Shot-Flag `_suppress_resume`, konsumiert in `after_playing` —, sonst würde jeder Skip die wachsende Datei des aktuellen Tracks wieder vorn einreihen. Neue `vc.stop()`-Aufrufe auf Musik-Quellen daher immer über diesen Helper (Radio-Quellen nicht: deren Callback `after_radio` konsumiert das Flag nicht).
- **Blockliste:** Fehlgeschlagene Downloads löschen die Teil-Datei via `safe_unlink` (bei Sharing-Violation bleiben Marker + Sidecar liegen, die URL wird sofort via `block_progressive` gesperrt → Neustart räumt auf).
- **Schnellstart:** Hat das Info-Dict `formats` (frisch extrahiert, nicht der reduzierte Disk-Cache), lädt `_download_progressive_sync` direkt via `process_ie_result` (wie `--load-info-json`) — spart die Zweit-Extraktion samt JS-Challenge (~5–10 s vor dem ersten Byte); scheitert das (abgelaufene URL), Teil-Datei weg + einmal frische Extraktion. Geht die Teil-Datei dabei nicht weg (Windows: FFmpeg liest sie noch), wird der progressive Versuch abgebrochen statt anzuhängen — yt_dlps `continuedl` würde sonst einen korrupten Mischling erzeugen; die URL landet auf der Blockliste, der Stream-Fallback übernimmt.
