# ADR 0012: Now-Playing-Karte als Components-V2-Layout

- **Status:** Akzeptiert
- **Datum:** 2026-11-13 (Commits `bd53ffa`, `5d49a9d`)

## Kontext

Die Now-Playing-Karte war ein Embed mit angehängter Button-Zeile. Gewünscht war ein Clear-Button **oben rechts in der Karte**. Ein Embed kann keine Buttons tragen — sie stehen immer darunter und linksbündig, und die eine Zeile war mit fünf Buttons schon voll.

## Entscheidung

Die Karte ist ein Components-V2-Layout (`MusicControlView` als `LayoutView`, `views/music_controls.py`): ein Container in Akzentfarbe mit

- Section `[Titel als Markdown-Link aufs Video, Fortschrittsbalken]` → Accessory: grauer Button mit rotem ❌ (= `!clear`),
- bei YouTube: MediaGallery mit dem Video-Thumbnail in 320×180 (`mqdefault`, `compact_card_image()`). Das Thumbnail-Accessory
  hat bei Discord eine feste, kleine Größe; ein einzelnes Galeriebild lässt sich nicht skalieren, die kleine Bildvariante hält die Karte kompakt,
- TextDisplay `[Details, „Als Nächstes", Uploader]` (ohne YouTube-Bild: Section mit kleinem Thumbnail-Accessory; sonst bei Songs
  Section mit grauem 🔗-Accessory, das den Song-Link zum Kopieren liefert),
- ActionRow `[Zurück, Pause/Fortsetzen, Skip, Autoplay, Loop]`.

Der Link-Text der Titelzeile darf **keine Emojis** enthalten: dann stellt Discord den Link nicht dar, sondern zeigt
`[Titel](<url>)` als Rohtext (live getestet 22.11.2026 — derselbe Titel ohne Emojis wurde Link, mit Emojis auch kurz nicht;
Länge und `<>` um die URL spielen keine Rolle). `utils/text.link_label()` entfernt deshalb Emojis, ersetzt Klammern und
vermeidet vorsorglich Backslash-Escapes; bleibt nichts übrig, steht der Titel ohne Link da.

Gilt für Song- und Radio-Karte. Alle Zustandsänderungen der Karte laufen über die View selbst: `set_body()` für den Balken, `sync_state()` für Button-Farben/-Beschriftung und „Als Nächstes" (Zurück fest rot, Pause blau ↔ Fortsetzen grün, Autoplay rot = an, Loop grün = an). Weil der Zustand an den Buttons ablesbar ist, quittieren die Umschalter stumm: `_push_state` nutzt den Karten-Edit als Interaktions-Antwort, sonst `defer()`.

## Konsequenzen

- **Nie `content=`/`embed=` an eine Karten-Nachricht:** Nachrichten mit V2-Flag dürfen beides nicht tragen, und das Flag lässt sich nicht mehr entfernen. Der Rückbau zur Textzeile geht deshalb über `retired_card(title)` (Layout mit genau einer Textzeile), nie über `view=None` — eine V2-Nachricht ohne Komponenten ist leer und wird mit 400 abgelehnt.
- **Live-Edits** schicken dieselbe View erneut (`msg.edit(view=view)`); der `_progress_loop` editiert, wenn sich Balken **oder** Zustand geändert hat.
- **`_push_state` nur auf die aktive Karte:** Ein Klick, der einen neuen Song startet, hat die eigene Nachricht schon zurückgebaut — ein Edit mit der View würde die alte Karte samt Buttons wiederbeleben. Abgrenzung über `now_playing_msg.id`.
- **Thumbnail nur mit http(s)-URL:** Ein Embed ignoriert eine unbrauchbare Bild-URL, das Layout lehnt die ganze Nachricht ab.
- **Stale-Fallback** ([ADR 0006](0006-stale-buttons-dynamicitem.md)): alte V2-Karten werden über die Titelzeile (`NP_HEADING_ID`) zur Textzeile zurückgebaut; Embed-Karten aus der Zeit davor weiterhin per `view=None`.
- **Buttons ohne Decorator:** Die Buttons entstehen in `__init__` (`make()`), weil sie in Section und ActionRow verschachtelt sind. Dort sitzt auch zentral die `REQUIRE_SAME_VOICE`-Prüfung für jeden Button ([ADR 0004](0004-security-flags-default-altverhalten.md)).
- **`!clear`/❌ teilen sich `_clear_all`**, das einen Snapshot für den Rückgängig-Button liefert (`UndoClearView` → `_restore_cleared`, 30 s).
- **🔗 kopiert nicht selbst** (Stand 22.11.2026): Ein Bot kann nicht in die Zwischenablage schreiben. Der Button antwortet ephemer mit dem Link als Codeblock (Desktop: Kopier-Symbol), Backticks kodiert. Er nimmt denselben Link wie der Kartentitel (`webpage_url`, sonst die Queue-URL) und fehlt ohne http(s)-URL sowie auf der Radio-Karte. Platz: Accessory der Details — die Button-Zeile ist mit fünf voll; belegt das kleine Thumbnail das Accessory, bekommt er eine eigene ActionRow darunter.
- **Rückbau sofort nach `!clear`/❌, Karte bleibt beim Queue-Ende** (Stand 28.11.2026): `!clear`/❌ bauen die aktive Karte sofort zur Textzeile zurück (`_retire_active_card`). Beim natürlichen Queue-Ende bleibt die letzte Karte samt Buttons stehen (`_ended_np`, Balken auf 100 %), bis der nächste Song startet — der Versuch vom 27.11., sie auch dort sofort zurückzubauen, wurde auf Nutzerwunsch zurückgenommen.
- **Übersprungene Karte wird gelöscht** (Stand 28.11.2026): `!s` und ⏭ merken die laufende Karte in `_skipped_np_msg`; `_retire_np_message` löscht genau diese Nachricht beim Wechsel, statt eine Textzeile `🎶 Titel` zu hinterlassen (gescheitertes Löschen → doch Textzeile). Ist die Queue nach dem Skip leer, gilt die Queue-Ende-Regel: Die Karte bleibt stehen und verschwindet erst mit dem nächsten Song. Natürliche Wechsel, ⏮ und Radio-Übernahme hinterlassen weiter die Textzeile. Der Skip selbst quittiert stumm (kein „Übersprungen“ im Chat, der Button antwortet per `defer()`) – die neue Karte ist Rückmeldung genug.
- **⭐ unter dem 🔗** (Stand 27.11.2026): Favoriten sind eine gemeinsame Liste des Servers (`FavoritesMixin`). Der ⭐ sitzt auf Nutzerwunsch direkt unter dem 🔗: eine eigene Section, deren Text die Uploader-Zeile ist (sie wandert dafür aus den Details; ohne Uploader ein Hinweis auf `!favs`). Belegt das kleine Vorschaubild den Accessory-Platz, bekommt der ⭐ eine eigene Zeile unter der 🔗-Zeile. Kein Favorit = leerer Umriss-Stern ☆ auf Grau, Favorit = gelber ⭐ auf Grün (Stand 28.11.2026, vorher nur die Farbe – auf Nutzerwunsch; ☆ ist kein Emoji und steht deshalb als Beschriftung statt `emoji=`), stumm wie Autoplay/Loop; fehlt auf der Radio-Karte.
- Setzt discord.py ≥ 2.6 voraus (`LayoutView`).
