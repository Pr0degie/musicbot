"""Zustands-Schnappschuss für die Fehlersuche (GET /debug der DM-Bridge).

Ein Blick in den laufenden Bot statt Log-Archäologie: lebt die
Sprachverbindung, wie lange war das Gateway still, was spielt gerade. Liest
nur, ändert nichts, und übersteht fehlende Cogs oder Attribute – ein
Diagnose-Endpunkt, der selbst crasht, hilft niemandem.
"""
import math
import time

from utils.voice import dead_voice_poller


def _r(value, digits=1):
    return round(value, digits) if isinstance(value, (int, float)) else None


def _ms(seconds):
    """Latenz in ms – None solange discord.py noch inf/NaN meldet."""
    if isinstance(seconds, (int, float)) and math.isfinite(seconds):
        return round(seconds * 1000)
    return None


def _voice_client_state(vc):
    channel = getattr(vc, "channel", None)
    try:
        poller = dead_voice_poller(vc)
    except Exception as e:   # Diagnose darf nie selbst scheitern
        poller = f"unbekannt ({type(e).__name__})"
    return {
        "guild": getattr(getattr(vc, "guild", None), "name", None),
        "channel": getattr(channel, "name", None),
        "class": type(vc).__name__,
        "connected": vc.is_connected(),
        "playing": vc.is_playing(),
        "paused": vc.is_paused(),
        # None = Poller lebt; sonst warum die Verbindung tot ist
        "poller_dead": poller,
        "listening": bool(getattr(vc, "_reader", None)),
        "latency_ms": _ms(getattr(vc, "latency", None)),
    }


def _music_state(cog, now):
    track = getattr(cog, "current_track", None)
    elapsed = None
    if track and getattr(cog, "track_start_time", None) is not None:
        elapsed = (getattr(cog, "_current_seek_offset", 0) or 0) + (cog._elapsed_seconds() or 0)
    last_recv = getattr(cog, "_gw_last_recv", None)
    heal_task = getattr(cog, "_heal_task", None)
    return {
        "track": track[1] if track else None,
        "position_s": _r(elapsed, 0),
        "duration_s": track[2] if track and len(track) > 2 else None,
        "queue_len": len(getattr(cog, "queue", ()) or ()),
        "queue_next": [title for _, title in list(getattr(cog, "queue", ()))[:3]],
        "is_playing": getattr(cog, "is_playing", None),
        "stopped_by_user": getattr(cog, "_stopped_by_user", None),
        "radio": getattr(cog, "radio_station_name", None) if getattr(cog, "is_radio", False) else None,
        "loop_mode": getattr(cog, "loop_mode", None),
        "autoplay": getattr(cog, "autoplay_enabled", None),
        "equalizer": getattr(cog, "equalizer", None),
        "gateway_silent_s": _r(now - last_recv) if last_recv else None,
        "voice_healing": getattr(cog, "_voice_healing", None),
        "heal_running": bool(heal_task and not heal_task.done()),
        "uptime_s": _r(now - cog._start_time, 0) if hasattr(cog, "_start_time") else None,
    }


def _listen_state(cog):
    return {
        "enabled": getattr(cog, "enabled", None),
        "own_listening": getattr(cog, "_own_listening", None),
        "model": getattr(cog, "_model_info", None) or None,
    }


def snapshot(bot) -> dict:
    """Der komplette Schnappschuss als JSON-taugliches dict."""
    now = time.monotonic()
    music = bot.get_cog("MusicCommands")
    listen = bot.get_cog("VoiceListen")
    return {
        "bot": str(bot.user),
        "gateway_latency_ms": _ms(bot.latency),
        "dm_speaking": getattr(bot, "dm_speaking", False),
        "voice_clients": [_voice_client_state(vc) for vc in bot.voice_clients],
        "music": _music_state(music, now) if music else None,
        "voice_listen": _listen_state(listen) if listen else None,
    }
