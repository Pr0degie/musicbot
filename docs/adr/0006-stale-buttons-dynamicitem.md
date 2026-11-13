# ADR 0006: Stale-Buttons-Fallback via DynamicItem

- **Status:** Akzeptiert
- **Datum:** 2026-07-12 (Commit `ff282f1`)

## Kontext

Nach einem Bot-Neustart sind die Views früherer Läufe nicht mehr registriert — Klicks auf Buttons alter Now-Playing-Nachrichten enden in Discords „Interaktion fehlgeschlagen" ohne Erklärung.

## Entscheidung

Buttons tragen `custom_id = musicctl:{BOOT_ID}:{nonce}:{action}` (BOOT_ID = pro Prozessstart, nonce = pro Nachricht). `StaleControlsFallback` (DynamicItem, registriert in `setup_hook`) beantwortet Klicks auf Now-Playing-Nachrichten aus früheren Läufen mit ephemerer Erklärung + entfernt die toten Buttons.

## Konsequenzen

- **Wichtig:** discord.py dispatcht Dynamic Items **zusätzlich** zu Live-Views (nicht nur als Fallback) — die Abgrenzung kommt allein aus dem Template-Regex (negative lookahead auf die aktuelle BOOT_ID), nie die Reihenfolge ändern.
- **Stand 2026-11-13:** Die Karte ist seit [ADR 0012](0012-now-playing-karte-components-v2.md) ein Components-V2-Layout. „Tote Buttons entfernen" heißt dort: Karte per `retired_card()` zur Textzeile zurückbauen (Titel aus der Titelzeile mit `NP_HEADING_ID`) — `view=None` ließe eine leere Nachricht zurück (400). Embed-Karten aus früheren Läufen weiterhin per `view=None`; die Unterscheidung trifft `message.flags.components_v2`.
- Nachrichten von vor diesem Feature (32-Hex-Auto-IDs) matchen nichts → weiterhin „Interaktion fehlgeschlagen", einmalig bis zum ersten Neustart danach.
