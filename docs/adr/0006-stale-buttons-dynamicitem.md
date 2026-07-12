# ADR 0006: Stale-Buttons-Fallback via DynamicItem

- **Status:** Akzeptiert
- **Datum:** 2026-07-12 (Commit `ff282f1`)

## Kontext

Nach einem Bot-Neustart sind die Views früherer Läufe nicht mehr registriert — Klicks auf Buttons alter Now-Playing-Nachrichten enden in Discords „Interaktion fehlgeschlagen" ohne Erklärung.

## Entscheidung

Buttons tragen `custom_id = musicctl:{BOOT_ID}:{nonce}:{action}` (BOOT_ID = pro Prozessstart, nonce = pro Nachricht). `StaleControlsFallback` (DynamicItem, registriert in `setup_hook`) beantwortet Klicks auf Now-Playing-Nachrichten aus früheren Läufen mit ephemerer Erklärung + entfernt die toten Buttons.

## Konsequenzen

- **Wichtig:** discord.py dispatcht Dynamic Items **zusätzlich** zu Live-Views (nicht nur als Fallback) — die Abgrenzung kommt allein aus dem Template-Regex (negative lookahead auf die aktuelle BOOT_ID), nie die Reihenfolge ändern.
- Nachrichten von vor diesem Feature (32-Hex-Auto-IDs) matchen nichts → weiterhin „Interaktion fehlgeschlagen", einmalig bis zum ersten Neustart danach.
