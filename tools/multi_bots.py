"""Startet mehrere Instanzen des Musikbots gleichzeitig – je ein Bot-Account pro Token.

    python tools/multi_bots.py start            # alle Tokens aus multi_bots/tokens.txt
    python tools/multi_bots.py start -n 3       # nur die ersten 3
    python tools/multi_bots.py status           # Live-Zustand aller Instanzen (/debug)

Anleitung (Bots im Developer Portal anlegen, Tokens eintragen) → docs/multi-bots.md.

Jede Instanz läuft mit demselben Code, aber in ihrem eigenen Ordner
multi_bots/botN/ als Arbeitsverzeichnis: bot.log, downloads/, Playlists und die
JSON-Dateien liegen relativ dazu und kommen sich so nicht in die Quere (ein
gemeinsamer downloads/-Ordner würde die In-Flight-Registry aushebeln, ADR 0002).
Einstellungen kommen aus der Haupt-.env; pro Instanz überschrieben werden nur
DISCORD_TOKEN, DM_BRIDGE_PORT (Hauptport + N, Bridge immer nur auf localhost)
und die Sprachsteuerung (aus – zehn Sprachmodelle würden die GPU sprengen).

Strg+C zweimal wirkt wie beim normalen Bot auf alle Instanzen gleichzeitig
(sie hängen an derselben Konsole); das Skript wartet, bis alle beendet sind.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / "multi_bots"
TOKENS_FILE = ROOT / "tokens.txt"
DEFAULT_PORT = 8765
START_DELAY = 2.0   # Sekunden zwischen den Starts – verteilt Login- und CPU-Last

TOKENS_TEMPLATE = """\
# Ein Bot-Token pro Zeile (Developer Portal -> Application -> Bot -> Reset Token).
# Zeilen mit # werden ignoriert. Anleitung: docs/multi-bots.md
# Diese Datei ist per .gitignore ausgeschlossen - Tokens nie committen.
"""

TOKEN_RE = re.compile(r"[\w-]{20,}\.[\w-]{4,}\.[\w-]{20,}")


def parse_tokens(text: str) -> list[str]:
    """Tokens aus dem Dateiinhalt: eine pro Zeile, # = Kommentar, ohne Duplikate."""
    tokens = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line and line not in tokens:
            tokens.append(line)
    return tokens


def looks_like_token(token: str) -> bool:
    """Bot-Tokens haben drei Teile mit Punkt dazwischen. Fängt den häufigsten
    Fehler ab: das 'Client Secret' von der OAuth2-Seite statt des Bot-Tokens."""
    return bool(TOKEN_RE.fullmatch(token))


def read_env_file(path: Path) -> dict[str, str]:
    """Liest KEY=VALUE aus einer .env (ohne python-dotenv, damit das Skript
    auch ohne venv startet). Anführungszeichen um den Wert werden entfernt."""
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key.strip()] = value
    return env


