"""Spracherkennung – die einzige Stelle, die faster-whisper kennt.

Das Modell wird übergeben, nicht gehalten: so bleibt utils/ zustandslos, und
alles darüber ist ohne GPU und ohne installiertes faster-whisper testbar.

Beide Funktionen hier blockieren und gehören ausnahmslos in `asyncio.to_thread`
– die Invariante "kein blockierendes I/O im Event-Loop" gilt auch für die GPU.
"""
import os
import sys

from utils.logger import logger

# Reihenfolge der Ladeversuche. float16 ist auf einer RTX-Karte das Schnellste;
# int8_float16 fängt Karten/cuDNN-Builds ab, die bei float16 zicken.
_KASKADE = (("cuda", "float16"), ("cuda", "int8_float16"))
_CPU_VERSUCH = ("cpu", "int8")


def ensure_cuda_dlls() -> None:
    """Registriert die CUDA-DLLs aus den nvidia-*-Wheels (nur Windows).

    Ohne das findet ctranslate2 cuBLAS/cuDNN nicht und meldet ein
    nichtssagendes "cudnn_ops64_9.dll is not found". Liegen die Bibliotheken
    bereits systemweit (etwa durch eine andere CUDA-Installation), ist das hier
    schlicht wirkungslos – deshalb schluckt es jeden Fehler.
    """
    if sys.platform != "win32":
        return
    try:
        import importlib.util
        spec = importlib.util.find_spec("nvidia")
        if not spec or not spec.submodule_search_locations:
            return
        wurzel = list(spec.submodule_search_locations)[0]
        for eintrag in os.scandir(wurzel):
            bin_dir = os.path.join(eintrag.path, "bin")
            if os.path.isdir(bin_dir):
                os.add_dll_directory(bin_dir)
    except Exception:
        pass


def load_model(size: str, device: str = "cuda", compute_type: str = "float16",
               allow_cpu: bool = False, download_root: str = ""):
    """Lädt das Whisper-Modell. Blockierend – in to_thread aufrufen.

    Erster Versuch immer mit local_files_only: das Modell liegt meist schon im
    HF-Cache (der DM-Bot benutzt dasselbe), und ohne diese Bremse würden bei
    verschobenem Cache still 1,5 GB gezogen.
    """
    from faster_whisper import WhisperModel

    ensure_cuda_dlls()

    versuche = [(device, compute_type)]
    versuche += [v for v in _KASKADE if v != (device, compute_type)]
    if allow_cpu:
        versuche.append(_CPU_VERSUCH)

    optionen = {"download_root": download_root} if download_root else {}
    letzter = None
    for dev, ct in versuche:
        for nur_lokal in (True, False):
            try:
                modell = WhisperModel(size, device=dev, compute_type=ct,
                                      local_files_only=nur_lokal, **optionen)
                if not nur_lokal:
                    logger.info(f"[STT] Modell {size} wurde heruntergeladen.")
                logger.info(f"[Voice-Listen] Sprachmodell bereit: {size} / {dev} / {ct}")
                return modell, f"{size} / {dev} / {ct}"
            except Exception as e:
                letzter = e
                if nur_lokal and "local_files_only" not in str(e).lower():
                    continue
        logger.warning(f"[Voice-Listen] {dev}/{ct} nicht nutzbar: "
                       f"{type(letzter).__name__}: {str(letzter)[:120]}")

    raise RuntimeError(f"{type(letzter).__name__}: {str(letzter)[:200]}")


def transcribe(model, audio, *, language="de", initial_prompt=None) -> str:
    """Transkribiert 16-kHz-Mono-float32. Blockierend – in to_thread aufrufen.

    beam_size=1 kostet den letzten Prozentpunkt Genauigkeit und spart spürbar
    Latenz. condition_on_previous_text=False, weil Whisper sonst
    Halluzinationen von Segment zu Segment mitschleppt.
    """
    segmente, _info = model.transcribe(
        audio,
        language=language,
        beam_size=1,
        vad_filter=True,
        condition_on_previous_text=False,
        temperature=0.0,
        initial_prompt=initial_prompt,
    )
    return " ".join(s.text.strip() for s in segmente).strip()
