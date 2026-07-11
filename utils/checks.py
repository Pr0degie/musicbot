"""Rechte-Checks für Commands – beide Flags sind per Default AUS (heutiges Verhalten).

REQUIRE_SAME_VOICE: Wiedergabe-steuernde Commands nur aus dem Voice-Channel des Bots.
ADMIN_ROLE_ID: Verwaltungs-Commands nur für die Admin-Rolle oder den Owner.

Oberste Regel: Der Bot-Owner wird durch keine Flag-Kombination ausgesperrt –
is_owner() gewinnt in beiden Checks immer. Die Fehlermeldung sendet der Check
selbst (i18n); die resultierende CheckFailure schluckt on_command_error still.
"""

import config
from discord.ext import commands
from utils.i18n import t


def require_same_voice():
    """Check: User muss im selben Voice-Channel sein wie der Bot.

    Nur aktiv wenn REQUIRE_SAME_VOICE=true. Ist der Bot in keinem Voice-Channel,
    gibt es nichts zu schützen → durchlassen.
    """

    async def predicate(ctx):
        if not getattr(config, "REQUIRE_SAME_VOICE", False):
            return True
        vc = ctx.voice_client
        if vc is None or getattr(vc, "channel", None) is None:
            return True
        voice = getattr(ctx.author, "voice", None)
        if voice is not None and voice.channel == vc.channel:
            return True
        # Owner-Check zuletzt: kostet ggf. einen API-Call, ist aber die Garantie
        # gegen Selbst-Aussperren.
        if await ctx.bot.is_owner(ctx.author):
            return True
        await ctx.send(t("error.same_voice_required"))
        return False

    return commands.check(predicate)


async def check_admin(ctx) -> bool:
    """True wenn ADMIN_ROLE_ID nicht gesetzt ist, der User die Rolle hat oder Owner ist.

    Direkt aufrufbar (für Subcommands wie !radio delete/rename) – sendet die
    Fehlermeldung selbst.
    """
    role_id = getattr(config, "ADMIN_ROLE_ID", 0)
    if not role_id:
        return True
    roles = getattr(ctx.author, "roles", None) or []
    if any(getattr(r, "id", None) == role_id for r in roles):
        return True
    if await ctx.bot.is_owner(ctx.author):
        return True
    await ctx.send(t("error.admin_required"))
    return False


def require_admin():
    return commands.check(check_admin)
