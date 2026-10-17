"""Persian speech to text: ffmpeg converts Telegram OGG/Opus to 16 kHz mono WAV, Vosk transcribes."""

import json
import logging
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000


class SttError(RuntimeError):
    pass


@dataclass
class Transcription:
    text: str
    confidence: float  # mean word confidence, 0..1; 0 when nothing was recognised

    def is_usable(self, min_confidence: float) -> bool:
        return bool(self.text.strip()) and self.confidence >= min_confidence


@lru_cache
def _model():
    from vosk import Model, SetLogLevel

    SetLogLevel(-1)
    path = get_settings().vosk_model_path
    if not Path(path).is_dir():
        raise SttError(f"Vosk model not found at {path}")
    return Model(path)


def to_wav(src: Path, dst: Path) -> None:
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(src),
         "-ac", "1", "-ar", str(SAMPLE_RATE), "-sample_fmt", "s16", str(dst)],
        capture_output=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise SttError(f"ffmpeg failed: {result.stderr.decode(errors='replace')[:300]}")


def transcribe_wav(path: Path) -> Transcription:
    from vosk import KaldiRecognizer

    recognizer = KaldiRecognizer(_model(), SAMPLE_RATE)
    recognizer.SetWords(True)
    texts: list[str] = []
    confidences: list[float] = []

    def collect(raw: str) -> None:
        result = json.loads(raw)
        if result.get("text"):
            texts.append(result["text"])
        confidences.extend(w.get("conf", 0.0) for w in result.get("result", []))

    with wave.open(str(path), "rb") as wav:
        while data := wav.readframes(4000):
            if recognizer.AcceptWaveform(data):
                collect(recognizer.Result())
    collect(recognizer.FinalResult())

    text = " ".join(texts).strip()
    confidence = sum(confidences) / len(confidences) if confidences else 0.0
    return Transcription(text=text, confidence=round(confidence, 3))


def transcribe_file(audio: Path) -> Transcription:
    """Transcribe a Telegram voice file. The caller deletes the original audio."""
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "audio.wav"
        to_wav(audio, wav)
        result = transcribe_wav(wav)
    logger.info("Transcribed %s: %d chars, confidence %.2f", audio.name, len(result.text), result.confidence)
    return result