def main_port(repo_env: dict[str, str]) -> int:
    try:
        return int(repo_env.get("DM_BRIDGE_PORT") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


def instance_env(base_env: dict, repo_env: dict, token: str, port: int) -> dict:
    """Umgebung für eine Instanz: Haupt-.env < Prozess-Umgebung < Instanz-Werte."""
    env = {**repo_env, **base_env}
    env["DISCORD_TOKEN"] = token
    env["DM_BRIDGE_PORT"] = str(port)
    env["DM_BRIDGE_HOST"] = "127.0.0.1"
    env["VOICE_CONTROL"] = "false"
    env["VOICE_OWN_LISTEN"] = "false"
    env.pop("MUSICBOT_LOG_FILE", None)   # jede Instanz ihr eigenes bot.log
    env["PYTHONIOENCODING"] = "utf-8"
    # Relative Cookie-Pfade gelten vom Repo aus, nicht vom Instanz-Ordner.
    cookies = env.get("YDL_COOKIES_FILE", "")
    if cookies and not Path(cookies).is_absolute():
        env["YDL_COOKIES_FILE"] = str(REPO / cookies)
    return env


def load_tokens() -> list[str]:
    if not TOKENS_FILE.exists():
        ROOT.mkdir(exist_ok=True)
        TOKENS_FILE.write_text(TOKENS_TEMPLATE, encoding="utf-8")
        sys.exit(f"{TOKENS_FILE} angelegt - trag dort die Bot-Tokens ein "
                 f"(Anleitung: docs/multi-bots.md) und starte nochmal.")
    tokens = parse_tokens(TOKENS_FILE.read_text(encoding="utf-8"))
    if not tokens:
        sys.exit(f"Keine Tokens in {TOKENS_FILE} (Anleitung: docs/multi-bots.md).")
    return tokens


def prepare_dir(index: int) -> Path:
    workdir = ROOT / f"bot{index}"
    workdir.mkdir(parents=True, exist_ok=True)
    stations = REPO / "radio_stations.json"
    if stations.exists() and not (workdir / stations.name).exists():
        shutil.copy2(stations, workdir / stations.name)
    return workdir


def cmd_start(args) -> int:
    tokens = load_tokens()
    if args.count:
        tokens = tokens[: args.count]
    repo_env = read_env_file(REPO / ".env")

    bad = [i for i, tok in enumerate(tokens, 1) if not looks_like_token(tok)]
    if bad:
        sys.exit(f"Zeile(n) {bad} sehen nicht wie ein Bot-Token aus (drei Teile mit Punkt). "
                 f"Häufig: 'Client Secret' statt 'Token' kopiert - siehe docs/multi-bots.md.")
    if repo_env.get("DISCORD_TOKEN") in tokens:
        sys.exit("Ein Token in tokens.txt ist das des Hauptbots aus .env - derselbe Bot "
                 "zweimal eingeloggt kämpft um die Voice-Verbindung. Bitte entfernen.")

    base_port = main_port(repo_env)
    procs = []
    for i, token in enumerate(tokens, 1):
        workdir = prepare_dir(i)
        port = base_port + i
        console = open(workdir / "console.log", "w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, str(REPO / "main.py")],
            cwd=workdir,
            env=instance_env(dict(os.environ), repo_env, token, port),
            stdout=console,
            stderr=subprocess.STDOUT,
        )
        procs.append((i, port, proc, console))
        print(f"bot{i}: gestartet (PID {proc.pid}), Bridge http://127.0.0.1:{port}/debug")
        if i < len(tokens):
            time.sleep(START_DELAY)

    print(f"\n{len(procs)} Instanz(en) laufen. Logs: multi_bots/botN/bot.log, "
          f"Status: python tools/multi_bots.py status")
    print("Beenden: zweimal Strg+C (wirkt auf alle Instanzen).\n")
    return _wait_all(procs)


def _wait_all(procs) -> int:
    running = {i: (port, proc, console) for i, port, proc, console in procs}
    while running:
        try:
            for i, (port, proc, console) in list(running.items()):
                code = proc.poll()
                if code is None:
                    continue
                console.close()
                del running[i]
                hint = "" if code == 0 else f" - letzte Zeilen in multi_bots/bot{i}/console.log"
                print(f"bot{i}: beendet (Code {code}){hint}")
            time.sleep(0.5)
        except KeyboardInterrupt:
            # Die Instanzen bekommen das Strg+C selbst (gleiche Konsole) und
            # fahren nach dem zweiten geordnet herunter – wir warten nur.
            continue
    return 0


def fetch_debug(port: int, timeout: float = 2.0):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/debug", timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def describe(data) -> str:
    if data is None:
        return "nicht erreichbar"
    bot = data.get("bot", "?")
    vcs = data.get("voice_clients") or []
    music = data.get("music") or {}
    if not vcs:
        return f"{bot} - in keinem Voice-Channel"
    vc = vcs[0]
    state = "spielt" if vc.get("playing") else ("pausiert" if vc.get("paused") else "still")
    track = music.get("track") or music.get("radio") or "-"
    return f"{bot} - #{vc.get('channel')} - {state}: {track}"


def cmd_status(args) -> int:
    tokens = load_tokens()
    base_port = main_port(read_env_file(REPO / ".env"))
    for i in range(1, len(tokens) + 1):
        port = base_port + i
        print(f"bot{i} (:{port}): {describe(fetch_debug(port))}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Mehrere Musikbot-Instanzen gleichzeitig.")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start", help="alle Instanzen starten")
    start.add_argument("-n", "--count", type=int, default=0, help="nur die ersten N Tokens")
    start.set_defaults(func=cmd_start)
    status = sub.add_parser("status", help="Live-Zustand aller Instanzen")
    status.set_defaults(func=cmd_status)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
