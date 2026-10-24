"""Verdichtet bot.log auf eine Zeitleiste der wichtigen Ereignisse.

    python tools/logsummary.py                 # letzter Bot-Lauf in bot.log
    python tools/logsummary.py bot.log.1       # vorheriger Lauf (Rotation beim Start)
    python tools/logsummary.py --list          # alle Läufe einer Datei im Überblick
    python tools/logsummary.py --session -2    # vorletzter Lauf derselben Datei
    python tools/logsummary.py -n 30           # nur die letzten 30 Ereignisse

Zeigt: Start/Ende, Warnungen und Fehler (Wiederholungen zusammengefasst,
Tracebacks auf die Exception-Zeile gekürzt), Gateway- und Voice-Ereignisse,
Nutzereingaben und gestartete Songs. Lässt weg: Prefetch, Cache, Resolve,
Queue-Speichern und den Verbindungsaufbau-Smalltalk von discord.py.

Importiert bewusst NICHT utils.logger – schon der Import würde das Log eines
gerade laufenden Bots rotieren.
"""
import argparse
import re
import sys
from dataclasses import dataclass, field

LINE_RE = re.compile(r"^(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d),\d+ \[(\w+)\] ([\w.]+): (.*)$")
TAG_RE = re.compile(r"^\[([^\]]+)\]\s*")
SESSION_START = "[INIT] MusicCommands erfolgreich initialisiert"

# INFO-Tags, die in die Zeitleiste gehören (alles andere INFO entfällt).
KEEP_TAGS = {
    "Gateway", "Voice", "Shutdown", "INIT", "restart", "Maintenance",
    "p", "next", "now", "Sprachbefehl", "Voice-Listen",
    "Auto-Join", "Auto-Leave", "Auto-Start", "KEINE VERBINDUNG", "play_next",
    "Radio", "yt-dlp", "Cookies",
}
# discord.py-INFO, die nur den normalen Verbindungsaufbau erzählt.
DISCORD_SMALLTALK = (
    "Connecting to voice", "Starting voice handshake", "Voice handshake complete",
    "Voice connection complete", "logging in using static token",
    "The voice handshake is being terminated",
)
WIDTH = 170


@dataclass
class Event:
    time: str
    level: str
    text: str
    count: int = 1


@dataclass
class Session:
    start_date: str = ""
    start: str = ""
    end: str = ""
    lines: list = field(default_factory=list)


def split_sessions(raw_lines):
    """Teilt die Datei an jedem Bot-Start. Zeilen vor dem ersten Start
    (Datei-Anfang nach einer Rotation) bilden eine eigene Sitzung."""
    sessions = [Session()]
    for line in raw_lines:
        m = LINE_RE.match(line)
        if m and SESSION_START in m.group(5) and sessions[-1].lines:
            sessions.append(Session())
        s = sessions[-1]
        if m:
            if not s.start:
                s.start_date, s.start = m.group(1), m.group(2)
            s.end = m.group(2)
        s.lines.append(line)
    return [s for s in sessions if s.lines]


def classify(level, source, msg):
    """Text für die Zeitleiste – oder None, wenn die Zeile Lärm ist."""
    if level in ("WARNING", "ERROR", "CRITICAL"):
        return msg
    if source.startswith("discord"):
        return None if any(s in msg for s in DISCORD_SMALLTALK) else f"({source}) {msg}"
    m = TAG_RE.match(msg)
    if not m:
        return None
    tag = m.group(1)
    if tag == "Wiedergabe" and msg.startswith("[Wiedergabe] Starte: "):
        return "▶ " + msg[len("[Wiedergabe] Starte: "):]
    return msg if tag in KEEP_TAGS else None


def summarize(session):
    events, stats = [], {"Songs": 0, "Warnungen": 0, "Fehler": 0,
                         "Gateway-Abbrüche": 0, "Voice-Neuaufbauten": 0}
    last = None
    for line in session.lines:
        m = LINE_RE.match(line)
        if not m:
            # Traceback-Fortsetzung: die Exception-Zeile ans letzte Ereignis hängen
            text = line.strip()
            if last and last.level in ("ERROR", "CRITICAL") and re.match(r"^[\w.]+(Error|Exception)\b", text):
                last.text = f"{last.text} → {text}"
            continue
        _, hhmmss, level, source, msg = m.groups()
        if level == "WARNING":
            stats["Warnungen"] += 1
        elif level in ("ERROR", "CRITICAL"):
            stats["Fehler"] += 1
        if "stopped responding to the gateway" in msg:
            stats["Gateway-Abbrüche"] += 1
        if msg.startswith("[Voice] Sprachverbindung") and "neu aufgebaut" in msg:
            stats["Voice-Neuaufbauten"] += 1
        if msg.startswith("[Wiedergabe] Starte: "):
            stats["Songs"] += 1
        text = classify(level, source, msg)
        if text is None:
            continue
        if last and last.text == text and last.level == level:
            last.count += 1
            continue
        last = Event(hhmmss, level, text)
        events.append(last)
    return events, stats


def _fmt(ev):
    mark = {"WARNING": "!", "ERROR": "✖", "CRITICAL": "✖"}.get(ev.level, " ")
    text = ev.text if len(ev.text) <= WIDTH else ev.text[: WIDTH - 1] + "…"
    rep = f"  (×{ev.count})" if ev.count > 1 else ""
    return f"{ev.time} {mark} {text}{rep}"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("file", nargs="?", default="bot.log")
    ap.add_argument("--session", type=int, default=-1, help="Index des Laufs (-1 = letzter)")
    ap.add_argument("--list", action="store_true", help="nur die Läufe auflisten")
    ap.add_argument("-n", type=int, default=80, help="max. Ereignisse (die letzten)")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        with open(args.file, encoding="utf-8", errors="replace") as f:
            sessions = split_sessions(f.read().splitlines())
    except OSError as e:
        print(f"{args.file}: {e}")
        return 1
    if not sessions:
        print(f"{args.file}: leer")
        return 0

    if args.list:
        for i, s in enumerate(sessions):
            _, st = summarize(s)
            print(f"{i - len(sessions):>4}  {s.start_date} {s.start}–{s.end}  "
                  + "  ".join(f"{k} {v}" for k, v in st.items() if v))
        return 0

    try:
        session = sessions[args.session]
    except IndexError:
        print(f"Lauf {args.session} gibt es nicht ({len(sessions)} Läufe, siehe --list)")
        return 1
    events, stats = summarize(session)
    print(f"{args.file}: Lauf {session.start_date} {session.start}–{session.end} "
          f"({len(sessions)} Läufe in der Datei)")
    print("  ".join(f"{k}: {v}" for k, v in stats.items()))
    if len(events) > args.n:
        print(f"… {len(events) - args.n} frühere Ereignisse ausgelassen (-n erhöhen)")
        events = events[-args.n:]
    for ev in events:
        print(_fmt(ev))
    return 0


if __name__ == "__main__":
    sys.exit(main())
