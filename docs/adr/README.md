# Architecture Decision Records

Rückwirkend angelegt am 2026-07-12 (Datierung aus dem git log der zugehörigen Commits). Format: Status / Kontext / Entscheidung / Konsequenzen.

- [0001](0001-progressiver-download-statt-cdn-stream.md) — Progressiver Download statt CDN-Stream für ungecachte Tracks
- [0002](0002-inflight-registry.md) — In-Flight-Registry: pro URL maximal ein schreibender Download
- [0003](0003-windows-native-zielplattform.md) — Windows-native Zielplattform
- [0004](0004-security-flags-default-altverhalten.md) — Security-Flags mit Default = Altverhalten + Owner-Garantie
- [0005](0005-persistenz-debouncing.md) — Persistenz-Debouncing für Dauerbetrieb auf schwacher Hardware
- [0006](0006-stale-buttons-dynamicitem.md) — Stale-Buttons-Fallback via DynamicItem
- [0007](0007-track-zustand-vor-play-generationszaehler.md) — Track-Zustand vor `vc.play()` + Generationszähler gegen Races
- [0008](0008-dm-bridge-blocking-speak-ohne-callback.md) — DM-Bridge: blockierendes `/speak` ohne Rück-Callback
- [0009](0009-music-cog-mixin-zerlegung.md) — Music-Cog-Mixin-Zerlegung + `play_next`-Orchestrator
- [0010](0010-cookielos-zuerst-cookie-fallback.md) — Cookielos zuerst, Cookies nur als Fallback (Startlatenz)
- [0011](0011-sprachsteuerung-ein-parser-zwei-eingaenge.md) — Sprachsteuerung: ein Parser, zwei Eingänge (kein LLM)
