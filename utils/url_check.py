"""SSRF-Schutz für vom User gelieferte Stream-URLs (!radio <url>, !next url||titel).

check_url() ist bewusst synchron (DNS-Resolve blockiert) – Aufrufer im Event-Loop
nutzen enforce_url_policy(), das den Resolve in einen Thread auslagert.
Verhalten je URL_VALIDATION (config.py): off = kein Check, warn (Default) =
abspielen wie bisher + Warnung in Log und Channel, block = ablehnen mit
i18n-Meldung.
"""

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

import config
from utils.i18n import t
from utils.logger import logger


def check_url(url: str):
    """Prüft eine URL auf SSRF-Risiken. Gibt (ok, grund) zurück.

    grund: None (ok), "scheme" (kein http/https bzw. kein Hostname) oder
    "private" (Hostname löst auf eine private/loopback/link-local IP auf).
    Nicht auflösbare Hostnamen gelten als ok – der Stream-Start scheitert
    dann ohnehin von selbst.
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
    except ValueError:
        return False, "scheme"
    if parsed.scheme not in ("http", "https") or not hostname:
        return False, "scheme"
    try:
        infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError, OSError):
        return True, None
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            return False, "private"
    return True, None


async def enforce_url_policy(ctx, url: str) -> bool:
    """Wendet URL_VALIDATION auf eine User-URL an. False = Wiedergabe abbrechen."""
    mode = getattr(config, "URL_VALIDATION", "warn")
    if mode == "off":
        return True
    ok, reason = await asyncio.to_thread(check_url, url)
    if ok:
        return True
    if mode == "block":
        key = "error.url_scheme" if reason == "scheme" else "error.url_private"
        logger.warning(f"[URL-Check] Blockiert ({reason}): {url}")
        await ctx.send(t(key))
        return False
    logger.warning(f"[URL-Check] Verdächtige URL ({reason}), warn-Modus – spiele trotzdem: {url}")
    await ctx.send(t("warn.url_suspicious"))
    return True
