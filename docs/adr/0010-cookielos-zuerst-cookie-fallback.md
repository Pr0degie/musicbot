# ADR 0010: Cookielos zuerst, Cookies nur als Fallback

- **Status:** Akzeptiert
- **Datum:** 2026-07-25

## Kontext

Der erste Song nach `!p <suchbegriff>` startete reproduzierbar erst nach ~13 s. Messung am Log eines echten Laufs (`!p smooth operator`, 2026-07-25 19:13):

| Abschnitt | Dauer |
|---|---|
| `!p` → Auto-Join | 0,5 s |
| Suche + Status-/Alternativ-Nachrichten | 3,7 s |
| Warten auf `extract_info` (Prefetch-Task) | 2,1 s |
| Progressiver Download bis Startpuffer | 6,7 s |
| **bis zum ersten Ton** | **13,0 s** |

Der Download selbst war 0,84 s nach Erreichen des Puffers komplett fertig — die 6,7 s waren also keine Bandbreite. Ein zeitgestempeltes yt_dlp-Verbose-Log zeigte die Ursache:

```
3.19s [download] Invoking http downloader on "https://rr1---sn-...googlevideo.com/..."
3.19s [download] Sleeping 5.00 seconds as required by the site...
8.47s [download] Destination: ...Smooth Operator.webm
```

Dahinter steckt die eingeloggte YouTube-Session aus `YDL_BROWSER=firefox` (Default): Der Extractor erkennt eine Pre-Roll-Werbung (`Detected a 6s ad skippable after 5s for tv_downgraded`) und schreibt deren Skip-Zeitpunkt als absoluten Zeitstempel `format["available_at"]` an **alle** Audio-Formate. `yt_dlp/downloader/common.py` wartet vor dem ersten Byte bis dahin. Die Wartezeit ist nicht umgehbar: entfernt man `available_at` aus dem Info-Dict und lädt sofort, antwortet das CDN mit **HTTP 403**.

Werbefrei sind nur die HLS-Video-Formate (91–94) — für einen Audio-Bot unbrauchbar. Ein Vergleich mehrerer `player_client`-Werte brachte nichts: ohne PO-Token liefern `web`/`mweb` nur 360p-mp4, `web_safari` nur HLS, `tv_simply` gar nichts. Nur der Default `tv_downgraded` liefert Format 251 (Opus) — und zahlt die Werbepause.

Entscheidend war die Messung **ohne** Cookies (drei Suchbegriffe, jeweils identisches Format und identische Bitrate):

| | Format | Wartezeit | Extraktion | bis zum Ton |
|---|---|---|---|---|
| mit Firefox-Cookies | 251 @ 143 kbps | 5–6 s | 3,8–5,1 s | 11,7–12,1 s |
| ohne Cookies | 251 @ 143 kbps | **0 s** | **1,9–3,0 s** | **3,0–3,3 s** |

Die anonyme Session bekommt keine Werbung und spart zusätzlich die Extra-Roundtrips der authentifizierten Clients. Die Audioqualität ist identisch, die Dateidauer stimmt exakt mit der Songlänge (kein Werbe-Inhalt in der Datei).

Cookies sind trotzdem nicht wertlos: Alterssperre, Bot-Check („Sign in to confirm you're not a bot"), Mitglieder- und private Videos brauchen sie. `SETUP.md` behauptete bislang, Cookie-Auth sei generell Pflicht — das gilt nur für diese Fälle bzw. für auffällige IPs.

## Entscheidung

**Cookielos ist der Normalbetrieb; Cookies werden erst zugeschaltet, wenn YouTube sie verlangt.** Der Downloader hält dazu ein Modus-Flag `_cookie_mode` (Start: `False`); `_cookie_opts()` liefert leere Optionen, solange es aus ist.

Umgeschaltet wird über `enable_cookie_mode(grund)` — einmalig und dauerhaft für den Rest des Prozesses:

- **Automatisch** bei einem Fehler, dessen Meldung auf fehlende Authentifizierung deutet (`COOKIE_ERROR_MARKERS` → `_needs_cookies()`). Ausgelöst in `extract_info_async()` (dann **ein** Zweitversuch derselben Abfrage) und im Fehlerpfad von `_run_progressive`.
- **Manuell** durch `!reloadcookies` — expliziter Wunsch schlägt den cookielosen Modus.

Alle Extraktionen laufen über `Downloader.extract_info_async(query, kind)`; `kind` ("main"/"search"/"url"/"playlist"/"autoplay") wird erst im Aufruf zur Instanz aufgelöst, damit nach dem Rebuild nie eine veraltete Instanz benutzt wird. `_extract_info_or_report` im Cog nimmt deshalb den `kind`-String statt einer yt_dlp-Instanz.

Zusätzlich verschwindet die Serialisierung im Suchpfad: Startet gerade nichts, stößt `prime_first_hit(url, title)` den progressiven Download des ersten Treffers **sofort** an — er läuft damit parallel zu den Discord-Nachrichten statt hinter ihnen. `resolve_track` adoptiert den laufenden Task später über die In-Flight-Registry ([ADR 0002](0002-inflight-registry.md)); die Gates teilen sich beide Pfade über `_may_start_progressive()`.

## Konsequenzen

- Erster Ton nach `!p <suchbegriff>`: **~13 s → ~3 s** (Integrationslauf gegen den echten Downloader: 3,25 s bzw. 2,62 s inkl. 0,6 s simulierter Discord-Latenz).
- Beim Umschalten in den Cookie-Modus wird der Metadaten-Cache geleert: cookielos aufgelöste Einträge stammen aus einer anderen Session, der Zweitversuch soll frisch auflösen.
- Der Zweitversuch kostet die Zeit des ersten Versuchs zusätzlich — nur für gesperrte Videos, und nur beim ersten Vorkommen pro Prozess.
- Ohne konfigurierte Cookie-Quelle (`YDL_COOKIES_FILE`/`YDL_BROWSER` leer) schlägt ein Auth-Fehler unverändert bis zum Nutzer durch.
- Timeouts werden **nicht** wiederholt — sie deuten nicht auf fehlende Authentifizierung, und der Aufrufer meldet sie selbst.
- Bandbreite: `prime_first_hit` lädt nur, wenn gerade nichts läuft. Wählt der Nutzer danach eine Alternative (Button B/C), war der Download des ersten Treffers umsonst — die Datei bleibt aber als Cache liegen.
- Tests: `tests/test_cookie_mode.py`, `tests/test_prime_first_hit.py`, `tests/test_search_prime.py`.
