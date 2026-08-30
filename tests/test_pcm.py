"""Tests für die Audio-Umrechnung zwischen Discord und Whisper.

Discord liefert 48 kHz, 16 bit, stereo. Whisper will 16 kHz mono float32.
48000/16000 ist exakt 3, deshalb reicht Mittelung über je drei Samples –
das wirkt zugleich als grober Anti-Alias-Filter. Kein scipy, kein torchaudio.
"""
import numpy as np
import pytest

from utils.pcm import pcm48_stereo_to_f32_16k, pcm_duration_seconds, rms_dbfs


def pcm_bytes(samples):
    """samples: Liste von (links, rechts)-Paaren als int16."""
    flach = np.array([w for paar in samples for w in paar], dtype="<i2")
    return flach.tobytes()


# --- Format -----------------------------------------------------------------

def test_ergebnis_ist_float32():
    ergebnis = pcm48_stereo_to_f32_16k(pcm_bytes([(100, 100)] * 300))
    assert ergebnis.dtype == np.float32


def test_laenge_schrumpft_um_faktor_sechs():
    """Zwei Kanäle zu einem, dann 48 kHz zu 16 kHz: 12 Bytes ergeben 1 Sample."""
    ergebnis = pcm48_stereo_to_f32_16k(pcm_bytes([(0, 0)] * 300))
    assert len(ergebnis) == 100


def test_wertebereich_bleibt_zwischen_minus_eins_und_eins():
    ergebnis = pcm48_stereo_to_f32_16k(pcm_bytes([(32767, -32768)] * 300))
    assert ergebnis.min() >= -1.0
    assert ergebnis.max() <= 1.0


# --- Rechnen ----------------------------------------------------------------

def test_kanaele_werden_gemittelt():
    ergebnis = pcm48_stereo_to_f32_16k(pcm_bytes([(1000, 2000)] * 3))
    assert ergebnis[0] == pytest.approx(1500 / 32768.0, abs=1e-6)


def test_konstantes_signal_bleibt_konstant():
    """Prüft, dass beim Dezimieren nichts verschoben wird."""
    ergebnis = pcm48_stereo_to_f32_16k(pcm_bytes([(8000, 8000)] * 300))
    assert np.allclose(ergebnis, 8000 / 32768.0, atol=1e-6)


# --- Robustheit -------------------------------------------------------------

def test_leerer_puffer_gibt_leeres_array():
    assert len(pcm48_stereo_to_f32_16k(b"")) == 0


@pytest.mark.parametrize("ueberhang", [1, 2, 3, 5, 7, 11])
def test_unvollstaendiger_letzter_frame_wird_abgeschnitten(ueberhang):
    """Ein halb angekommenes Paket darf nichts werfen."""
    roh = pcm_bytes([(500, 500)] * 300) + b"\x01" * ueberhang
    ergebnis = pcm48_stereo_to_f32_16k(roh)
    assert ergebnis.dtype == np.float32
    assert len(ergebnis) >= 99


# --- Dauer und Pegel --------------------------------------------------------

def test_dauer_aus_bytes():
    eine_sekunde = 48000 * 2 * 2   # 48 kHz, 2 Kanäle, 2 Byte
    assert pcm_duration_seconds(eine_sekunde) == pytest.approx(1.0)
    assert pcm_duration_seconds(0) == 0


def test_stille_hat_sehr_niedrigen_pegel():
    """Das Rauschgate vor der GPU hängt daran."""
    assert rms_dbfs(pcm_bytes([(0, 0)] * 300)) < -80


def test_vollausschlag_liegt_nahe_null_dbfs():
    assert rms_dbfs(pcm_bytes([(32000, 32000)] * 300)) > -2


def test_leerer_puffer_gilt_als_stille():
    assert rms_dbfs(b"") < -80
