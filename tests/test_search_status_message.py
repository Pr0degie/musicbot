"""Tests für die Status-Nachricht ("Suche läuft..." / "Verarbeite...") in
_extract_info_or_report: Sie wird nach Abschluss gelöscht – bei Erfolg wie
im Fehlerfall, und auch wenn Discord das Löschen verweigert (kein Crash)."""

import asyncio

from conftest import FakeCtx, build_cog
from cogs.music import _YTDLP_FAILED
from utils.i18n import t


class FakeYdl:
    """Minimaler yt_dlp-Ersatz: liefert ein festes Ergebnis oder wirft."""

    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc

    def extract_info(self, query, download=False):
        if self.exc:
            raise self.exc
        return self.result


def _call(mc, ctx, ydl):
    return mc._extract_info_or_report(
        ctx, "ytsearch3:test", ydl,
        status_key="status.searching", timeout_key="error.search_timeout",
        error_key="error.search_error", log_msg="[test] Fehler bei Suche",
    )


def test_status_message_deleted_on_success():
    async def run():
        mc = build_cog()
        ctx = FakeCtx()
        info = {"entries": [{"title": "Treffer"}]}

        result = await _call(mc, ctx, FakeYdl(result=info))

        assert result is info
        assert ctx.texts == [t("status.searching")]
        assert ctx.messages[0].deleted is True, "Status-Nachricht muss nach der Suche gelöscht werden"

    asyncio.run(run())


def test_status_message_deleted_on_error():
    async def run():
        mc = build_cog()
        ctx = FakeCtx()

        result = await _call(mc, ctx, FakeYdl(exc=RuntimeError("kaputt")))

        assert result is _YTDLP_FAILED
        assert ctx.texts == [t("status.searching"), t("error.search_error")]
        assert ctx.messages[0].deleted is True, "Status-Nachricht muss auch im Fehlerfall gelöscht werden"
        assert ctx.messages[1].deleted is False, "Fehlermeldung muss stehen bleiben"

    asyncio.run(run())


def test_delete_failure_does_not_crash():
    async def run():
        mc = build_cog()
        ctx = FakeCtx()
        info = {"entries": []}

        # Discord verweigert das Löschen (z. B. Nachricht schon weg) → Ergebnis
        # kommt trotzdem normal zurück.
        async def _failing_delete():
            raise RuntimeError("404 Not Found")

        orig_send = ctx.send

        async def send_with_broken_delete(*args, **kwargs):
            msg = await orig_send(*args, **kwargs)
            msg.delete = _failing_delete
            return msg

        ctx.send = send_with_broken_delete

        result = await _call(mc, ctx, FakeYdl(result=info))
        assert result is info

    asyncio.run(run())
