# FFmpeg-Filterchains pro EQ-Preset.
#
# Struktur aller aktiven Presets:
#   asetpts=N/SR/TB  → normalisiert YouTube-Stream-Timestamps vor dem ersten Filter;
#                      verhindert Geschwindigkeits-Artefakte bei manchen Videos –
#                      NICHT entfernen.
#   <Filter>         → preset-spezifische Kette
#   aresample=48000  → explizite Ausgabe auf 48 kHz für Discord
#
# "flat" ist leer → play_next übergibt codec="copy" (verlustfreies Opus-Durchreichen,
# kein Re-Encode, kein Qualitätsverlust).
#
# !vol gibt es bewusst nicht – Lautstärke-Normalisierung ist Aufgabe des EQ-Presets.
#
# Tracks die kürzer als 2 s laufen, loggen eine Warnung mit aktivem Preset
# (Hinweis auf defekten Filter oder ungültige Datei).
#
# NICHT hinzufügen:
#   resampler=soxr       → crash auf diesem FFmpeg-Build
#   aformat=sample_fmts=fltp → crash auf diesem FFmpeg-Build
#   stereotools          → crash auf diesem FFmpeg-Build

EQ_PRESETS: dict[str, str] = {
    "bassboost": "-af asetpts=N/SR/TB,bass=g=12,dynaudnorm=f=200,volume=0.85,aresample=48000:async=1",
    "flat": "",
    "vocalboost": (
        "-af asetpts=N/SR/TB"
        ",highpass=f=80"
        ",equalizer=f=1000:width_type=o:width=2:g=4"
        ",equalizer=f=3000:width_type=o:width=2:g=3"
        ",equalizer=f=10000:width_type=h:width=10000:g=2"
        ",aresample=48000:async=1"
    ),
    # +20 dB Bass-Boost mit alimiter dahinter – ohne Limiter würde hard-clippen.
    "superbass": "-af asetpts=N/SR/TB,bass=g=20,alimiter=level_in=1:level_out=0.9:limit=0.9:attack=5:release=50,aresample=48000:async=1",
    # Sub-Bass (~80 Hz) boosten für Punch, Upper-Bass (~250 Hz) leicht senken gegen
    # Matsch. alimiter sorgt für konsistente Lautstärke ohne Pumpen.
    "punchy": (
        "-af asetpts=N/SR/TB"
        ",equalizer=f=80:width_type=o:width=2:g=5"
        ",equalizer=f=250:width_type=o:width=2:g=-2"
        ",alimiter=level_in=1:level_out=0.9:limit=0.9:attack=5:release=50"
        ",aresample=48000:async=1"
    ),
    "nightcore": "-af asetpts=N/SR/TB,asetrate=60000,aresample=48000:async=1",
    "karaoke": "-af asetpts=N/SR/TB,pan=stereo|c0=c0-0.5*c1|c1=c1-0.5*c0,aresample=48000:async=1",
    "8d": "-af asetpts=N/SR/TB,apulsator=hz=0.1:mode=sine:offset_l=0:offset_r=0.5,aecho=0.8:0.88:60:0.4,aresample=48000:async=1",
}
