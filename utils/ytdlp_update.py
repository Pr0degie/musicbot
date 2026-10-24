"""Einmaliger yt-dlp-Versionsvergleich gegen PyPI (Hintergrund-Task nach on_ready)."""
import yt_dlp

from utils.logger import logger


def _version_tuple(v: str):
    """"2026.7.15" → (2026, 7, 15); None wenn nicht rein numerisch."""
    try:
        return tuple(int(x) for x in v.split("."))
    except (ValueError, AttributeError):
        return None


async def check_ytdlp_update():
    """Vergleicht die installierte yt-dlp-Version einmalig gegen PyPI und loggt
    bei neuerer Version EINE WARNING-Zeile (im Quiet-Terminal sichtbar).
    Kein Auto-Update, Fehler werden still geschluckt; läuft als Hintergrund-
    Task nach on_ready – kein blockierendes I/O im Event-Loop (aiohttp)."""
    try:
        import aiohttp

        installed = yt_dlp.version.__version__
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get("https://pypi.org/pypi/yt-dlp/json") as resp:
                data = await resp.json()
        latest = data["info"]["version"]
        inst_t, latest_t = _version_tuple(installed), _version_tuple(latest)
        newer = (latest_t > inst_t) if (inst_t and latest_t) else (latest != installed)
        if newer:
            logger.warning(
                f"[yt-dlp] Version {latest} verfügbar, installiert ist {installed} – "
                f"Update empfohlen bei YouTube-Problemen (pip install -U yt-dlp)"
            )
        else:
            logger.info(f"[yt-dlp] Version {installed} ist aktuell.")
    except Exception:
        pass  # reiner Komfort-Check – darf nie einen Fehlerpfad einführen
