"""Regelbasierter Parser für gesprochene Befehle ("yo bot, spiel mal ...").

Rein und ohne Discord-, Config- oder GPU-Abhängigkeit: was hier steht, ist
vollständig unit-testbar und überlebt jeden Umbau der Audio-Schicht darüber.

Bewusst kein LLM. Der Preis ist, dass nur verstanden wird, was hier steht;
der Gewinn sind null laufende Kosten, keine Netzabhängigkeit und ein
Verhalten, das man Zeile für Zeile nachlesen kann.
"""
import re
from dataclasses import dataclass

# Muss mit Bot Bs Weckwortliste übereinstimmen – driften sie auseinander,
# reagiert der Bot einfach nicht mehr und niemand weiß warum.
WAKE_DEFAULTS = ("yo bot", "jo bot", "yobot", "ey bot", "hey bot")

# Zwischen den Wortteilen des Weckworts darf Whisper Leerzeichen, Kommas,
# Punkte oder Bindestriche setzen – oder gar nichts ("Yobot").
_GAP = r"[\s,.\-]*"

# Whisper schreibt gelegentlich "Bott" oder "Bot." statt "Bot".
_BOT = r"bo?tt?"


def build_wake_re(phrases=WAKE_DEFAULTS):
    """Baut aus den Weckwort-Phrasen eine einzelne Alternation."""
    alts = []
    for phrase in phrases:
        tokens = [_BOT if tok == "bot" else re.escape(tok)
                  for tok in phrase.lower().split()]
        if tokens:
            alts.append(_GAP.join(tokens))
    return re.compile(r"\b(?:" + "|".join(alts) + r")\b", re.IGNORECASE)


@dataclass(frozen=True)
class VoiceIntent:
    """Ein erkannter Befehl: Command-Name ohne "!" plus Argument."""
    command: str
    arg: str = ""


