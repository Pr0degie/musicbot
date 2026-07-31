"""Tests für normalize_title (Autoplay-Duplikat-Erkennung).

Reine Funktion, keine yt_dlp/discord-Abhängigkeit → läuft überall.
"""

from utils.text import normalize_title


def test_basic_artist_title():
    # Wörter werden kleingeschrieben und alphabetisch sortiert.
    assert normalize_title("AHA - Take on Me") == "aha me on take"


def test_word_order_independent():
    # Kernzweck: Wortreihenfolge darf keine Rolle spielen.
    assert normalize_title("AHA Take on Me") == normalize_title("Take on Me AHA")


def test_case_insensitive():
    assert normalize_title("Daft Punk") == normalize_title("DAFT PUNK")


def test_brackets_removed():
    # "(Official Video)" & Co. fallen weg.
    assert normalize_title("Rick Astley - Never Gonna Give You Up (Official Video)") == \
        normalize_title("Rick Astley - Never Gonna Give You Up")


def test_square_brackets_removed():
    assert normalize_title("Song Title [HD]") == normalize_title("Song Title")


def test_feat_stripped():
    assert normalize_title("Artist - Song feat. Someone") == \
        normalize_title("Artist - Song")


def test_ft_variant_stripped():
    assert normalize_title("Artist - Song ft. Other") == normalize_title("Artist - Song")


def test_pipe_segment_with_dash_preferred():
    # Dokumentiertes Beispiel: Segment mit ' - ' wird bevorzugt.
    a = normalize_title("Winner's Performance | DARA - Bangaranga (Reprise) | Live")
    b = normalize_title("DARA - Bangaranga | Official")
    assert a == b == "bangaranga dara"


def test_special_chars_become_separators():
    # Sonderzeichen (auch Apostrophe) werden zu Trennern, nicht entfernt:
    # "Don't" -> "don" + "t". Dokumentiert das tatsächliche Verhalten.
    assert normalize_title("Don't Stop!!!") == "don stop t"


def test_no_dash_normalizes_whole_title():
    # Ohne ' - '-Segment wird der GANZE Titel normalisiert – der frühere
    # Fallback auf segments[0] erzeugte Mini-Wortmengen ("Just A Title"),
    # gegen die kein Duplikat-Check mehr matchen konnte.
    assert normalize_title("Just A Title | Context") == "a context just title"


def test_variant_keyword_detected_in_brackets():
    from utils.text import has_variant_keyword

    assert has_variant_keyword("Africa (Toto Cover) - Alex Melton")
    assert has_variant_keyword("Toto - Africa (Live at Wembley)")
    assert has_variant_keyword("Africa (sped up)")
    assert has_variant_keyword("Africa Nightcore")
    assert not has_variant_keyword("Toto - Africa")
    assert not has_variant_keyword("Alive - Pearl Jam")   # 'live' nur als ganzes Wort


def test_title_core_words_keeps_brackets_drops_variant_keywords():
    from utils.text import title_core_words

    # Klammer-Inhalt bleibt (Original-Künstler!), Varianten-Schlagwort fliegt.
    assert title_core_words("Africa (Toto Cover) - Alex Melton") == {"africa", "toto", "alex", "melton"}


def test_idempotent():
    once = normalize_title("AHA - Take On Me (Official Video)")
    assert normalize_title(once) == once
