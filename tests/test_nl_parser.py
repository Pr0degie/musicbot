"""Tests für den regelbasierten Satz-Parser der Sprachsteuerung.

Der Parser ist rein (keine Discord-/Config-Importe) und deshalb der Ort, an dem
das Verhalten der Sprachsteuerung vollständig festgenagelt wird. Die Eingaben
sind bewusst so geschrieben, wie Whisper sie liefert: mit Großschreibung,
Kommas und Satzzeichen.
"""
import pytest

from utils.nl_parser import VoiceIntent, build_wake_re, parse_utterance


@pytest.fixture
def wake():
    return build_wake_re(("yo bot", "jo bot", "yobot", "ey bot", "hey bot"))


# --- Weckwort ---------------------------------------------------------------

def test_ohne_weckwort_wird_still_ignoriert(wake):
    """Kein Weckwort → (False, None): der Cog darf dann NICHTS in den Chat posten."""
    assert parse_utterance("spiel mal Bohemian Rhapsody", wake) == (False, None)


@pytest.mark.parametrize("satz", [
    "Yo Bot, spiel mal Bohemian Rhapsody.",
    "yo bot spiel mal Bohemian Rhapsody",
    "Jo Bot! Spiel mal Bohemian Rhapsody.",
    "Yo, Bot, spiel mal Bohemian Rhapsody.",
    "Yobot, spiel mal Bohemian Rhapsody.",
    "Hey Bot, spiel mal Bohemian Rhapsody.",
    "Yo Bott, spiel mal Bohemian Rhapsody.",
])
def test_weckwort_varianten_wie_whisper_sie_schreibt(satz, wake):
    woke, intent = parse_utterance(satz, wake)
    assert woke is True
    assert intent is not None
    assert intent.command == "p"
    assert intent.arg == "Bohemian Rhapsody"


def test_weckwort_muss_nicht_am_satzanfang_stehen(wake):
    woke, intent = parse_utterance("ähm, yo Bot, spiel mal Bohemian Rhapsody", wake)
    assert woke is True
    assert intent.arg == "Bohemian Rhapsody"


def test_weckwort_am_satzende_nimmt_den_teil_davor(wake):
    woke, intent = parse_utterance("Spiel mal Bohemian Rhapsody, yo Bot!", wake)
    assert woke is True
    assert intent.arg == "Bohemian Rhapsody"


def test_nur_weckwort_ohne_wunsch_ist_nicht_verstanden(wake):
    """(True, None) → der Cog stellt eine Rückfrage."""
    assert parse_utterance("Yo Bot!", wake) == (True, None)


def test_bot_allein_ist_kein_weckwort(wake):
    assert parse_utterance("der Bot spinnt mal wieder", wake) == (False, None)


# --- Steuerbefehle ----------------------------------------------------------

@pytest.mark.parametrize("satz,erwartet", [
    ("Yo Bot, überspring das mal.",        "s"),
    ("Yo Bot, skip!",                       "s"),
    ("Yo Bot, nächstes Lied bitte.",        "s"),
    ("Yo Bot, mach mal Pause.",             "x"),
    ("Yo Bot, halt mal kurz an.",           "x"),
    ("Yo Bot, mach weiter.",                "resume"),
    ("Yo Bot, spiel weiter!",               "resume"),
    ("Yo Bot, hör auf.",                    "stop"),
    ("Yo Bot, mach die Musik aus.",         "stop"),
    ("Yo Bot, was läuft denn als nächstes?", "q"),
    ("Yo Bot, zeig mir mal die Queue.",     "q"),
    ("Yo Bot, mach die Liste leer.",        "clear"),
    ("Yo Bot, misch mal durch.",            "shuffle"),
    ("Yo Bot, spiel das nochmal.",          "replay"),
    ("Yo Bot, mach mal Dauerschleife.",     "loop"),
    ("Yo Bot, komm mal rein.",              "j"),
    ("Yo Bot, hau ab!",                     "l"),
])
def test_steuerbefehle(satz, erwartet, wake):
    woke, intent = parse_utterance(satz, wake)
    assert woke is True
    assert intent is not None, f"nicht verstanden: {satz}"
    assert intent.command == erwartet