def _normalize(text: str) -> str:
    """Satzzeichen raus, Mehrfach-Leerzeichen zusammen. Schreibweise bleibt."""
    text = re.sub(r"[.!?;:,]+", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def split_wake(text: str, wake_re):
    """Gibt den Befehlsteil hinter dem Weckwort zurück, sonst None.

    Steht das Weckwort am Satzende ("spiel mal X, yo Bot"), wird der Teil
    davor genommen. Kein Weckwort -> None (der Aufrufer schweigt dann).
    """
    matches = list(wake_re.finditer(text or ""))
    if not matches:
        return None
    last = matches[-1]
    rest = _normalize(text[last.end():])
    if len(rest) < 3:
        rest = _normalize(text[:last.start()])
    return rest


# Reihenfolge IST Korrektheit: geprüft wird von spezifisch nach generisch.
# Die Songsuche steht als Catch-all ganz unten, weil fast jeder Steuersatz
# ebenfalls ein Spiel-Verb enthält ("mach mal aus", "spiel weiter").
# Zwei Feinheiten, die je einen Regressionstest haben:
#   - clear vor q, sonst wird "mach die Queue leer" zum Anzeigen-Befehl
#   - q vor s, sonst frisst "nächstes" das "was läuft als nächstes"
_RULES = (
    ("stop",    r"\b(?:stopp?|h(?:ö|oe)r(?:\s+mal)?\s+auf|mach(?:\s+mal|\s+die\s+musik)*\s+aus|beenden?|schluss|ruhe)\b"),
    ("x",       r"\b(?:pause|pausier(?:e|en)?|halt(?:\s+mal)?(?:\s+kurz)?\s+an|moment\s+mal)\b"),
    ("resume",  r"\b(?:mach\s+weiter|spiele?\s+weiter|weiter(?:spielen|machen)|weiter\s+geht'?s|fortsetzen?|resume)\b"),
    ("clear",   r"\b(?:queue|warteschlange|liste|playlist)\b.{0,15}?"
                r"\b(?:leer(?:en)?|l(?:ö|oe)sch(?:e|en)?|weg)\b"
                r"|\balles\s+l(?:ö|oe)schen\b"),
    ("q",       r"\b(?:was\s+(?:l(?:ä|ae)uft|kommt)|zeig\w*(?:\s+\w+){0,3}\s+(?:queue|warteschlange|liste)|queue|warteschlange)\b"),
    ("s",       r"\b(?:skip(?:pe|pen)?|(?:ü|ue)berspring(?:e|en)?|n(?:ä|ae)chst(?:er|es|en|e)|weiter\s+zum\s+n(?:ä|ae)chsten)\b"),
    ("shuffle", r"\b(?:shuffle|misch(?:e|en)?|durchmischen|zufall\w*|random)\b"),
    ("replay",  r"\b(?:noch\s?mal(?:\s+von\s+vorn\w*)?|wiederhol(?:e|en)?|replay|von\s+vorne)\b"),
    ("loop",    r"\b(?:loop|schleife|dauerschleife|endlosschleife)\b"),
    ("j",       r"\b(?:komm(?:\s+mal)?\s+(?:rein|r(?:ü|ue)ber|her|zu\s+uns)|join)\b"),
    ("l",       r"\b(?:hau\s+ab|verschwinde|geh(?:\s+mal)?\s+raus|verlass\w*|leave)\b"),
)

INTENT_RULES = tuple((cmd, re.compile(pat, re.IGNORECASE)) for cmd, pat in _RULES)

# Gesprochene Klang-Wuensche auf die Presets aus cogs/presets.py abbilden.
# Bewusst als Wortliste hier statt per Import: utils/ soll kein Cog laden.
# tests/test_nl_parser.py erzwingt, dass jeder Wert wirklich ein Preset ist.
EQ_WORDS = {
    "mehr bass": "bassboost",
    "bass": "bassboost",
    "bassboost": "bassboost",
    "richtig bass": "superbass",
    "superbass": "superbass",
    "normal": "flat",
    "flat": "flat",
    "stimme": "vocalboost",
    "vocal": "vocalboost",
    "punchy": "punchy",
    "nightcore": "nightcore",
    "karaoke": "karaoke",
    "8d": "8d",
}

# Laengste Phrasen zuerst, damit "mehr bass" vor "bass" greift.
_EQ_RE = re.compile(
    r"\b(?P<w>" + "|".join(re.escape(k) for k in
                            sorted(EQ_WORDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


_FILLERS = r"(?:mal|doch|bitte|mir|uns|jetzt|kurz|noch|einfach|so)"
_OBJECTS = r"(?:den song|das lied|den track|den titel|die nummer|song|lied|track|titel)"
_PLAY_VERBS = r"(?:spiel|spiele|spielt|leg|mach|hau|pack|nimm|start|starte)"

_PLAY_RE = re.compile(
    r"\b" + _PLAY_VERBS + r"\b\s*(?:" + _FILLERS + r"\s+)*(?:" + _OBJECTS + r"\s+)?(?P<q>.+)$",
    re.IGNORECASE,
)

_TRAILING = re.compile(r"\s+(?:an|ab|rein|auf|los|" + _FILLERS + r")$", re.IGNORECASE)

# Vage Wünsche ("spiel was Chilliges") sind Empfehlungsanfragen – bewusst
# noch nicht unterstützt. Lieber nachfragen als Zufallstreffer suchen.
_VAGUE_RE = re.compile(r"^(?:was|irgend\s?was|etwas)\b(?!\s+von\s+)", re.IGNORECASE)

# "spiel was von Queen" ist dagegen eine Künstlersuche, die die Suche kann.
_ARTIST_RE = re.compile(r"^(?:was|etwas)\s+von\s+(?P<a>.+)$", re.IGNORECASE)


def _clean_song_query(q: str):
    """Schneidet Füllwörter und nachgestellte Partikel vom Songtitel ab."""
    q = (q or "").strip().strip("\"'„“”")
    artist = _ARTIST_RE.match(q)
    if artist:
        q = artist.group("a").strip()
    elif _VAGUE_RE.match(q):
        return None
    while True:
        shortened = _TRAILING.sub("", q).strip()
        if shortened == q:
            break
        q = shortened
    return q if len(q) >= 2 else None


def parse_utterance(text: str, wake_re=None):
    """Zerlegt eine Äußerung in (weckwort_gefunden, VoiceIntent | None).

    Die Unterscheidung ist wichtig: ohne Weckwort war der Satz nicht an den
    Bot gerichtet und muss still ignoriert werden. Mit Weckwort, aber ohne
    erkannten Befehl, gehört eine Rückfrage in den Chat.
    """
    wake_re = wake_re or build_wake_re()
    rest = split_wake(text, wake_re)
    if rest is None:
        return (False, None)
    if not rest:
        return (True, None)

    for command, pattern in INTENT_RULES:
        if pattern.search(rest):
            return (True, VoiceIntent(command))

    eq = _EQ_RE.search(rest)
    if eq:
        return (True, VoiceIntent("eq", EQ_WORDS[eq.group("w").lower()]))

    m = _PLAY_RE.search(rest)
    if m:
        query = _clean_song_query(m.group("q"))
        if query:
            return (True, VoiceIntent("p", query))
    return (True, None)
