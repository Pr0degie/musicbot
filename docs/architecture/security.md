# Security & YouTube-Cookies

[← Architektur-Übersicht](../architecture.md)

## Security

Grundsatz (Flag-Defaults = Altverhalten, `allowed_mentions=none()`-Ausnahme, Owner-Garantie, Check-Verhalten
in Tests) → [ADR 0004](../adr/0004-security-flags-default-altverhalten.md). Die Flags:

- **`URL_VALIDATION`** = `off` | `warn` (Default) | `block` — SSRF-Schutz (`utils/url_check.py`) für `!radio
  <url>` und `!next url||titel`. Nur http/https; Hostname darf nach DNS-Resolve nicht auf
  private/loopback/link-local IPs zeigen. `warn` spielt wie bisher + Warnung in Log/Channel; `block` lehnt mit
  i18n-Meldung ab. Nicht auflösbare Hosts gelten als ok (Stream scheitert ohnehin). `!radio` persistiert neue
  Sender erst **nach** erfolgreichem Stream-Start (`_play_radio_stream` → `bool`).
- **`REQUIRE_SAME_VOICE`** = `false` (Default) | `true` — Wiedergabe-steuernde Commands (`!s`, `!stop`,
  `!clear`, `!eq`, `!seek`, `!now`, `!remove`, `!move`, `!shuffle`) erfordern denselben Voice-Channel wie der
  Bot (`utils/checks.py` → `require_same_voice()`). Bot nicht in Voice → kein Check.
- **`ADMIN_ROLE_ID`** = leer (Default = kein Gating) | Rollen-ID — `!radio delete/rename` (inline
  `check_admin()`), `!reloadcookies`, `!format` nur für diese Rolle oder den Owner.
- **`!loadq`** validiert ohne Flag (nur Fehlerpfad geändert): Datei muss Liste von `[url,
  titel]`-String-Paaren sein (`_is_valid_playlist` in `music_queue_io.py`), max. `HARD_PLAYLIST_LIMIT`
  Einträge — sonst i18n-Fehlermeldung statt Traceback. Valide saveq-Dateien laden unverändert.

## YouTube Authentication (Cookies)

Cookie config read from `.env` via `update_ydl()`. `cookiefile` takes priority over `cookiesfrombrowser`.
Details → `SETUP.md`.

**Cookielos im Normalbetrieb** (`_cookie_mode = False`): Eine angemeldete YouTube-Session bekommt
Pre-Roll-Werbung, deren Skip-Zeit yt_dlp vor dem ersten Byte abwarten muss (`format["available_at"]`, sonst
CDN-403) — das kostete 5–6 s Startlatenz pro ungecachtem Track. Cookies schaltet erst `enable_cookie_mode()`
zu: automatisch bei Auth-Fehlern (`COOKIE_ERROR_MARKERS` → Alterssperre, Bot-Check, Mitglieder-Video; dann ein
Zweitversuch derselben Abfrage) oder manuell per `!reloadcookies`. Danach bleibt der Modus für den Rest des
Prozesses aktiv und der Metadaten-Cache wird geleert. Alle Extraktionen laufen über
`dl.extract_info_async(query, kind)` → [ADR 0010](../adr/0010-cookielos-zuerst-cookie-fallback.md).
