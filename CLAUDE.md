# CLAUDE.md

Setup → `README.md`/`install.bat`, Cookies → `SETUP.md` (je `*_DE.md`). Tests `pytest` (dev deps
`requirements-dev.txt`), Lint `ruff check .` (nur E/F/W, Config `pyproject.toml`). Start `start.bat` bzw.
`python main.py`. **Zielplattform Windows nativ**, Linux/macOS bleibt lauffähig → [ADR 0003](docs/adr/0003-windows-native-zielplattform.md).

## Fehlersuche — zuerst das, nicht bot.log greppen

1. `curl http://127.0.0.1:8765/debug` — Live-Zustand (Voice-Client lebt? `poller_dead`, Gateway-Stille, Track, Queue).
2. `python tools/logsummary.py` — letzter Lauf als Zeitleiste; `bot.log.1` = vorheriger Lauf, `--list` alle.
3. Hänger: `py-spy dump --pid <pid>`. Details → [logging.md](docs/architecture/logging.md) → *Fehlersuche*.

## Invarianten — niemals brechen

- Queue = 2-Tupel `(url, title)`, `current_track` = 3-Tupel; beim Re-Add `url, title, *_ = self.current_track`.
- Kein nacktes `unlink`/`os.remove` in `cogs/` → `utils/files.safe_unlink` ([ADR 0003](docs/adr/0003-windows-native-zielplattform.md)).
- Pro URL ein schreibender Download; lebendig/tot entscheidet nur die `_inflight`-Registry (Einzel-Task, Shield,
  Deregistrierung nach Thread-Ende), wachsende Datei nie unlinken → [ADR 0002](docs/adr/0002-inflight-registry.md).
- Musik-`vc.stop()` nur via `_stop_for_advance()` ([ADR 0001](docs/adr/0001-progressiver-download-statt-cdn-stream.md));
  jeder Wiedergabe-Stopp über `utils/voice.stop_playback()` (erzwingt `tests/test_no_raw_vc_stop.py`).
- Verbindungsaufbau nur über `utils/voice.connect_voice()` (Klasse nur beim connect setzbar; liefert
  `SafeVoiceRecvClient`, fixt den voice_recv-Crash beim Channel-Verlassen) → [ADR 0011](docs/adr/0011-sprachsteuerung-ein-parser-zwei-eingaenge.md).
- Gesprochene Befehle nur durch `VoiceListen.handle_text()`; synthetische Message statt `ctx.invoke`, `ctx.author`
  echtes `Member`; DM-Bot im Channel → kein eigenes Sprachmodell → [ADR 0011](docs/adr/0011-sprachsteuerung-ein-parser-zwei-eingaenge.md).
- `after_playing`: Marker-Reset **nach** dem Progressive-Resume-Block → [playback.md](docs/architecture/playback.md).
- Track-Zustand komplett vor `vc.play()`, nach jedem Post-Play-await `_track_generation` prüfen → [ADR 0007](docs/adr/0007-track-zustand-vor-play-generationszaehler.md).
- Wachsende Dateien transkodieren, nie `codec=copy` → [ADR 0001](docs/adr/0001-progressiver-download-statt-cdn-stream.md).
- Now-Playing-Karte nur beim aktuellen Song; Altes via `_retire_np_message` zur Textzeile, nie leer editieren → [playback.md](docs/architecture/playback.md).
- `StaleControlsFallback`: Abgrenzung nur über den Template-Regex (BOOT_ID-Lookahead) → [ADR 0006](docs/adr/0006-stale-buttons-dynamicitem.md).
- Neue `t()`-Keys in **beiden** `locales/*.json` (erzwingt `tests/test_i18n_keys.py`).
- Logging: nie `logging.basicConfig()`; `logger` aus `utils.logger`; `bot.log` immer vollständig, Terminal filtert
  per `[Tag]` — neuer INFO-Tag in genau eine von `_ESSENTIAL_TAGS`/`_QUIET_TAGS` → [logging.md](docs/architecture/logging.md).
- Security-Flags defaulten auf Altverhalten, Owner immer durch → [ADR 0004](docs/adr/0004-security-flags-default-altverhalten.md).
- Kein blockierendes I/O im Event-Loop (auch GPU: `asyncio.to_thread`), wiederkehrende Writes gedebounct → [ADR 0005](docs/adr/0005-persistenz-debouncing.md).
- yt_dlp nicht thread-safe: Prefetch sequenziell, gesichert durch das Lock in `_prefetch_one` → [playback.md](docs/architecture/playback.md).
- yt_dlp cookielos zuerst; Extraktion nur über `dl.extract_info_async(query, kind)` → [ADR 0010](docs/adr/0010-cookielos-zuerst-cookie-fallback.md).

## Landkarte

Modul-Übersicht und Themen-Dateien → [`docs/architecture.md`](docs/architecture.md) (Index), Entscheidungen →
[`docs/adr/`](docs/adr/README.md), manuelle Tests → [`docs/smoke-checklist.md`](docs/smoke-checklist.md),
[`docs/sprachsteuerung-testen.md`](docs/sprachsteuerung-testen.md). Größte Dateien: `cogs/downloader.py`
(yt_dlp, Cache, Prefetch, In-Flight), `cogs/music.py` + Mixins `music_playback.py` / `music_voice_ui.py`
(Voice-Lifecycle, Reparatur, Watchdog) / `music_radio.py` / `music_stats.py` / `music_queue_io.py` → [ADR 0009](docs/adr/0009-music-cog-mixin-zerlegung.md).
`cogs/dm_bridge.py` = HTTP-Server für den DM-Bot (`/speak`, `/command`, `/health`, `/debug`) → [ADR 0008](docs/adr/0008-dm-bridge-blocking-speak-ohne-callback.md).