# --- Ordnungsfallen: hier scheitert ein naiver Parser ------------------------
# Alle diese Saetze enthalten ein Spiel-Verb. Griffe die play-Regel zuerst,
# wuerde der Bot nach "aus", "weiter" oder "nochmal" auf YouTube suchen.

@pytest.mark.parametrize("satz,erwartet", [
    ("Yo Bot, mach mal aus.",        "stop"),
    ("Yo Bot, spiel weiter.",        "resume"),
    ("Yo Bot, spiel das nochmal.",   "replay"),
    ("Yo Bot, mach die Queue leer.", "clear"),
    ("Yo Bot, mach mal Pause.",      "x"),
])
def test_steuerbefehle_gewinnen_gegen_die_songsuche(satz, erwartet, wake):
    _, intent = parse_utterance(satz, wake)
    assert intent.command == erwartet, f"{satz!r} wurde faelschlich als {intent} gelesen"


# --- Songtitel-Extraktion ---------------------------------------------------

@pytest.mark.parametrize("satz,titel", [
    ("Yo Bot, spiel mal Bohemian Rhapsody an.",          "Bohemian Rhapsody"),
    ("Yo Bot, leg doch bitte Sandstorm auf.",            "Sandstorm"),
    ("Yo Bot, mach das Lied Numb von Linkin Park an.",   "Numb von Linkin Park"),
    ("Yo Bot, spiele Bohemian Rhapsody von Queen.",      "Bohemian Rhapsody von Queen"),
    ("Yo Bot, pack mal Thunderstruck rein.",             "Thunderstruck"),
])
def test_songtitel_wird_aus_dem_satz_geschnitten(satz, titel, wake):
    _, intent = parse_utterance(satz, wake)
    assert intent == VoiceIntent("p", titel)


def test_kuenstlersuche_was_von_x(wake):
    _, intent = parse_utterance("Yo Bot, spiel mal was von Queen.", wake)
    assert intent == VoiceIntent("p", "Queen")


def test_vage_empfehlungswuensche_fuehren_zur_rueckfrage(wake):
    """"spiel was Chilliges" ist eine Empfehlungsanfrage - bewusst spaeter.
    Bis dahin lieber nachfragen als nach "was Chilliges" zu suchen."""
    assert parse_utterance("Yo Bot, spiel mal was Chilliges.", wake) == (True, None)


# --- Robustheit -------------------------------------------------------------

@pytest.mark.parametrize("text", ["", "   ", "...", "?!", "yo bot ,,, ..."])
def test_muell_eingaben_stuerzen_nicht_ab(text, wake):
    woke, intent = parse_utterance(text, wake)
    assert isinstance(woke, bool)
    assert intent is None


# --- Klang / EQ -------------------------------------------------------------

@pytest.mark.parametrize("satz,preset", [
    ("Yo Bot, mach mal mehr Bass rein.", "bassboost"),
    ("Yo Bot, mach den Sound wieder normal.", "flat"),
    ("Yo Bot, mach mal Nightcore an.", "nightcore"),
    ("Yo Bot, stell auf Karaoke um.", "karaoke"),
])
def test_eq_presets_aus_dem_satz(satz, preset, wake):
    _, intent = parse_utterance(satz, wake)
    assert intent == VoiceIntent("eq", preset)


def test_eq_wortliste_nennt_nur_existierende_presets():
    """Koppelt die Sprach-Synonyme an cogs/presets.py, ohne dass utils/ ein
    Cog importieren muss - die Kopplung wird hier im Test erzwungen."""
    from cogs.presets import EQ_PRESETS
    from utils.nl_parser import EQ_WORDS
    unbekannt = set(EQ_WORDS.values()) - set(EQ_PRESETS)
    assert not unbekannt, f"unbekannte Presets in EQ_WORDS: {unbekannt}"
