import subprocess
from pathlib import Path

import pytest

from app.stt.vosk_stt import transcribe_file


@pytest.fixture
def silent_ogg(tmp_path: Path) -> Path:
    """Two seconds of silence encoded like a Telegram voice note (OGG/Opus)."""
    path = tmp_path / "silence.ogg"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
         "anullsrc=r=48000:cl=mono", "-t", "2", "-c:a", "libopus", str(path)],
        check=True,
    )
    return path


def test_silence_is_not_usable(silent_ogg: Path) -> None:
    result = transcribe_file(silent_ogg)
    assert result.text == ""
    assert not result.is_usable(0.5)
