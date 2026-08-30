"""Audio-Umrechnung zwischen Discord und Whisper. Zustandslos.

Discord liefert 48 kHz, 16 bit, stereo, little-endian. Whisper will 16 kHz
mono float32. Kein `audioop` (in Python 3.13 entfernt), kein scipy, kein
FFmpeg-Subprozess – nur numpy, das mit ctranslate2 ohnehin an Bord ist.
"""
import numpy as np

SAMPLE_RATE = 48000
KANAELE = 2
BYTES_PRO_SAMPLE = 2
BYTES_PRO_FRAME = KANAELE * BYTES_PRO_SAMPLE

# 48000 / 16000 = exakt 3 – deshalb genügt Mittelung über je drei Samples.
DEZIMIERUNG = 3

_VOLLAUSSCHLAG = 32768.0


def pcm_duration_seconds(nbytes: int) -> float:
    """Wie viele Sekunden Audio stecken in n Bytes Discord-PCM?"""
    return nbytes / float(SAMPLE_RATE * BYTES_PRO_FRAME)


def pcm48_stereo_to_f32_16k(data: bytes) -> np.ndarray:
    """48 kHz Stereo-PCM → 16 kHz Mono float32, wie Whisper es erwartet.

    Unvollständige Frames am Ende (ein halb angekommenes Paket) werden
    abgeschnitten statt zu werfen. Die Mittelung über drei Samples ersetzt
    einen echten Tiefpass – für Sprache völlig ausreichend.
    """
    # frombuffer wirft bei ungerader Byte-Zahl – ein halb angekommenes Paket
    # darf die Pipeline aber nicht anhalten.
    roh = np.frombuffer(data[: len(data) - (len(data) % BYTES_PRO_SAMPLE)], dtype="<i2")
    if roh.size == 0:
        return np.zeros(0, dtype=np.float32)

    # Auf ganze Stereo-Frames kürzen, dann Kanäle mitteln.
    roh = roh[: roh.size - (roh.size % KANAELE)]
    mono = roh.reshape(-1, KANAELE).mean(axis=1)

    # Auf ein Vielfaches des Dezimierungsfaktors kürzen, dann mitteln.
    mono = mono[: mono.size - (mono.size % DEZIMIERUNG)]
    if mono.size == 0:
        return np.zeros(0, dtype=np.float32)
    schmal = mono.reshape(-1, DEZIMIERUNG).mean(axis=1)

    return (schmal / _VOLLAUSSCHLAG).astype(np.float32)


def rms_dbfs(data: bytes) -> float:
    """Pegel in dBFS – billiges Rauschgate, bevor die GPU bemüht wird.

    Spart Transkriptionen von Tastaturklicks und Lüftergeräuschen.
    """
    roh = np.frombuffer(data[: len(data) - (len(data) % BYTES_PRO_SAMPLE)], dtype="<i2")
    if roh.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(roh.astype(np.float64)))))
    if rms <= 0:
        return -120.0
    return 20.0 * float(np.log10(rms / _VOLLAUSSCHLAG))
