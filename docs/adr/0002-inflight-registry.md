# ADR 0002: In-Flight-Registry — pro URL maximal ein schreibender Download

- **Status:** Akzeptiert
- **Datum:** 2026-07-12 (Commits `0966bb6`, `2852500`, `5d35880`, `a89884d`, `400d83b`)

## Kontext

Vier Pfade schreiben Bytes für eine URL auf die Platte: Queue-Prefetch, Autoplay-Prefetch, progressiver Download, Download-Fallback (`force_download`). Ohne zentrale Buchführung kollidierten sie auf drei Wegen (je ein Commit pro Weg):

1. `resolve_track` beurteilte per Datei-Heuristik, ob eine unvollständige Datei eine Leiche ist — und konnte so eine noch wachsende progressive Datei unlinken/neu laden (`2852500`).
2. Der Download-Fallback (`force_download`) lud parallel zu einem bereits laufenden Download derselben URL (`5d35880`).
3. Ein zweiter `_start_progressive`-Aufruf für dieselbe URL startete einen zweiten Download (`a89884d`).

## Entscheidung

**Invariante: pro URL maximal ein schreibender Download, Quelle: `_inflight`** (url → `(Task, Quelle)`). Jeder Pfad, der Bytes für eine URL auf die Platte schreibt, registriert sich via `_register_inflight` beim Start und deregistriert im `finally` (`_inflight_done`; done-Callback als Sicherheitsnetz).

## Konsequenzen

- `resolve_track` konsultiert die Registry als Erstes: lebender progressiver Task wird **adoptiert** (Puffer-Wait auf dem bestehenden Task; puffert er zu langsam → Stream-Fallback, **nie** unlink/Neustart der wachsenden Datei), lebende Prefetch-Tasks (Queue oder Autoplay) werden awaitet (danach normaler Datei-Check), `force_download` awaitet einen lebenden Task statt parallel zu laden.
- Die Leichen-Behandlung (unlink + Neuladen einer `is_incomplete`-Datei) greift nur, wenn die Registry **keinen** lebenden Task kennt — lebendig/Leiche entscheidet allein die Registry, nie eine Datei-Heuristik.
- `_start_progressive` ist idempotent (zweiter Aufruf für dieselbe URL → derselbe Task).
- **Cancel-Pfad (2026-08):** Der Queue-Prefetch registriert pro URL einen **eigenen** Download-Task (`_prefetch_one`), nicht den Sammel-Task des Aufrufers. Cancelt der Aufrufer (`play_next`, `!clear`, Queue-Umbau) seinen Task, läuft der Worker-Thread von `yt_dlp.download` trotzdem weiter — der Download-Task wird **adoptiert** (Shield), läuft zu Ende und deregistriert sich erst dann. Vorher löste der Cancel das `finally` des Aufrufers aus: Registry meldete „kein Writer", während der Thread weiterschrieb → genau die Kollision, die diese ADR ausschließt. Sequenzialität der Prefetch-Downloads (yt_dlp ist nicht thread-safe) sichert seither ein Lock um den Download-Aufruf, nicht die Task-Reihenfolge.
- `resolve_track` wartet damit nur auf den Download der konkret gebrauchten URL, nie auf den Sammel-Task (der danach noch weitere Songs lädt).
- Kollisionsversuche loggen dauerhaft INFO `[Download] Bereits in Arbeit über <pfad> – kein zweiter Start`.
- Tests: `tests/test_inflight_registry.py`.
