# ADR 0012: Now-Playing-Karte als Components-V2-Layout

- **Status:** Akzeptiert
- **Datum:** 2026-11-13 (Commits `bd53ffa`, `5d49a9d`)

## Kontext

Die Now-Playing-Karte war ein Embed mit angehängter Button-Zeile. Gewünscht war ein Clear-Button **oben rechts in der Karte**. Ein Embed kann keine Buttons tragen — sie stehen immer darunter und linksbündig, und die eine Zeile war mit fünf Buttons schon voll.

## Entscheidung

Die Karte ist ein Components-V2-Layout (`MusicControlView` als `LayoutView`, `views/music_controls.py`): ein Container in Akzentfarbe mit

- Section `[Titel, Fortschrittsbalken]` → Accessory: grauer Button mit rotem ❌ (= `!clear`),
- Section `[Details, „Als Nächstes", Uploader]` → Accessory: Thumbnail,
- ActionRow `[Zurück, Pause/Fortsetzen, Skip, Autoplay, Loop]`.

Gilt für Song- und Radio-Karte. Alle Zustandsänderungen der Karte laufen über die View selbst: `set_body()` für den Balken, `sync_state()` für Button-Farben/-Beschriftung und „Als Nächstes".

## Konsequenzen

- **Nie `content=`/`embed=` an eine Karten-Nachricht:** Nachrichten mit V2-Flag dürfen beides nicht tragen, und das Flag lässt sich nicht mehr entfernen. Der Rückbau zur Textzeile geht deshalb über `retired_card(title)` (Layout mit genau einer Textzeile), nie über `view=None` — eine V2-Nachricht ohne Komponenten ist leer und wird mit 400 abgelehnt.
- **Live-Edits** schicken dieselbe View erneut (`msg.edit(view=view)`); der `_progress_loop` editiert, wenn sich Balken **oder** Zustand geändert hat.
- **`_push_state` nur auf die aktive Karte:** Ein Klick, der einen neuen Song startet, hat die eigene Nachricht schon zurückgebaut — ein Edit mit der View würde die alte Karte samt Buttons wiederbeleben. Abgrenzung über `now_playing_msg.id`.
- **Thumbnail nur mit http(s)-URL:** Ein Embed ignoriert eine unbrauchbare Bild-URL, das Layout lehnt die ganze Nachricht ab.
- **Stale-Fallback** ([ADR 0006](0006-stale-buttons-dynamicitem.md)): alte V2-Karten werden über die Titelzeile (`NP_HEADING_ID`) zur Textzeile zurückgebaut; Embed-Karten aus der Zeit davor weiterhin per `view=None`.
- **Buttons ohne Decorator:** Die Buttons entstehen in `__init__` (`make()`), weil sie in Section und ActionRow verschachtelt sind. Dort sitzt auch zentral die `REQUIRE_SAME_VOICE`-Prüfung für jeden Button ([ADR 0004](0004-security-flags-default-altverhalten.md)).
- **`!clear`/❌ teilen sich `_clear_all`**, das einen Snapshot für den Rückgängig-Button liefert (`UndoClearView` → `_restore_cleared`, 30 s).
- Setzt discord.py ≥ 2.6 voraus (`LayoutView`).
