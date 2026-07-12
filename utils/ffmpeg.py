"""Zustandslose FFmpeg-Helfer (kein discord/yt_dlp-State → überall testbar).

Header-Optionen für den Stream-Pfad, stderr-Puffer-Auswertung und die grobe
Fehler-Klassifikation. Reine Funktionen ohne Cog-Bezug; in cogs/music.py über
Klassen-Aliase weiterhin als MusicCommands._ffmpeg_header_opts o. ä. erreichbar.
"""

import shlex


def ffmpeg_header_opts(info) -> str:
    """-headers-Option für FFmpeg aus dem yt_dlp-Info-Dict.

    yt_dlp hat die Stream-URL mit genau diesen Headern (User-Agent & Co.)
    angefragt – ohne sie lehnen googlevideo-CDN-Server Anfragen sporadisch
    mit 403 ab. shlex.quote reicht als Quoting, weil discord.py
    before_options mit shlex.split zerlegt (kein Shell-Aufruf); die
    \\r\\n-getrennte Header-Liste ist dasselbe Format, das yt_dlp selbst
    an FFmpeg übergibt.
    """
    headers = (info.get("http_headers") or {}) if info else {}
    if not headers:
        return ""
    blob = "".join(f"{key}: {value}\r\n" for key, value in headers.items())
    return f"-headers {shlex.quote(blob)}"


def stderr_tail(buf, max_lines: int = 20) -> str:
    """Liest die letzten Zeilen aus dem FFmpeg-stderr-Puffer und schließt ihn."""
    if buf is None:
        return ""
    try:
        buf.seek(0)
        data = buf.read()
    except Exception:
        return ""
    finally:
        try:
            buf.close()
        except Exception:
            pass
    lines = data.decode("utf-8", errors="replace").strip().splitlines()
    return "\n".join(lines[-max_lines:])


def classify_ffmpeg_error(stderr_text: str):
    """Ordnet FFmpeg-stderr grob ein: 'input' (Netz/HTTP-Quelle), 'filter' (EQ-Kette)
    oder None (keine verwertbare Ausgabe). Filter-Marker gewinnen, weil sie
    eindeutig sind – Input-Marker sind breiter gefasst."""
    if not stderr_text:
        return None
    low = stderr_text.lower()
    filter_markers = (
        "error initializing filter",
        "error reinitializing filters",
        "no such filter",
        "invalid filter",
        "error applying option",
    )
    input_markers = (
        "403 forbidden",
        "404 not found",
        "http error",
        "invalid data found",
        "connection reset",
        "connection refused",
        "connection timed out",
        "server returned",
        "input/output error",
        "end of file",
        "error in the pull function",
        "failed to resolve",
    )
    if any(m in low for m in filter_markers):
        return "filter"
    if any(m in low for m in input_markers):
        return "input"
    return None
