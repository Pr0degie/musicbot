# ADR 0004: Security-Flags mit Default = Altverhalten + Owner-Garantie

- **Status:** Akzeptiert
- **Datum:** 2026-07-11 (Commits `c152f9a`, `8c47e82`, `b8822b5`, `c176c56`)

## Kontext

SSRF-Schutz, Rechte-Gating und Input-Validierung wurden nachträglich auf einem laufenden Bot eingeführt; bestehende Nutzung sollte sich dadurch nicht ändern.

## Entscheidung

Alle einschränkenden Änderungen hängen an `.env`-Flags, deren **Default das alte Verhalten beibehält** (Ausnahme: globales `allowed_mentions=none()` im Bot-Konstruktor in `main.py` — Fremd-Content wie YouTube-Titel/`!echo`/Lyrics kann nie pingen; kein Command nutzt Mentions absichtlich).

**Owner-Garantie:** `is_owner()` gewinnt in beiden Checks (`require_same_voice()`, `check_admin()`) immer — der Owner kann sich durch keine Flag-Kombination aussperren.

Die einzelnen Flags (`URL_VALIDATION`, `REQUIRE_SAME_VOICE`, `ADMIN_ROLE_ID`) und die flaglose `!loadq`-Validierung → `docs/architecture.md` → *Security*.

## Konsequenzen

- Check-Fehlermeldungen kommen via i18n aus dem Check selbst; die `CheckFailure` schluckt `on_command_error` still.
- Checks greifen nicht in Tests, die Commands über `.callback` aufrufen.
